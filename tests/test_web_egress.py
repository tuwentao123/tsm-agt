"""Who owns the egress decision for web.fetch_markdown, and can it be evaded."""

import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.builtin import NetworkToolProvider
from tsm_agt.adapters.builtin.network_tools import (
    FetchResponse, PinnedAddressWebFetchTransport, UrllibWebFetchTransport,
    WebEgressMode, _FetchFailure, _validate_fetch_url,
)
from tsm_agt.bootstrap.egress_configuration import (
    WebEgressConfiguration, load_web_egress_configuration,
)
from tsm_agt.ports import AdapterContext, ToolCall


class _RecordingTransport:
    def __init__(self, response: FetchResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str | None]] = []

    def fetch(
        self, url, *, headers, timeout_seconds, max_bytes, pinned_address=None,
    ):
        self.calls.append((url, pinned_address))
        return self.response


def _markdown(url: str) -> FetchResponse:
    return FetchResponse(
        200, url, {"Content-Type": "text/markdown; charset=utf-8"},
        b"# Document\n",
    )


async def _fetch(provider: NetworkToolProvider, url: str):
    await provider.start(AdapterContext({}, lambda *_: None))
    try:
        return await provider.invoke(
            ToolCall("fetch", "web.fetch_markdown", {"url": url}), None,
        )
    finally:
        from datetime import datetime
        await provider.stop(datetime.now())


class WebEgressValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_mode_pins_the_address_it_just_validated(self) -> None:
        resolutions: list[str] = []

        def resolver(host: str, _port: int) -> tuple[str, ...]:
            resolutions.append(host)
            return ("93.184.216.34",)

        transport = _RecordingTransport(_markdown("https://docs.example.test/a"))
        provider = NetworkToolProvider(
            fetch_transport=transport, address_resolver=resolver,
            egress_mode=WebEgressMode.DIRECT,
        )
        result = await _fetch(provider, "https://docs.example.test/a")

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(transport.calls, [
            ("https://docs.example.test/a", "93.184.216.34"),
        ])
        # One resolution per hop: the transport must not resolve again, which is
        # what would let a second answer point at an internal address.
        self.assertEqual(resolutions, ["docs.example.test"])
        self.assertEqual(result.data["egress_mode"], "DIRECT")
        self.assertTrue(result.data["address_validated_in_process"])

    async def test_direct_mode_rejects_a_host_resolving_off_the_public_internet(
        self,
    ) -> None:
        provider = NetworkToolProvider(
            fetch_transport=_RecordingTransport(
                _markdown("https://metadata.example.test/")
            ),
            address_resolver=lambda *_: ("169.254.169.254",),
            egress_mode=WebEgressMode.DIRECT,
        )
        result = await _fetch(provider, "https://metadata.example.test/")

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "URL_NOT_ALLOWED")

    async def test_delegated_mode_neither_resolves_nor_pins(self) -> None:
        def resolver(*_args):
            raise AssertionError("DELEGATED egress must not classify addresses")

        transport = _RecordingTransport(_markdown("https://docs.example.test/b"))
        provider = NetworkToolProvider(
            fetch_transport=transport, address_resolver=resolver,
            egress_mode=WebEgressMode.DELEGATED,
        )
        result = await _fetch(provider, "https://docs.example.test/b")

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(transport.calls, [("https://docs.example.test/b", None)])
        self.assertEqual(result.data["egress_mode"], "DELEGATED")
        self.assertFalse(result.data["address_validated_in_process"])

    async def test_delegated_mode_still_refuses_non_http_and_credentials(
        self,
    ) -> None:
        provider = NetworkToolProvider(
            fetch_transport=_RecordingTransport(_markdown("x")),
            address_resolver=lambda *_: ("8.8.8.8",),
            egress_mode=WebEgressMode.DELEGATED,
        )
        for url in ("file:///etc/passwd", "https://user:pass@docs.example.test/"):
            result = await _fetch(provider, url)
            self.assertFalse(result.ok, url)
            self.assertEqual(result.error_code, "URL_NOT_ALLOWED", url)

    def test_delegated_validation_returns_no_pinned_address(self) -> None:
        target = _validate_fetch_url(
            "https://docs.example.test/c", lambda *_: ("8.8.8.8",),
            mode=WebEgressMode.DELEGATED,
        )
        self.assertIsNone(target.pinned_address)

    def test_redirect_hops_are_validated_and_pinned_independently(self) -> None:
        first = _validate_fetch_url(
            "https://docs.example.test/start", lambda *_: ("93.184.216.34",),
        )
        with self.assertRaises(_FetchFailure) as caught:
            _validate_fetch_url(
                "https://internal.example.test/final",
                lambda *_: ("10.0.0.5",),
                previous_scheme="https",
            )
        self.assertEqual(first.pinned_address, "93.184.216.34")
        self.assertEqual(caught.exception.code, "URL_NOT_ALLOWED")


class WebFetchTransportContractTest(unittest.TestCase):
    def test_pinning_transport_refuses_to_guess_a_destination(self) -> None:
        with self.assertRaises(_FetchFailure) as caught:
            PinnedAddressWebFetchTransport().fetch(
                "https://docs.example.test/", headers={},
                timeout_seconds=1.0, max_bytes=16,
            )
        self.assertEqual(caught.exception.code, "EGRESS_PIN_REQUIRED")

    def test_urllib_transport_refuses_a_pin_it_cannot_honour(self) -> None:
        with self.assertRaises(_FetchFailure) as caught:
            UrllibWebFetchTransport().fetch(
                "https://docs.example.test/", headers={},
                timeout_seconds=1.0, max_bytes=16,
                pinned_address="93.184.216.34",
            )
        self.assertEqual(caught.exception.code, "EGRESS_PIN_UNSUPPORTED")

    def test_pinning_transport_sends_the_requested_host_to_the_pinned_socket(
        self,
    ) -> None:
        received: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                received["path"] = self.path
                received["host"] = self.headers.get("Host", "")
                body = b"# Local\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            response = PinnedAddressWebFetchTransport().fetch(
                f"http://docs.example.test:{port}/spec.md?v=1",
                headers={"User-Agent": "tsm-agt-test"},
                timeout_seconds=5.0, max_bytes=1024,
                pinned_address="127.0.0.1",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"# Local\n")
        self.assertEqual(received["path"], "/spec.md?v=1")
        # The socket went to the pinned address, but the request still names the
        # host the caller asked for, so virtual hosting and TLS stay correct.
        self.assertEqual(received["host"], f"docs.example.test:{port}")

    def test_pinning_transport_truncates_instead_of_failing_on_oversize(
        self,
    ) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                body = b"x" * 128
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            response = PinnedAddressWebFetchTransport().fetch(
                f"http://docs.example.test:{port}/big",
                headers={}, timeout_seconds=5.0, max_bytes=16,
                pinned_address="127.0.0.1",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.body, b"x" * 16)
        self.assertTrue(response.truncated)


class WebEgressConfigurationTest(unittest.TestCase):
    def test_default_is_direct_so_deployments_validate_in_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration = load_web_egress_configuration(
                Path(directory) / ".env", {}
            )
        self.assertEqual(configuration.mode, WebEgressMode.DIRECT)
        self.assertEqual(configuration.sources["web.egress_mode"], "default")
        self.assertEqual(WebEgressConfiguration().mode, WebEgressMode.DIRECT)

    def test_environment_and_env_file_can_delegate_egress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_WEB_EGRESS_MODE=delegated\n", encoding="utf-8"
            )
            from_file = load_web_egress_configuration(env_file, {})
            exported = load_web_egress_configuration(
                env_file, {"TSM_AGT_WEB_EGRESS_MODE": "DIRECT"}
            )
        self.assertEqual(from_file.mode, WebEgressMode.DELEGATED)
        self.assertEqual(from_file.sources["web.egress_mode"], "env_file")
        self.assertEqual(exported.mode, WebEgressMode.DIRECT)
        self.assertEqual(exported.sources["web.egress_mode"], "environment")

    def test_unknown_mode_fails_instead_of_silently_weakening_egress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "TSM_AGT_WEB_EGRESS_MODE"):
                load_web_egress_configuration(
                    Path(directory) / ".env",
                    {"TSM_AGT_WEB_EGRESS_MODE": "off"},
                )


class WebEgressCompositionTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _environment(**extra: str) -> dict[str, str]:
        return {
            "TSM_AGT_MODEL_BASE_URL": "https://models.example.test/v1",
            "TSM_AGT_MODEL": "test-model",
            "TSM_AGT_MODEL_API_KEY": "test-secret",
            **extra,
        }

    def _compose(self, **extra: str) -> NetworkToolProvider:
        from tsm_agt.bootstrap import (
            compose_openai_compatible_engineering_application_from_env,
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, self._environment(**extra), clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_env(
                        env_file=Path(directory) / ".env"
                    )
                )
        from tsm_agt.ports import ToolProviderPort
        return next(
            provider for provider in application.registry.all(ToolProviderPort)
            if isinstance(provider, NetworkToolProvider)
        )

    async def test_default_composition_validates_addresses_in_process(self) -> None:
        provider = self._compose()
        self.assertEqual(provider.egress_mode, WebEgressMode.DIRECT)
        self.assertIsInstance(
            provider._fetch_transport, PinnedAddressWebFetchTransport
        )

    async def test_configured_delegation_selects_the_delegating_transport(
        self,
    ) -> None:
        provider = self._compose(TSM_AGT_WEB_EGRESS_MODE="DELEGATED")
        self.assertEqual(provider.egress_mode, WebEgressMode.DELEGATED)
        self.assertIsInstance(provider._fetch_transport, UrllibWebFetchTransport)


if __name__ == "__main__":
    unittest.main()
