"""Explicit per-request model routing; no mutable process-wide selection."""
from dataclasses import replace
import copy
from tsm_agt.ports import AdapterDescriptor, ModelStreamCompleted, StreamingModelProviderPort
from tsm_agt.ports.model import ReasoningBlock


class ModelProfileProvider:
    def __init__(self, providers, default_profile, catalog, configuration_hashes=None):
        if default_profile not in providers:
            raise ValueError("default model profile must be enabled")
        self._providers = dict(providers)
        self.default_profile = default_profile
        self._catalog = tuple(dict(row) for row in catalog)
        self._configuration_hashes = dict(configuration_hashes or {})
        self.capabilities = self._providers[default_profile].capabilities
        self.default_provider = self._providers[default_profile]
        for name in ("_model", "_base_url", "_timeout_seconds", "_output_token_parameter", "_strict_tool_schema", "_streaming"):
            if hasattr(self.default_provider, name):
                setattr(self, name, getattr(self.default_provider, name))
        self.descriptor = AdapterDescriptor(
            "builtin.model-profiles", "1.0", "ModelProviderPort", "1.0",
            frozenset({"profile-routing", "streaming"}),
        )

    def resolve_profile(self, profile_id=None):
        selected = profile_id or self.default_profile
        if selected not in self._providers:
            raise ValueError(f"unknown or disabled model profile: {selected}")
        return selected

    def profile_catalog(self):
        return {"default_profile": self.default_profile, "profiles": copy.deepcopy(list(self._catalog))}

    def profile_fingerprint(self, profile_id=None):
        return self._configuration_hashes.get(self.resolve_profile(profile_id))

    def capabilities_for(self, profile_id=None):
        return self._providers[self.resolve_profile(profile_id)].capabilities

    def output_token_limit_for(self, profile_id=None):
        provider = self._providers[self.resolve_profile(profile_id)]
        lookup = getattr(provider, "output_token_limit_for", None)
        return lookup() if lookup else None

    async def start(self, context):
        started = []
        try:
            for provider in self._providers.values():
                await provider.start(context)
                started.append(provider)
        except BaseException:
            from datetime import datetime, timezone
            for provider in reversed(started):
                await provider.stop(datetime.now(timezone.utc))
            raise

    async def stop(self, deadline):
        for provider in reversed(tuple(self._providers.values())):
            await provider.stop(deadline)

    async def health(self):
        return await self._providers[self.default_profile].health()

    def _annotate(self, response, selected):
        row = next(row for row in self._catalog if row["id"] == selected)
        message = replace(response.message, content=tuple(
            replace(block, model_profile_id=selected) if isinstance(block, ReasoningBlock) else block
            for block in response.message.content
        ))
        return replace(response, message=message, diagnostics={**response.diagnostics, "model_profile": {
            "id": selected, "requested_model": row["model"], "protocol": row["protocol"],
        }})

    def _request_for_profile(self, request, selected):
        row = next(row for row in self._catalog if row["id"] == selected)
        allowed = row.get("reasoning_efforts", [])
        if request.reasoning_effort is not None and allowed and request.reasoning_effort not in allowed:
            raise ValueError(f"reasoning effort {request.reasoning_effort!r} is not supported by profile {selected}; allowed: {', '.join(allowed)}")
        # Signatures/encrypted reasoning belong to their originating model,
        # even when two profiles share a wire protocol. Keep portable history.
        messages = []
        for message in request.messages:
            content = tuple(block for block in message.content
                            if not isinstance(block, ReasoningBlock) or block.model_profile_id == selected)
            if content:
                messages.append(replace(message, content=content))
        return replace(request, messages=tuple(messages))

    async def complete(self, request):
        selected = self.resolve_profile(request.model_profile_id)
        return self._annotate(await self._providers[selected].complete(self._request_for_profile(request, selected)), selected)

    async def stream_complete(self, request):
        selected = self.resolve_profile(request.model_profile_id)
        provider = self._providers[selected]
        request = self._request_for_profile(request, selected)
        if not isinstance(provider, StreamingModelProviderPort):
            yield ModelStreamCompleted(self._annotate(await provider.complete(request), selected))
            return
        async for event in provider.stream_complete(request):
            if isinstance(event, ModelStreamCompleted):
                event = ModelStreamCompleted(self._annotate(event.response, selected))
            yield event
