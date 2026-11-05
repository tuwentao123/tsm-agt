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

import asyncio
from collections.abc import Sequence
from contextlib import AsyncExitStack
import math
from typing import Any, Mapping
import sys

from tsm_agt.ports import MCPCallNotSentError, MCPServerConfig, MCPServerStartupError
from .mcp_errors import mcp_failure_detail


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


_MAX_TOOL_LIST_PAGES = 1000
_MAX_DISCOVERED_TOOLS = 10000


class _WrappedMCPClient:
    """Proxy whose SDK contexts live entirely in one long-lived owner Task."""

    def __init__(self, kind: str, server_id: str) -> None:
        self.kind = kind
        self._server_id = server_id
        self._session: Any = None
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._closing = False
        self._tearing_down = False
        self._failure: BaseException | None = None
        self._owner: asyncio.Task[None] | None = None

    async def _run(self, transport: Any) -> None:
        try:
            async with AsyncExitStack() as exits:
                try:
                    streams = await exits.enter_async_context(transport)
                    session = await exits.enter_async_context(
                        _ClientSession(streams[0], streams[1])
                    )
                    await session.initialize()
                    self._session = session
                    self._ready.set_result(None)
                    await self._stop.wait()
                finally:
                    self._tearing_down = True
                    self._session = None
        except asyncio.CancelledError as error:
            # The factory preserves its caller's cancellation after joining us.
            # Store owner cancellation rather than leaving an unobserved task
            # exception; the SDK scopes have already unwound in this Task.
            self._failure = error
        except Exception as error:
            self._failure = error
        finally:
            self._session = None
            if not self._ready.done():
                self._ready.set_result(None)

    def _require_session(self) -> Any:
        if self._closing or self._session is None or self._owner is None or self._owner.done():
            raise MCPCallNotSentError(
                f"MCP server '{self._server_id}' connection is not available"
            ) from self._failure
        return self._session

    async def list_tools(self) -> Any:
        """Return tool descriptors from the underlying MCP client.

        Complete every page before exposing descriptors to ``MCPToolProvider``.
        Repeated cursors and bounded discovery limits reject the whole catalog.
        """

        tools: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_TOOL_LIST_PAGES):
            session = self._require_session()
            page = (
                await session.list_tools() if cursor is None
                else await session.list_tools(cursor=cursor)
            )
            items = getattr(page, "tools", page)
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                raise RuntimeError("MCP tools/list returned an invalid tools sequence")
            tools.extend(items)
            if len(tools) > _MAX_DISCOVERED_TOOLS:
                raise RuntimeError("MCP tools/list exceeded the tool discovery limit")
            cursor = getattr(page, "nextCursor", None)
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise RuntimeError("MCP tools/list returned an invalid or repeated cursor")
            seen_cursors.add(cursor)
        raise RuntimeError("MCP tools/list exceeded the page discovery limit")

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Call a single MCP tool and return the SDK's result object."""

        session = self._require_session()
        return await session.call_tool(name, dict(arguments))

    async def aclose(self) -> None:
        """Ask the owner to close; join it before propagating caller cancellation."""
        self._closing = True
        self._stop.set()
        await self._wait_owner()
        if self._failure is not None and not isinstance(self._failure, asyncio.CancelledError):
            raise RuntimeError(
                f"MCP server '{self._server_id}' session failed"
            ) from self._failure

    async def _wait_owner(self) -> None:
        assert self._owner is not None
        cancelled = False
        while not self._owner.done():
            try:
                await asyncio.shield(self._owner)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        self._owner.result()


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

        # Preserve argv. Bare Python names deliberately use the Agent's Python
        # for compatibility; explicit interpreter paths are kept unchanged.
        command = list(config.command)
        program = command[0]
        args = command[1:]

        if program in {"python", "python3"}:
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
    """Keep AnyIO context entry/exit in the same Task, independent of callers."""
    client = _WrappedMCPClient(kind, server_id)
    client._owner = asyncio.create_task(
        client._run(transport), name=f"mcp-session:{server_id}",
    )
    try:
        await asyncio.shield(client._ready)
    except asyncio.CancelledError:
        if not client._tearing_down:
            client._owner.cancel()
        try:
            await client._wait_owner()
        except asyncio.CancelledError:
            pass  # Repeated caller cancellation must not abandon SDK cleanup.
        raise
    if client._session is None or client._owner.done():
        if isinstance(client._failure, asyncio.CancelledError):
            raise asyncio.CancelledError
        raise MCPServerStartupError(
            server_id, "initialization", mcp_failure_detail(client._failure)
        ) from client._failure
    return client


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
            or not math.isfinite(float(value)) or float(value) <= 0):
        raise ValueError(f"MCP server option '{field}' must be positive")
    return float(value)


__all__ = [
    "create_mcp_sdk_client",
]

