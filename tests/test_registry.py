from __future__ import annotations

import unittest

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters import AdapterRegistry, DuplicateAdapterError, MissingAdapterError
from tsm_agt.ports import ModelProviderPort, RuntimeStorePort


class AdapterRegistryTest(unittest.TestCase):
    def test_register_and_resolve_singleton(self) -> None:
        registry = AdapterRegistry()
        adapter = EchoModelProvider()

        registry.register(ModelProviderPort, adapter)

        self.assertIs(registry.require(ModelProviderPort), adapter)

    def test_duplicate_adapter_id_is_rejected(self) -> None:
        registry = AdapterRegistry()
        registry.register(ModelProviderPort, EchoModelProvider())

        with self.assertRaises(DuplicateAdapterError):
            registry.register(ModelProviderPort, EchoModelProvider())

    def test_missing_required_port_is_rejected(self) -> None:
        registry = AdapterRegistry()

        with self.assertRaises(MissingAdapterError):
            registry.require(RuntimeStorePort)


if __name__ == "__main__":
    unittest.main()
