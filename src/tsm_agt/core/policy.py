"""Non-replaceable minimum policy enforced before every tool invocation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from tsm_agt.ports import ToolCall, ToolResultAuthority, ToolRisk, ToolSpec
from .trust import ProjectTrustLevel

#: ``git`` subcommands that only bring a public repository in. They create no
#: remote state and cannot rewrite local history, so a project trust that
#: already permits execution does not need a per-command approval for them.
_READ_ONLY_GIT_FETCH_SUBCOMMANDS = frozenset({"clone", "fetch", "ls-remote"})

#: Arguments that let a caller inject configuration or run a program through an
#: otherwise read-only fetch. They keep the command on the approval path.
_GIT_FETCH_INJECTION_FLAGS = frozenset({
    "-c", "--config", "-u", "--upload-pack", "--template",
    "--reference", "--separate-git-dir", "--server-option",
})

#: Shell executables. The sandbox never authorizes one, so the policy refuses it
#: outright instead of parking the turn behind an approval a human cannot make
#: useful: the model must re-issue the program and its arguments directly.
_DENIED_SHELL_EXECUTABLES = frozenset({
    "ash", "bash", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh",
    "cmd", "powershell", "pwsh",
})

#: ``gh api`` flags that turn a read into a write. ``gh api`` defaults to GET but
#: switches to POST as soon as a field is supplied, so those stay gated.
_GH_API_WRITE_FLAGS = frozenset({
    "-X", "--method", "-f", "--field", "-F", "--raw-field", "--input",
})

#: Two-word ``gh`` subcommands that only read. ``gh api`` is handled separately
#: because its safety depends on the endpoint and flags.
_GH_READ_ONLY_SUBCOMMANDS = frozenset({("auth", "status")})
_GH_READ_ONLY_VIEWS = frozenset({"view", "list", "diff", "status"})
_GH_SEARCH_KINDS = frozenset({"code", "commits", "issues", "prs", "repos"})


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

    _POLICY_VERSION = 2
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
            "r4": "reserved-no-active-classification",
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
        elif (
            call.name == "core.run_command"
            and self._executable_name(call) in _DENIED_SHELL_EXECUTABLES
        ):
            action = PolicyAction.DENY
            reason = (
                "a shell executable is denied; pass the program and its arguments "
                "directly as the argv array"
            )
        elif call.name == "core.run_command" and self.is_external_source_read(call):
            # Reading another project's source changes no remote state: a git
            # fetch brings it in, ``gh api`` reads it where it lives. A project
            # trust that already permits execution therefore needs no
            # per-command approval for it; writes still fall through.
            action = PolicyAction.ALLOW
            reason = (
                "read-only source retrieval is allowed under "
                f"project trust {project_trust.value}"
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
            # R4 remains a serialized compatibility value, but the current
            # policy has no active R4 operations. External adapters that still
            # declare R4 are conservatively mapped to R3 exact approval.
            action = PolicyAction.REQUIRE_APPROVAL
            effective_risk = ToolRisk.R3
            reason = "legacy R4 declaration requires explicit R3 approval"
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
            return (
                ToolRisk.R3 if spec.risk is ToolRisk.R4 else spec.risk,
                spec.requires_network,
                (("legacy R4 declaration mapped to R3 approval",)
                 if spec.risk is ToolRisk.R4 else ()),
            )
        classified, network, factors = cls._classify_command(call)
        ranks = {risk: index for index, risk in enumerate(ToolRisk)}
        declared = ToolRisk.R3 if spec.risk is ToolRisk.R4 else spec.risk
        return (
            classified if ranks[classified] > ranks[declared] else declared,
            spec.requires_network or network,
            factors,
        )

    @staticmethod
    def _executable_name(call: ToolCall) -> str:
        raw_argv = call.arguments.get("argv")
        if not isinstance(raw_argv, (list, tuple)) or not raw_argv:
            return ""
        name = Path(str(raw_argv[0])).name.lower()
        for suffix in (".exe", ".cmd", ".bat"):
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return name

    @classmethod
    def is_external_source_read(cls, call: ToolCall) -> bool:
        """Whether one invocation reads provenance material outside the workspace.

        This is the single owner of that judgement. Policy uses it to skip
        approval, and the Runtime uses it to label the execution's result
        authority, so completion can ask "did this turn read something from
        outside?" without knowing a single command or tool name.
        """

        return cls._is_read_only_git_fetch(call) or cls._is_read_only_gh(call)

    @classmethod
    def retrieves_external_source(cls, call: ToolCall) -> bool:
        """Whether one invocation actually obtained content from a remote source.

        Narrower than :meth:`is_external_source_read`: a ref listing or a search
        tells you a repository exists, not what its source says, so only the
        calls that bring or read content count. Completion uses this to decide
        whether a declared external dependency was really satisfied.
        """

        if cls._is_read_only_gh(call):
            argv = [str(item) for item in (call.arguments.get("argv") or ())]
            return len(argv) > 1 and argv[1].lower() == "api"
        return cls._is_read_only_git_fetch(call) and cls._git_fetch_subcommand(
            call
        ) in {"clone", "fetch"}

    @classmethod
    def _git_fetch_subcommand(cls, call: ToolCall) -> str:
        raw_argv = call.arguments.get("argv")
        if not isinstance(raw_argv, (list, tuple)):
            return ""
        argv = tuple(str(item) for item in raw_argv)
        valued_options = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
        index = 1
        while index < len(argv):
            item = argv[index]
            if not item.startswith("-"):
                return item.lower()
            index += 2 if item in valued_options else 1
        return ""

    @classmethod
    def execution_result_authority(
        cls, spec: ToolSpec, call: ToolCall,
    ) -> ToolResultAuthority:
        """The authority to record for one invocation.

        ``core.run_command`` is a general command runner, so its spec says
        ``PROCESS_FACT``. An invocation that only fetched public material
        establishes a fact from *outside* the workspace, and saying so here is
        what lets completion stay generic.
        """

        if spec.name == "core.run_command" and cls.is_external_source_read(call):
            return ToolResultAuthority.EXTERNAL_SERVICE
        return spec.result_authority

    @classmethod
    def _is_read_only_git_fetch(cls, call: ToolCall) -> bool:
        """Whether ``argv`` is a ``git`` command that only reads a public remote.

        Only the subcommands that bring a repository *in* are recognised: they
        create no remote state and cannot rewrite local history. Everything else
        (push, reset, clean, publish) keeps its normal approval path.
        """

        raw_argv = call.arguments.get("argv")
        if not isinstance(raw_argv, (list, tuple)) or len(raw_argv) < 2:
            return False
        argv = tuple(str(item) for item in raw_argv)
        executable = Path(argv[0]).name.lower()
        for suffix in (".exe", ".cmd", ".bat"):
            if executable.endswith(suffix):
                executable = executable[: -len(suffix)]
                break
        if executable != "git":
            return False
        if any(
            item in _GIT_FETCH_INJECTION_FLAGS
            or item.split("=", 1)[0] in _GIT_FETCH_INJECTION_FLAGS
            for item in argv[1:]
        ):
            return False
        return cls._git_fetch_subcommand(call) in _READ_ONLY_GIT_FETCH_SUBCOMMANDS

    @staticmethod
    def _is_read_only_gh(call: ToolCall) -> bool:
        """Whether ``argv`` is a ``gh`` invocation that only reads GitHub.

        ``gh api`` with a raw-Accept header is the direct way to read one
        file without cloning the repository, so it is the important case here.
        """

        raw_argv = call.arguments.get("argv")
        if not isinstance(raw_argv, (list, tuple)) or len(raw_argv) < 2:
            return False
        argv = tuple(str(item) for item in raw_argv)
        executable = Path(argv[0]).name.lower()
        for suffix in (".exe", ".cmd", ".bat"):
            if executable.endswith(suffix):
                executable = executable[: -len(suffix)]
                break
        if executable != "gh":
            return False
        subcommand = argv[1].lower()
        if subcommand == "api":
            if len(argv) < 3:
                return False
            for item in argv[2:]:
                if item.split("=", 1)[0] in _GH_API_WRITE_FLAGS:
                    return False
                if item.startswith("-X") and item != "-X":
                    return False
            # The GraphQL endpoint is always a POST, whatever the flags say.
            return not any(
                item.lower() == "graphql"
                for item in argv[2:]
                if not item.startswith("-")
            )
        if (subcommand, argv[2].lower() if len(argv) > 2 else "") in _GH_READ_ONLY_SUBCOMMANDS:
            return True
        if subcommand == "search":
            return len(argv) > 2 and argv[2].lower() in _GH_SEARCH_KINDS
        return (
            subcommand in {"repo", "pr", "issue", "run", "release"}
            and len(argv) > 2
            and argv[2].lower() in _GH_READ_ONLY_VIEWS
        )

    @staticmethod
    def _classify_command(
        call: ToolCall,
    ) -> tuple[ToolRisk, bool, tuple[str, ...]]:
        raw_argv = call.arguments.get("argv")
        if not isinstance(raw_argv, (list, tuple)) or not raw_argv:
            return ToolRisk.R3, False, ("command argv is missing or invalid",)
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
            network = bool(
                (executable == "git" and "push" in tokens)
                or (
                    executable in {"npm", "pnpm", "yarn", "cargo"}
                    and "publish" in tokens
                )
                or (
                    executable in {"gradle", "gradlew", "mvn"}
                    and any(
                        "publish" in item or item == "uploadarchives"
                        for item in lowered
                    )
                )
                or any(
                    marker in item
                    for item in lowered
                    for marker in ("production", "--prod", "prod-deploy")
                )
            )
            return ToolRisk.R3, network, tuple(factors)

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
