"""Non-replaceable minimum policy enforced before every tool invocation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from tsm_agt.ports import ToolCall, ToolRisk, ToolSpec
from .trust import ProjectTrustLevel


class PolicyAction(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision_id: str
    action: PolicyAction
    effective_risk: ToolRisk
    reason: str
    payload_hash: str
    requires_network: bool = False
    risk_factors: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.action is PolicyAction.ALLOW

    @property
    def requires_approval(self) -> bool:
        return self.action is PolicyAction.REQUIRE_APPROVAL

    def to_data(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "action": self.action.value,
            "effective_risk": self.effective_risk.value,
            "reason": self.reason,
            "payload_hash": self.payload_hash,
            "requires_network": self.requires_network,
            "risk_factors": list(self.risk_factors),
        }


class CoreToolPolicy:
    """Fail closed; risky calls pause until the kernel resolves approval."""

    _POLICY_VERSION = 1
    _INTERNAL_STATE_TOOLS = frozenset({
        "core.working_memory_update", "core.task_spec_update",
    })

    @classmethod
    def snapshot_data(cls) -> dict[str, Any]:
        """Public policy semantics included in Effective Configuration hashes."""
        return {
            "policy_id": "core-tool-policy",
            "version": cls._POLICY_VERSION,
            "r0_read_only": "allow",
            "r0_mutating": "deny",
            "r0_runtime_internal_state": "allow-listed",
            "r1_r3": "require_approval",
            "r4": "deny",
            "project_trust_enforced": True,
            "payload_hash_bound": True,
        }

    def evaluate(
        self, spec: ToolSpec, call: ToolCall,
        project_trust: ProjectTrustLevel = ProjectTrustLevel.TRUSTED_FULL,
    ) -> PolicyDecision:
        effective_risk, requires_network, factors = self._effective_risk(spec, call)
        payload_hash = self.payload_hash(spec, call, project_trust)
        detail = "; ".join(factors)
        if (
            call.name == "core.run_command"
            and not project_trust.allows_project_execution
        ):
            action = PolicyAction.DENY
            reason = (
                f"project trust {project_trust.value} does not allow repository "
                "commands; grant TRUSTED_BUILD or TRUSTED_FULL first"
            )
        elif effective_risk is ToolRisk.R0 and spec.is_read_only:
            action = PolicyAction.ALLOW
            reason = "R0 read-only workspace tool is allowed"
        elif (
            effective_risk is ToolRisk.R0
            and spec.is_internal_state
            and call.name in self._INTERNAL_STATE_TOOLS
            and not requires_network
            and spec.data_transmission == "none"
        ):
            action = PolicyAction.ALLOW
            reason = "allow-listed R0 Runtime internal-state update is allowed"
        elif effective_risk is ToolRisk.R0:
            action = PolicyAction.DENY
            reason = "R0 tools must be declared read-only"
        elif effective_risk in {ToolRisk.R1, ToolRisk.R2, ToolRisk.R3}:
            action = PolicyAction.REQUIRE_APPROVAL
            reason = f"{effective_risk.value} tool requires explicit approval"
            if detail:
                reason += f": {detail}"
        else:
            action = PolicyAction.DENY
            reason = "R4 tools are denied by the default core policy"
            if detail:
                reason += f": {detail}"
        return PolicyDecision(
            decision_id=f"policy-{uuid4().hex}",
            action=action,
            effective_risk=effective_risk,
            reason=reason,
            payload_hash=payload_hash,
            requires_network=requires_network,
            risk_factors=factors,
        )

    @classmethod
    def _effective_risk(
        cls, spec: ToolSpec, call: ToolCall
    ) -> tuple[ToolRisk, bool, tuple[str, ...]]:
        if call.name != "core.run_command":
            return spec.risk, spec.requires_network, ()
        classified, network, factors = cls._classify_command(call)
        ranks = {risk: index for index, risk in enumerate(ToolRisk)}
        return (
            classified if ranks[classified] > ranks[spec.risk] else spec.risk,
            spec.requires_network or network,
            factors,
        )

    @staticmethod
    def _classify_command(
        call: ToolCall,
    ) -> tuple[ToolRisk, bool, tuple[str, ...]]:
        raw_argv = call.arguments.get("argv")
        if not isinstance(raw_argv, (list, tuple)) or not raw_argv:
            return ToolRisk.R4, False, ("command argv is missing or invalid",)
        argv = tuple(str(item) for item in raw_argv)
        executable = Path(argv[0]).name.lower()
        for suffix in (".exe", ".cmd", ".bat"):
            if executable.endswith(suffix):
                executable = executable[:-len(suffix)]
                break
        lowered = tuple(item.lower() for item in argv[1:])
        tokens = set(lowered)
        factors: list[str] = []

        shells = {
            "sh", "bash", "zsh", "fish", "dash", "ksh",
            "cmd", "cmd.exe", "powershell", "pwsh",
        }
        destructive_programs = {
            "rm", "rmdir", "dd", "mkfs", "shutdown", "reboot",
            "sudo", "su", "kill", "killall",
        }
        if executable in shells:
            factors.append("shell can interpret arbitrary command text")
        if (
            executable in {"node", "ruby"} and any(item in {"-e", "--eval"} for item in lowered)
        ) or (
            (executable == "python" or executable.startswith("python3"))
            and any(item in {"-c", "--command"} for item in lowered)
        ):
            factors.append("inline interpreter code cannot be safely classified")
        if executable in destructive_programs:
            factors.append(f"destructive or privileged executable: {executable}")
        if executable == "git" and (
            "push" in tokens
            or ("reset" in tokens and "--hard" in tokens)
            or ("clean" in tokens and any(flag in tokens for flag in {"-f", "-fd", "-df", "-fx"}))
            or ("branch" in tokens and "-d" in tokens)
        ):
            factors.append("Git publish or destructive history/worktree operation")
        if executable in {"npm", "pnpm", "yarn", "cargo"} and "publish" in tokens:
            factors.append("package publication changes an external registry")
        if executable in {"gradle", "gradlew", "mvn"} and any(
            "publish" in item or item == "uploadarchives" for item in lowered
        ):
            factors.append("build publication or release operation")
        if executable == "adb" and any(
            item in {"uninstall", "reboot", "root", "remount"} for item in lowered
        ):
            factors.append("destructive or privileged device operation")
        if any(
            marker in item
            for item in lowered
            for marker in ("production", "--prod", "prod-deploy")
        ):
            factors.append("production-targeting argument")
        if factors:
            return ToolRisk.R4, any("publish" in factor.lower() for factor in factors), tuple(factors)

        network = False
        install_programs = {"pip", "pip3", "gem", "bundle"}
        package_programs = {"npm", "pnpm", "yarn", "bun", "cargo"}
        if executable in install_programs and any(
            item in {"install", "add", "update", "upgrade", "uninstall"} for item in lowered
        ):
            network = "uninstall" not in tokens
            factors.append("package environment modification")
        if executable in package_programs and any(
            item in {"install", "add", "update", "upgrade", "up", "ci", "i", "remove"} for item in lowered
        ):
            network = not any(item in {"remove", "uninstall"} for item in lowered)
            factors.append("dependency environment modification may run lifecycle scripts")
        if executable == "yarn" and not lowered:
            network = True
            factors.append("dependency installation may download and run lifecycle scripts")
        if executable == "python" or executable.startswith("python3"):
            if len(lowered) >= 3 and lowered[0:2] == ("-m", "pip") and lowered[2] in {
                "install", "download", "uninstall",
            }:
                network = lowered[2] != "uninstall"
                factors.append("Python package environment modification")
        if executable == "git" and any(
            item in {"clone", "fetch", "pull", "submodule"} for item in lowered
        ):
            network = True
            factors.append("Git operation may contact a remote and modify repository metadata")
        if executable in {"curl", "wget", "ssh", "scp", "npx"}:
            network = True
            factors.append(f"network-capable executable: {executable}")
        if executable == "adb" and any(item in {"install", "push"} for item in lowered):
            factors.append("device state modification")
        if any(item.startswith(("http://", "https://", "ssh://")) for item in lowered):
            network = True
            factors.append("remote URL argument")
        if factors:
            return ToolRisk.R3, network, tuple(dict.fromkeys(factors))
        return ToolRisk.R2, False, ("local build, test, or development command",)

    @staticmethod
    def payload_hash(
        spec: ToolSpec, call: ToolCall,
        project_trust: ProjectTrustLevel = ProjectTrustLevel.TRUSTED_FULL,
    ) -> str:
        effective_risk, dynamic_network, risk_factors = CoreToolPolicy._effective_risk(
            spec, call
        )
        payload: dict[str, Any] = {
            "policy_version": CoreToolPolicy._POLICY_VERSION,
            "tool": call.name,
            "arguments": dict(call.arguments),
            "declared_risk": spec.risk.value,
            "read_only": spec.is_read_only,
            "internal_state": spec.is_internal_state,
            "requires_network": spec.requires_network,
            "data_transmission": spec.data_transmission,
            "rollback": spec.rollback,
            "effective_risk": effective_risk.value,
            "effective_requires_network": dynamic_network,
            "risk_factors": list(risk_factors),
            "project_trust": project_trust.value,
        }
        try:
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("tool policy payload must be JSON serializable") from error
        return hashlib.sha256(encoded).hexdigest()
