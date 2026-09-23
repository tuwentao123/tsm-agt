"""Governed process environment construction for runtime execution."""

from __future__ import annotations

import os
from collections.abc import Mapping

from tsm_agt.ports import ProcessEnvironmentPolicy

_BASE_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
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
        host = dict(host_environment or os.environ)
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
