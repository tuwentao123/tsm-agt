"""Authenticated loopback HTTP/SSE transport for the public Python SDK."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tsm_agt.core import ApprovalDecision
from tsm_agt.sdk import EngineeringAgentClient


class LocalEventApiServer:
    """Own one SDK client on one event loop and expose a narrow local API."""

    def __init__(
        self, workspace: Path, *, host: str = "127.0.0.1", port: int = 8765,
        token: str | None = None, application_factory=None,
    ) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("local Event API may bind only to a loopback host")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        selected_token = token or secrets.token_urlsafe(32)
        if len(selected_token) < 24:
            raise ValueError("local Event API token must contain at least 24 characters")
        self.workspace = workspace.expanduser().resolve()
        self.host = host
        self.port = port
        self.token = selected_token
        self._client = EngineeringAgentClient(
            self.workspace, application_factory=application_factory
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._http: ThreadingHTTPServer | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._http is None:
            return self.host, self.port
        host, port = self._http.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._http is not None:
            return
        ready = threading.Event()
        loop_error: list[BaseException] = []

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._client.start())
            except BaseException as error:
                loop_error.append(error)
                ready.set()
                loop.close()
                return
            ready.set()
            loop.run_forever()
            loop.run_until_complete(self._client.close())
            loop.close()

        self._loop_thread = threading.Thread(
            target=run_loop, name="tsm-agt-local-api-runtime", daemon=True
        )
        self._loop_thread.start()
        ready.wait(timeout=10)
        if loop_error:
            raise RuntimeError("failed to start local Event API Runtime") from loop_error[0]
        if self._loop is None:
            raise RuntimeError("local Event API Runtime did not start")
        handler = self._handler_type()
        server_type = ThreadingHTTPServer
        if self.host == "::1":
            class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
                address_family = socket.AF_INET6
            server_type = IPv6ThreadingHTTPServer
        self._http = server_type((self.host, self.port), handler)

    def serve_forever(self) -> None:
        self.start()
        assert self._http is not None
        self._http.serve_forever(poll_interval=0.2)

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=10)
            self._loop_thread = None
        self._loop = None

    def _call(self, awaitable, *, timeout: float = 70) -> Any:
        if self._loop is None:
            raise RuntimeError("local Event API is not running")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result(timeout=timeout)

    def _handler_type(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "tsm-agt-local-api/1"

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _dispatch(self, method: str) -> None:
                try:
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {
                            "error": {"code": "UNAUTHORIZED",
                                      "message": "valid Bearer token required"}
                        })
                        return
                    parsed = urlsplit(self.path)
                    parts = tuple(part for part in parsed.path.split("/") if part)
                    query = parse_qs(parsed.query)
                    if method == "GET" and parts == ("v1", "health"):
                        self._json(HTTPStatus.OK, {
                            "status": "healthy", "schema_version": 1
                        })
                    elif method == "POST" and parts == ("v1", "tasks"):
                        body = self._body()
                        result = owner._call(owner._client.submit_task(
                            _required_text(body, "goal"),
                            command_id=_required_text(body, "command_id"),
                            session_id=_optional_text(body, "session_id"),
                        ))
                        self._json(HTTPStatus.ACCEPTED, result.to_data())
                    elif method == "GET" and len(parts) == 3 and parts[:2] == ("v1", "tasks"):
                        result = owner._call(owner._client.get_task_result(parts[2]))
                        self._json(HTTPStatus.OK, result.to_data())
                    elif method == "GET" and len(parts) == 4 and parts[:2] == ("v1", "tasks") and parts[3] == "events":
                        after = _query_int(query, "after", 0, minimum=0, maximum=2**63 - 1)
                        wait = _query_float(query, "wait", 0, minimum=0, maximum=30)
                        self._events(parts[2], after, wait)
                    elif method == "GET" and len(parts) == 4 and parts[:2] == ("v1", "tasks") and parts[3] == "progress":
                        after = _query_int(
                            query, "after", 0, minimum=0, maximum=2**63 - 1
                        )
                        wait = _query_float(
                            query, "wait", 0, minimum=0, maximum=30
                        )
                        self._progress(parts[2], after, wait)
                    elif method == "GET" and len(parts) == 4 and parts[:2] == ("v1", "tasks") and parts[3] == "spec":
                        result = owner._call(owner._client.get_task_spec(parts[2]))
                        self._json(HTTPStatus.OK, dict(result))
                    elif method == "POST" and len(parts) == 4 and parts[:2] == ("v1", "tasks") and parts[3] == "spec":
                        body = self._body()
                        scope = _string_array(body, "scope")
                        constraints = _string_array(body, "constraints")
                        criteria = body.get("acceptance_criteria")
                        if not isinstance(criteria, list) or not all(
                            isinstance(item, dict) for item in criteria
                        ):
                            raise ValueError("acceptance_criteria must be an object array")
                        result = owner._call(owner._client.revise_task_spec(
                            parts[2], expected_revision=_required_int(
                                body, "expected_revision", minimum=1
                            ),
                            scope=scope, constraints=constraints,
                            acceptance_criteria=tuple(criteria),
                            command_id=_required_text(body, "command_id"),
                        ))
                        self._json(HTTPStatus.OK, result.to_data())
                    elif method == "POST" and len(parts) == 4 and parts[:2] == ("v1", "tasks") and parts[3] == "interrupt":
                        body = self._body()
                        result = owner._call(owner._client.interrupt(
                            parts[2], command_id=_required_text(body, "command_id"),
                            reason=_required_text(body, "reason"),
                        ))
                        self._json(HTTPStatus.OK, result.to_data())
                    elif method == "POST" and len(parts) == 4 and parts[:2] == ("v1", "tasks") and parts[3] in {"steer", "replace"}:
                        body = self._body()
                        operation = (
                            owner._client.steer if parts[3] == "steer"
                            else owner._client.replace
                        )
                        result = owner._call(operation(
                            parts[2], _required_text(body, "text"),
                            command_id=_required_text(body, "command_id"),
                        ))
                        self._json(HTTPStatus.ACCEPTED, result.to_data())
                    elif method == "POST" and len(parts) == 4 and parts[:2] == ("v1", "tasks") and parts[3] == "input":
                        body = self._body()
                        result = owner._call(owner._client.route_input(
                            parts[2], _required_text(body, "text"),
                            command_id=_required_text(body, "command_id"),
                            intent=_optional_text(body, "intent"),
                        ))
                        status = (
                            HTTPStatus.ACCEPTED
                            if result.result.get("applied")
                            else HTTPStatus.OK
                        )
                        self._json(status, result.to_data())
                    elif method == "POST" and len(parts) == 3 and parts[:2] == ("v1", "approvals"):
                        body = self._body()
                        result = owner._call(owner._client.resolve_approval(
                            parts[2], ApprovalDecision(_required_text(body, "decision")),
                            _required_text(body, "reason"),
                            command_id=_required_text(body, "command_id"),
                        ))
                        self._json(HTTPStatus.OK, result.to_data())
                    elif method == "POST" and len(parts) == 3 and parts[:2] == ("v1", "clarifications"):
                        body = self._body()
                        result = owner._call(owner._client.answer_clarification(
                            parts[2], _required_text(body, "resume_token"),
                            _optional_text(body, "answer"),
                            selected_choice=_optional_text(
                                body, "selected_choice"
                            ),
                            command_id=_required_text(body, "command_id"),
                        ))
                        self._json(HTTPStatus.OK, result.to_data())
                    else:
                        self._json(HTTPStatus.NOT_FOUND, {
                            "error": {"code": "NOT_FOUND", "message": "route not found"}
                        })
                except (BrokenPipeError, ConnectionResetError):
                    return
                except (KeyError, LookupError) as error:
                    self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", error)
                except PermissionError as error:
                    self._error(HTTPStatus.FORBIDDEN, "FORBIDDEN", error)
                except (TypeError, ValueError) as error:
                    self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", error)
                except Exception:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": {
                            "code": "COMMAND_NOT_COMPLETED",
                            "message": (
                                "command could not be completed; inspect the "
                                "redacted Task status and Event stream"
                            ),
                        }
                    })

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                prefix = "Bearer "
                return header.startswith(prefix) and hmac.compare_digest(
                    header[len(prefix):], owner.token
                )

            def _body(self) -> dict[str, Any]:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError as error:
                    raise ValueError("invalid Content-Length") from error
                if not 0 < length <= 1_000_000:
                    raise ValueError("JSON body must contain 1..1000000 bytes")
                raw = self.rfile.read(length)
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise ValueError("request body must be valid UTF-8 JSON") from error
                if not isinstance(decoded, dict):
                    raise ValueError("request body must be a JSON object")
                return decoded

            def _events(self, task_id: str, after: int, wait: float) -> None:
                async def collect():
                    events = await owner._client.read_events(task_id, after=after)
                    if events or wait <= 0:
                        return events
                    async for event in owner._client.subscribe_events(
                        task_id, after=after, timeout=wait
                    ):
                        return (event,)
                    return ()

                events = owner._call(collect(), timeout=wait + 5)
                chunks = [
                    "event: runtime.event\n"
                    f"id: {event.cursor}\n"
                    f"data: {json.dumps(event.to_data(), ensure_ascii=False, separators=(',', ':'))}\n\n"
                    for event in events
                ]
                chunks.append("event: cursor\n" + "data: " + json.dumps({
                    "after": events[-1].cursor if events else after,
                    "count": len(events),
                }, separators=(",", ":")) + "\n\n")
                encoded = "".join(chunks).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _progress(self, task_id: str, after: int, wait: float) -> None:
                """Send non-persistent, non-redacted progress to the local UI."""
                async def collect():
                    await owner._client.application.kernel.get_task(task_id)
                    items = owner._client.read_progress(task_id, after=after)
                    if items or wait <= 0:
                        return items
                    async for item in owner._client.subscribe_progress(
                        task_id, after=after, timeout=wait
                    ):
                        return (item,)
                    return ()

                items = owner._call(collect(), timeout=wait + 5)
                chunks = [
                    "event: agent.progress\n"
                    f"id: {item.sequence}\n"
                    f"data: {json.dumps(item.to_data(), ensure_ascii=False, separators=(',', ':'))}\n\n"
                    for item in items
                ]
                chunks.append("event: cursor\n" + "data: " + json.dumps({
                    "after": items[-1].sequence if items else after,
                    "count": len(items),
                    "persistence": "ephemeral",
                    "redaction": "none-local-authenticated-ui",
                }, separators=(",", ":")) + "\n\n")
                encoded = "".join(chunks).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type", "text/event-stream; charset=utf-8"
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _error(self, status: HTTPStatus, code: str, error: Exception) -> None:
                message = " ".join(str(error).split())[:300] or code
                self._json(status, {"error": {"code": code, "message": message}})

            def _json(self, status: HTTPStatus, data: dict[str, Any]) -> None:
                encoded = json.dumps(
                    data, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string when provided")
    return value.strip()


def _string_array(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError(f"{key} must be an array with at most 50 strings")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{key} must contain non-empty strings")
    return tuple(item.strip() for item in value)


def _required_int(
    data: dict[str, Any], key: str, *, minimum: int,
) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{key} must be an integer >= {minimum}")
    return value


def _query_int(
    query: dict[str, list[str]], key: str, default: int, *, minimum: int, maximum: int,
) -> int:
    raw = query.get(key, [str(default)])[-1]
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{key} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _query_float(
    query: dict[str, list[str]], key: str, default: float, *,
    minimum: float, maximum: float,
) -> float:
    raw = query.get(key, [str(default)])[-1]
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{key} must be a number") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value
