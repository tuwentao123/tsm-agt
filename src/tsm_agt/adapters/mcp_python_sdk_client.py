"""Integration with the official MCP Python SDK.

This module provides a concrete ``client_factory`` implementation for
``MCPClientManager`` using the `modelcontextprotocol/python-sdk` package
(`mcp`).

It keeps all SDK-specific logic in one place so the rest of the tsm-agt
codebase can continue to depend only on the abstract ``MCPServerConfig`` and
``MCPClientManager`` types.

The factory produces lightweight client wrappers that implement exactly the
minimal protocol required by :class:`MCPToolProvider`:

* ``await client.list_tools()`` → iterable of tool descriptors;
* ``await client.call_tool(name: str, arguments: Mapping[str, Any])``.

Configuration mapping
---------------------

``MCPServerConfig`` is interpreted as follows:

* ``kind == "http"``
  * ``url`` must be a non-empty string;
  * the SDK's streamable-HTTP transport and a ``ClientSession`` are opened
    against that URL.

* ``kind == "executable"``
  * ``command`` must be a non-empty tuple of program and arguments;
  * a stdio subprocess is launched via ``mcp.client.stdio.stdio_client`` and
    wrapped in a ``ClientSession``.

* ``kind == "inprocess"``
  * This is currently treated as an error by the concrete factory because the
    official SDK focuses on stdio and HTTP transports.  Callers that need
    in-process servers can continue to use the demo fixture wiring or provide
    their own custom factory.

The ``options`` field maps explicit transport parameters: executable servers
accept ``env`` and ``cwd``; HTTP servers accept ``headers`` and
``timeout_seconds``. Unknown options are deliberately not guessed or passed
through to the SDK.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import sys

from tsm_agt.ports import MCPServerConfig


try:  # pragma: no cover - import is environment dependent
    # The official MCP Python SDK.  The package name is ``mcp`` and provides
    # both server and client primitives.  Newer versions expose the HTTP
    # client via a ``client`` submodule rather than a top-level ``Client``
    # symbol, so we keep the import logic flexible and only require the
    # pieces we actually use.
    import mcp as _mcp_pkg  # type: ignore[import]
    from mcp.client.stdio import (
        stdio_client as _stdio_client,
        StdioServerParameters as _StdioServerParameters,
    )
    from mcp.client.session import ClientSession as _ClientSession

    from mcp.client.streamable_http import (
        streamablehttp_client as _streamablehttp_client,
    )
    _SDK_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only when SDK missing
    # ImportError is normal if the optional dependency is not installed.  We
    # capture the original exception so the factory can surface a clear error
    # message at runtime instead of failing with an opaque NameError later.
    _SDK_IMPORT_ERROR = exc


@dataclass(slots=True)
class _WrappedMCPClient:
    """Small adapter around the official MCP Client/Session.

    This wrapper normalises the Python SDK API into the minimal
    ``list_tools`` / ``call_tool`` protocol expected by :class:`MCPToolProvider`.
    It is intentionally conservative: it exposes only what tsm-agt needs
    today and leaves advanced features (resources, prompts, streaming) to
    future extensions.
    """

    kind: str
    session: Any
    _exit_stack: AsyncExitStack

    async def list_tools(self) -> Any:
        """Return tool descriptors from the underlying MCP client.

        The return value is passed straight through to ``MCPToolProvider``,
        which only relies on each element exposing ``name``, ``description``
        and an ``inputSchema`` (or compatible) attribute.
        """

        tool_list = await self.session.list_tools()
        # The python-sdk returns a wrapper object with a ``tools`` attribute;
        # retain compatibility with clients that directly return a sequence.
        return getattr(tool_list, "tools", tool_list)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Call a single MCP tool and return the SDK's result object."""

        return await self.session.call_tool(name, dict(arguments))

    async def aclose(self) -> None:
        """Close the session before its transport subprocess/HTTP stream."""
        await self._exit_stack.aclose()


async def create_mcp_sdk_client(config: MCPServerConfig) -> _WrappedMCPClient:
    """Concrete ``client_factory`` backed by the official MCP Python SDK.

    This function is designed to be passed directly to
    :class:`MCPClientManager` or :func:`compose_mcp_application_from_config`.

    It raises ``RuntimeError`` with a clear message when the ``mcp`` package
    is not installed so hosts can surface a helpful diagnostic.
    """

    if _SDK_IMPORT_ERROR is not None:
        raise RuntimeError(
            "The 'mcp' Python package is required for MCP integration but could "
            "not be imported. Install it with `pip install \"mcp[cli]\"` or "
            "add it to your project's dependencies."
        ) from _SDK_IMPORT_ERROR

    kind = config.kind.casefold()

    if kind in {"http", "streamable_http"}:
        if not config.url:
            raise ValueError(
                f"MCP server '{config.id}' of kind 'http' requires a non-empty url"
            )

        headers = _string_mapping(config.options.get("headers"), "headers")
        timeout_seconds = _positive_number(
            config.options.get("timeout_seconds", 30.0), "timeout_seconds"
        )
        return await _session_client(
            kind="http",
            transport=_streamablehttp_client(
                config.url, headers=headers or None, timeout=timeout_seconds,
            ),
            server_id=config.id,
        )

    if kind == "executable":
        if not config.command:
            raise ValueError(
                f"MCP server '{config.id}' of kind 'executable' requires a "
                "non-empty command tuple"
            )

        # The python-sdk uses a stdio transport plus a ClientSession.  The
        # exact helper names are stable across the v2 API surface; we keep the
        # integration intentionally simple and focused on the core
        # list_tools/call_tool flow.
        command = list(config.command)
        program = command[0]
        args = command[1:]

        # Some agents pass a single script path as the program; normalise that
        # into a ``Path`` so the SDK can infer the correct interpreter.
        if len(command) == 1:
            server_path = Path(program)
            program = sys.executable
            args = [str(server_path)]
        elif program in {"python", "python3"}:
            # Respect the current Python interpreter rather than relying on a
            # bare "python" shim being available on PATH.
            program = sys.executable

        # ``stdio_client`` in the official SDK is exposed as an async
        # context manager and expects a ``StdioServerParameters`` instance
        # describing how to launch the subprocess.  We map the
        # ``MCPServerConfig.command`` tuple into that structure.

        server_params = _StdioServerParameters(
            command=program,
            args=args,
            env=_string_mapping(config.options.get("env"), "env") or None,
            cwd=_optional_string(config.options.get("cwd"), "cwd"),
            encoding="utf-8",
            encoding_error_handler="strict",
        )
        return await _session_client(
            kind="executable", transport=_stdio_client(server_params),
            server_id=config.id,
        )

    raise ValueError(
        f"Unsupported MCP server kind for official SDK factory: {config.kind!r}"
    )


async def _session_client(*, kind: str, transport: Any, server_id: str) -> _WrappedMCPClient:
    """Enter transport and session scopes before issuing MCP initialize.

    ``ClientSession`` owns request/response task groups. Calling ``initialize``
    outside its async context leaves those groups inactive and can wait forever
    for a response. One exit stack makes the session lifetime match the MCP
    provider lifecycle.
    """
    exits = AsyncExitStack()
    try:
        streams = await exits.enter_async_context(transport)
        read, write = streams[0], streams[1]
        session = await exits.enter_async_context(_ClientSession(read, write))
        await session.initialize()
        return _WrappedMCPClient(kind=kind, session=session, _exit_stack=exits)
    except BaseException:
        await exits.aclose()
        raise RuntimeError(f"MCP server '{server_id}' failed during initialization") from None


def _string_mapping(value: object, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(f"MCP server option '{field}' must be an object of strings")
    return dict(value)


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"MCP server option '{field}' must be a non-empty string")
    return value


def _positive_number(value: object, field: str) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or float(value) <= 0):
        raise ValueError(f"MCP server option '{field}' must be positive")
    return float(value)


__all__ = [
    "create_mcp_sdk_client",
]

