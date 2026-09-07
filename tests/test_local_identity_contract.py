from __future__ import annotations

import os
import re
import unittest

from tsm_agt.adapters.posix_identity import PosixLocalIdentity
from tsm_agt.adapters.windows_identity import WindowsLocalIdentity
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import AdapterContext, HealthState, LocalIdentityPort


def native_identity() -> LocalIdentityPort:
    return WindowsLocalIdentity() if os.name == "nt" else PosixLocalIdentity()


class NativeLocalIdentityContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_subject_is_stable_os_identity(self) -> None:
        identity = native_identity()
        await identity.start(AdapterContext({}, lambda _type, _payload: None))
        try:
            first = identity.current_subject()
            second = identity.current_subject()
            self.assertEqual(first, second)
            pattern = r"sid:S-\d(?:-\d+)+" if os.name == "nt" else r"uid:\d+"
            self.assertRegex(first, re.compile(pattern))
            self.assertEqual((await identity.health()).state, HealthState.HEALTHY)
        finally:
            await identity.stop(None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(RuntimeError, "not started"):
            identity.current_subject()

    async def test_composition_registers_native_identity(self) -> None:
        application = compose_fixture_application()
        identity = application.registry.require(LocalIdentityPort)
        expected = (
            "builtin.windows-local-identity"
            if os.name == "nt" else "builtin.posix-local-identity"
        )
        self.assertEqual(identity.descriptor.adapter_id, expected)
        await application.registry.start_all()
        try:
            self.assertTrue(identity.current_subject())
        finally:
            await application.registry.stop_all()

    async def test_non_native_identity_fails_closed(self) -> None:
        identity = PosixLocalIdentity() if os.name == "nt" else WindowsLocalIdentity()
        expected = "cannot start on Windows" if os.name == "nt" else "only on Windows"
        with self.assertRaisesRegex(RuntimeError, expected):
            await identity.start(AdapterContext({}, lambda _type, _payload: None))


if __name__ == "__main__":
    unittest.main()
