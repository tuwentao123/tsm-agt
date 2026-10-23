"""Governed process environment construction for runtime execution."""

from __future__ import annotations

import os
from collections.abc import Mapping

from tsm_agt.ports import ProcessEnvironmentPolicy

#: Fallback for isolated execution or hosts without PATH. Governed execution
#: explicitly inherits PATH to resolve user-installed CLIs and their runtimes
#: consistently with the service's launch environment.
_BASE_ENVIRONMENT = {
    "PATH": (
        "/usr/bin:/bin:/usr/sbin:/sbin"
        ":/opt/homebrew/bin:/usr/local/bin"
    ),
    "LANG": "C.UTF-8",
}


class ProcessEnvironmentConfiguration:
    def build_environment(
        self,
        requested_environment: Mapping[str, str],
        policy: ProcessEnvironmentPolicy | None = None,
        host_environment: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        effective_policy = policy or ProcessEnvironmentPolicy()
        host = dict(os.environ if host_environment is None else host_environment)
        safe = dict(_BASE_ENVIRONMENT)

        if effective_policy.inherit_host_environment:
            for name in effective_policy.allowed_host_variables:
                value = host.get(name)
                if value is not None:
                    safe[name] = value

        for blocked in effective_policy.blocked_host_variables:
            safe.pop(blocked, None)

        safe.update(requested_environment)
        safe["TSM_RUNTIME_TRUST_MODE"] = effective_policy.runtime_trust_mode
        return safe
