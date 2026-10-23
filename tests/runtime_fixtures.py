"""Model fixtures for SDK and local API plumbing, without artificial budget failures."""
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.ports import ProviderCapabilities


class RuntimeEchoModel(EchoModelProvider):
    # Full conversation prompts need more room than the minimal echo adapter.
    # Small context windows remain covered by the dedicated compaction suite.
    capabilities = ProviderCapabilities(context_window=32768)
