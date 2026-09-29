"""Shared JSON-over-HTTP transport for model provider adapters.

This module is *shared infrastructure*, not a provider adapter. It lives under
``adapters/`` so it is discoverable next to its consumers, but its members are
protocol-neutral: POST a JSON body, stream a `text/event-stream`, normalize the
transport failure. Provider adapters (OpenAI-compatible, Anthropic, ...) may all
depend on it; they must never depend on each other.

Keeping these three symbols here is what lets an Anthropic adapter avoid
importing the OpenAI-compatible package just to reuse its HTTP/SSE plumbing.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tsm_agt.ports import (
    ModelAttemptFailed,
    ModelAttemptFailure,
    ModelFailureCategory,
    ModelRetrySafety,
)


class ModelProviderTransportError(ModelAttemptFailed):
    """Normalized provider-transport failure with an explicit retry class.

    Deliberately not named after any one wire protocol: any JSON-over-HTTP
    provider can raise it.
    """

    def __init__(
        self, message: str, *, retryable: bool = False, reason_code: str = "",
        category: ModelFailureCategory | None = None,
        retry_safety: ModelRetrySafety | None = None,
    ) -> None:
        self.reason_code = reason_code or "provider_error"
        self.category = category or (
            ModelFailureCategory.TRANSIENT_PROVIDER
            if retryable else ModelFailureCategory.INVALID_RESPONSE
        )
        self.retry_safety = retry_safety or (
            ModelRetrySafety.SAFE_SAME_REQUEST
            if retryable else ModelRetrySafety.SAFE_RESAMPLE
        )
        self.retryable = self.retry_safety in {
            ModelRetrySafety.SAFE_SAME_REQUEST,
            ModelRetrySafety.SAFE_RESAMPLE,
            ModelRetrySafety.SAFE_FALLBACK_TRANSPORT,
        }
        super().__init__(ModelAttemptFailure(
            self.category, self.retry_safety, self.reason_code, message
        ))


class HttpJsonTransport(Protocol):
    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...

    def stream_sse(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        cancellation_scope: object | None = None,
    ) -> AsyncIterator[str]: ...


class UrllibHttpJsonTransport:
    """Small stdlib transport; tests inject a fake and never use the network."""

    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            self._post_json_sync, url, headers, payload, timeout_seconds
        )

    async def stream_sse(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        cancellation_scope: object | None = None,
    ) -> AsyncIterator[str]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                **headers,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        loop = asyncio.get_running_loop()
        items: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        stopped = threading.Event()
        # Register the cooperative stop signal with the Task's in-process scope
        # (task-cancellation-spec §8). The scope only signals; it never joins.
        if cancellation_scope is not None:
            register = getattr(cancellation_scope, "register_thread", None)
            if register is not None:
                register(stopped, name="model-stream")
        response_lock = threading.Lock()
        response_holder: list[Any] = []

        def publish(kind: str, value: object = None) -> None:
            if stopped.is_set() and kind == "data":
                return
            try:
                loop.call_soon_threadsafe(items.put_nowait, (kind, value))
            except RuntimeError:
                # The event loop may already be closed after a second Ctrl+C.
                pass

        def read_stream() -> None:
            response = None
            data_lines: list[str] = []
            terminal_published = False
            try:
                response = urlopen(request, timeout=timeout_seconds)
                with response_lock:
                    response_holder.append(response)
                while not stopped.is_set():
                    raw = response.readline()
                    if not raw:
                        if data_lines:
                            publish("data", "\n".join(data_lines))
                        break
                    line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            publish("data", "\n".join(data_lines))
                            data_lines.clear()
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                publish("done")
                terminal_published = True
            except HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")[:1000]
                publish("error", ModelProviderTransportError(
                    f"provider returned HTTP {error.code}: {body}",
                    retryable=(error.code == 429 or 500 <= error.code < 600),
                    reason_code=f"http_{error.code}",
                ))
                terminal_published = True
            except URLError as error:
                publish("error", ModelProviderTransportError(
                    f"provider connection failed: {error.reason}",
                    retryable=True, reason_code="connection_failed",
                ))
                terminal_published = True
            except UnicodeDecodeError:
                publish("error", ModelProviderTransportError(
                    "provider SSE stream is not valid UTF-8"
                ))
                terminal_published = True
            except Exception as error:
                if not stopped.is_set():
                    publish("error", ModelProviderTransportError(
                        f"provider SSE stream failed: {error}",
                        retryable=True, reason_code="stream_failed",
                    ))
                    terminal_published = True
            finally:
                with response_lock:
                    response_holder.clear()
                if response is not None:
                    response.close()
                if not terminal_published and not stopped.is_set():
                    publish("done")

        worker = threading.Thread(
            target=read_stream, name="tsm-agt-model-stream", daemon=True
        )
        worker.start()
        try:
            while True:
                kind, value = await items.get()
                if kind == "data":
                    yield str(value)
                elif kind == "error":
                    assert isinstance(value, Exception)
                    raise value
                else:
                    return
        finally:
            stopped.set()
            with response_lock:
                response = response_holder[0] if response_holder else None
            if response is not None:
                # Closing a buffered response concurrently with readline can itself
                # block. A daemon closer releases the socket without delaying CLI exit.
                threading.Thread(
                    target=response.close, name="tsm-agt-stream-close", daemon=True
                ).start()

    @staticmethod
    def _post_json_sync(
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:1000]
            transient = error.code == 429 or 500 <= error.code < 600
            raise ModelProviderTransportError(
                f"provider returned HTTP {error.code}: {body}",
                retryable=transient,
                reason_code=f"http_{error.code}",
                category=(
                    ModelFailureCategory.AUTHENTICATION
                    if error.code in {401, 403}
                    else ModelFailureCategory.TRANSIENT_PROVIDER
                    if transient else ModelFailureCategory.CONFIGURATION
                ),
                retry_safety=(
                    ModelRetrySafety.SAFE_SAME_REQUEST
                    if transient else ModelRetrySafety.NEVER
                ),
            ) from error
        except TimeoutError as error:
            raise ModelProviderTransportError(
                "provider response timed out", retryable=True,
                reason_code="read_timeout",
            ) from error
        except URLError as error:
            raise ModelProviderTransportError(
                f"provider connection failed: {error.reason}",
                retryable=True, reason_code="connection_failed",
            ) from error
        except OSError as error:
            raise ModelProviderTransportError(
                f"provider connection failed: {error}", retryable=True,
                reason_code="connection_failed",
            ) from error
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ModelProviderTransportError(
                "provider returned invalid JSON"
            ) from error
        if not isinstance(decoded, Mapping):
            raise ModelProviderTransportError(
                "provider response must be a JSON object"
            )
        return decoded


__all__ = [
    "HttpJsonTransport",
    "ModelProviderTransportError",
    "UrllibHttpJsonTransport",
]
