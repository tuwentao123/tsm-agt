# Judge-layer gating audit — tsm_agt

Scope: does each "judge layer" actually BLOCK/FAIL work, or only annotate?
No code was changed. Note: `rg` is not installed in this environment, so the
`grep` tool provider fails (`ripgrep provider failure`); shell `grep` was used.

**Production composition** (used by `tsm_agt agent`): `cli.py:531-537`
`_agent()` → `compose_openai_compatible_engineering_application_from_env`
(`bootstrap/composition.py:1186`) → `compose_openai_compatible_engineering_application`
(`bootstrap/composition.py:998`). A second real composition
(`compose_openai_compatible_readonly_application`, `composition.py:809`) registers
the same layers. The `legacy` exploration profile is opt-in; default is `balanced`
(`bootstrap/exploration_configuration.py:53`), which is what builds the exploration
coordinator (`core/kernel.py:940-953`).

---

### 1. `semantic.action_classified`

- **(a) computed by:** `Kernel._classify_semantic_action` in `core/kernel_input.py:478-504`. Classifier: `RuleBasedSemanticActionClassifier` (`adapters/rule_based_semantic_action/classifier.py:19`, `classify` at `:70`).
- **(b) decision values:** `SemanticActionFamily` enum, `ports/semantic_action.py:13-26` (`INSPECT_PROJECT_STRUCTURE, SEARCH_CONCEPT, SEARCH_DEFINITION, SEARCH_REFERENCES, READ_ARTIFACT, INSPECT_CONFIGURATION, MUTATE_WORKSPACE, EXECUTE_VERIFICATION, EXECUTE_COMMAND, CONTROL_PROCESS, REQUEST_CLARIFICATION, MANAGE_AGENT_STATE, USE_TOOL`), carried on `SemanticAction` (`ports/semantic_action.py:39-72`).
- **(c) block tool?** No. `_classify_semantic_action` never raises on classifier failure: `except Exception as error:` → appends `semantic.action_classification_failed` and `return None` (`kernel_input.py:486-495`). The event is append-only (`kernel_input.py:496-503`). No tool call is failed/skipped/rewritten here.
- **(d) block turn / completion gap?** No. Return value is only read by later layers; no suspend/abort/gap.
- **(e) retry?** No.
- **(f)** Annotation-only. It appends `semantic.action_classified` and returns the `SemanticAction`. Its output *feeds* real gates in other layers (e.g. `RuleBasedReadHitsPolicy._is_search`, `RuleBasedExplorationBudgetPolicy._is_exploration`, `PlanGuard.evaluate`, `StopOrPivotSignals.semantic_family`) but is not itself a gate.
- **wiring:** `composition.py:1077` (engineering) / `:909` (readonly): `registry.register(SemanticActionClassifierPort, RuleBasedSemanticActionClassifier())`. **Optional / None-tolerant**: `kernel_input.py:481-483` and `composition.py:409-411` (`semantic_classifiers[0] if semantic_classifiers else None`).

---

### 2. `evidence_relation.evaluated`

- **(a) computed by:** `PolicyMixin._evaluate_exploration_before` (`core/kernel_policy.py:246-306`) → `ExplorationCoordinator.before_call` (`core/exploration_coordinator.py:75-122`). Relation policy is `RuleBasedEvidenceRelationPolicy.assess` (`adapters/evidence_guided/strategies.py:110-144`); rejection policy `BoundedRejectionLoopPolicy.decide` (`strategies.py:163-190`); relation clues from `BuiltinEvidenceRelationProvider.discover` (`strategies.py:58-91`).
- **(b) decision values:** event `action` is `ExplorationCoordinatorAction` (`core/exploration_coordinator.py:20-27`): `EXECUTE, RETURN_FEEDBACK, REWRITE, ALLOW_BOUNDED_PROBE, STOP_ROUTE, ASK_USER`. Event `assessment` is `EvidenceRelationAssessmentKind` (`ports/evidence_guided_exploration.py:85-89`): `SUPPORTED, UNSUPPORTED, NEEDS_BOUNDED_PROBE`. (Sibling `rejection_loop.decision_made` logs `RejectionRecoveryAction`, `ports/evidence_guided_exploration.py:100-106`.)
- **(c) block tool?** **YES.** Call site `core/turn_tool_batch.py:208-253`:
  ```
  if exploration_decision.action in {
      ExplorationCoordinatorAction.RETURN_FEEDBACK,
      ExplorationCoordinatorAction.STOP_ROUTE,
      ExplorationCoordinatorAction.ASK_USER,
  }:
      result = ToolResult(call.call_id, False, error_code="EXPLORATION_ROUTE_REJECTED", ...)
  ```
  the tool is never invoked, a synthetic failure `ToolResult` is appended (`:213-237`), and the loop `continue`s (`:253`). Additionally `REWRITE`/`ALLOW_BOUNDED_PROBE` can **replace** the call: `core/turn_tool_batch.py:194-196` `if exploration_decision.call != call: call = exploration_decision.call`; probe timeout is also narrowed at `:203-207`.
- **(d) block turn / completion gap?** Not terminal, and no `CompletionGap` is created. It skips/rewrites individual calls only.
- **(e) retry?** No forced retry. The synthetic result enters the ordinary loop (next model round).
- **wiring:** `composition.py:1087` `_register_exploration_profile(registry, budget.profile)`; inside `:559-568` (provider `:559-560`, relation policy `:562-563`, rejection `:565`, outcome `:566-568`). **Optional as a group / None-tolerant**: coordinator only built when all three policies exist (`core/kernel.py:947-952`); call site guarded by `kernel._exploration_coordinator is not None and scope_probe.scope_hash` (`turn_tool_batch.py:175-178`). Caveat: the coordinator call is *not* wrapped in try/except, so a raising relation/rejection policy propagates out of the turn.

---

### 3. `exploration_budget.action_evaluated`

- **(a) computed by:** `PolicyMixin._evaluate_exploration_budget_before` (`core/kernel_policy.py:397-447`); adapter `RuleBasedExplorationBudgetPolicy.before_call` (`adapters/rule_based_exploration_budget/policy.py:62-98`).
- **(b) decision values:** `ExplorationBudgetAction` (`ports/exploration_budget_policy.py:16-19`): `CONTINUE, FOCUS, WRAP_UP`.
- **(c) block tool?** **YES, both FOCUS and WRAP_UP.**
  - `turn_tool_batch.py:334` `if budget_decision.action is ExplorationBudgetAction.FOCUS:` → `:377` `if not budget_decision.focus_allows_call:` builds synthetic `EXPLORATION_FOCUS_REQUIRED` (`:378-397`), appends it (`:398-402`), and `continue`s (`:418`).
  - `turn_tool_batch.py:419` `if budget_decision.action is ExplorationBudgetAction.WRAP_UP:` → `:460` `elif pivot_decision.action is StopOrPivotAction.CHANGE_METHOD:` builds `EXPLORATION_CHANGE_METHOD_REQUIRED` and `continue`s (`:461-500`); the `else` at `:501` builds `EXPLORATION_BUDGET_WRAP_UP`, sets `state.budget_wrap_up = True` (`:529`), and `continue`s (`:541`).
- **(d) block turn / completion gap?** `WRAP_UP` sets `budget_wrap_up=True`, which makes `state.wrap_up=True` (`core/turn_phases.py:151-155`) and forces `allow_tool_calls=False` on the next model call (`turn_phases.py:446`) with a "do not call tools" instruction (`turn_phases.py:211-217`). It does not suspend/abort the turn and creates no `CompletionGap` (`kernel.py:1855` sets `gaps = ()`).
- **(e) retry?** No provider/model retry. It indirectly forces one finalization model round because the loop proceeds to `_prepare_model_call` after the batch drains (`turn_phases.py:86-89`).
- **wiring:** `composition.py:1088-1090` `registry.register(ExplorationBudgetPolicyPort, RuleBasedExplorationBudgetPolicy(**budget.policy_arguments()))`. **Optional / None-tolerant**: `kernel_policy.py:402-406` returns `ExplorationBudgetDecision(CONTINUE, "policy_disabled")`; on exception `:412-422` returns `CONTINUE, "policy_failed"` (event `exploration_budget.policy_failed`).

---

### 4. `read_hits.action_evaluated`

- **(a) computed by:** `PolicyMixin._evaluate_read_hits_before` (`core/kernel_policy.py:56-99`); adapter `RuleBasedReadHitsPolicy.before_call` (`adapters/rule_based_read_hits/policy.py:65-91`).
- **(b) decision values:** `ReadHitsAction` (`ports/read_hits_policy.py:15-17`): `ALLOW, REQUIRE_READ`.
- **(c) block tool?** **YES.** `turn_tool_batch.py:546` `if read_hits_decision.action is ReadHitsAction.REQUIRE_READ:` builds synthetic failure `READ_HITS_REQUIRED` (`:568-587`), appends it (`:588-592`), pops the pending call (`:597`) and `continue`s (`:608`). Trigger in adapter: `policy.py:83-87` `if self._is_search(call, semantic_action): return ReadHitsDecision(ReadHitsAction.REQUIRE_READ, "same_question_has_bounded_hits", ...)`.
- **(d) block turn / completion gap?** No (only the single call). No suspension/gap.
- **(e) retry?** No forced retry; synthetic result flows into the next normal model round.
- **(f)** Not annotation-only: `ALLOW` is an annotation, but `REQUIRE_READ` is a real gate.
- **wiring:** `composition.py:1079` `registry.register(ReadHitsPolicyPort, RuleBasedReadHitsPolicy())`. **Optional / None-tolerant**: `kernel_policy.py:61-62` returns `ALLOW, "policy_disabled"`; on exception `:77` returns `ALLOW, "policy_failed"`. `after_result` only mutates checkpoint state (`:101-136`, event `read_hits.state_changed`) and is annotation-only.

---

### 5. `artifact_read.action_evaluated`

- **(a) computed by:** `PolicyMixin._evaluate_artifact_read_before` (`core/kernel_policy.py:172-212`); adapter `RuleBasedArtifactReadPolicy.before_call` (`adapters/rule_based_artifact_read/policy.py:42-92`).
- **(b) decision values:** `ArtifactReadAction` (`ports/artifact_read_policy.py:14-16`): `ALLOW, REQUIRE_REUSE`.
- **(c) block tool?** **YES.** `turn_tool_batch.py:617` `if artifact_decision.action is ArtifactReadAction.REQUIRE_REUSE:` builds synthetic failure `ARTIFACT_ALREADY_READ` (`:618-635`), appends it (`:636-640`), pops the call (`:645`) and `continue`s (`:655`). Trigger: `policy.py:84-88` (`exact_empty_repeat or covered`).
- **(d) block turn / completion gap?** No. **(e) retry?** No forced retry.
- **wiring:** `composition.py:1080` `registry.register(ArtifactReadPolicyPort, RuleBasedArtifactReadPolicy())`. **Optional / None-tolerant**: `kernel_policy.py:177-178` (`ALLOW, "policy_disabled"`), `:191` (`ALLOW, "policy_failed"`). `after_result` (`:214-244`) is annotation/checkpoint-state only.

---

### 6. `plan.action_evaluated`

- **(a) computed by:** inline at `core/turn_tool_batch.py:656-664` → `PlanGuard.evaluate` (`core/plan_guard.py:134-175`); event emitted at `turn_tool_batch.py:665-686`.
- **(b) decision values:** no enum. `PlanGuardDecision` (`core/plan_guard.py:67-76`) with booleans/ints: `should_stop: bool`, `semantic_repeat: bool`, `goal_slice.plan_finished`, `consecutive_no_progress`, `action_signature`, `semantic_signature`, `semantic_family`, `relevant_state_hash`.
- **(c) block tool?** Not directly by the event, but **the decision object is consumed as a gate**. At `turn_tool_batch.py:695-709` its fields become `StopOrPivotSignals`: `plan_should_stop=decision.should_stop` (`:699`) and `plan_finished=decision.goal_slice.plan_finished` (`:700`), then `_decide_stop_or_pivot`. If that returns `terminal` (`:720`), **all pending calls are cancelled** with synthetic `CANCELLED_BY_RUNTIME_POLICY` (`:725-741`) and the batch `break`s (`:782`). `should_stop` is computed as `goal_slice.plan_finished or count >= self.no_progress_limit` (`plan_guard.py:173`). So the plan layer is a *signal producer* for a real gate, not pure annotation.
- **(d) block turn / completion gap?** The terminal path sets `state.no_progress_stop = True` (`:768`), emits `plan.no_progress_stopped` (`:769-778`), and disables tools next round via `wrap_up` (`turn_phases.py:151-155`). No suspension; no `CompletionGap`.
- **(e) retry?** No forced model/provider retry; forces a wrap-up model round indirectly (tools disabled on the next `_prepare_model_call`).
- **(f)** Not annotation-only: the event is append-only, but its computed decision gates via `_decide_stop_or_pivot`. It also mutates state: `action_progress` is replaced (`turn_tool_batch.py:687-694`).
- **wiring:** **not** registered in `composition.py` (not a replaceable port). It is hardcoded core: `Kernel` sets `state.plan_guard = PlanGuard()` at `core/kernel.py:3458` (default `no_progress_limit=3`, `plan_guard.py:81`). **Required** (always present).

---

### 7. `stop_or_pivot.decision_made`

- **(a) computed by:** `PolicyMixin._decide_stop_or_pivot` (`core/kernel_policy.py:338-395`); adapter `RuleBasedStopOrPivotPolicy.decide` (`adapters/rule_based_stop_or_pivot/policy.py:36-105`).
- **(b) decision values:** `StopOrPivotAction` (`ports/stop_or_pivot_policy.py:14-21`): `CONTINUE, READ_HITS, NARROW_SCOPE, CHANGE_METHOD, ASK_USER, SUMMARIZE_WITH_EVIDENCE, STOP_NO_PROGRESS`, plus `terminal: bool` (`StopOrPivotDecision`, `:69-76`).
- **(c) block tool?** **YES.** Consumed at four sites:
  - `turn_tool_batch.py:449` `if pivot_decision.action is StopOrPivotAction.CONTINUE:` (after WRAP_UP signal) → only resets `low_value_streak`, no block.
  - `:460` `elif ... CHANGE_METHOD:` → synthetic `EXPLORATION_CHANGE_METHOD_REQUIRED`, `continue` (`:461-500`).
  - `:501` `else:` → synthetic `EXPLORATION_BUDGET_WRAP_UP` + `budget_wrap_up=True`, `continue` (`:502-541`).
  - `:720` `if pivot_decision.terminal:` → cancels **all** pending calls with `CANCELLED_BY_RUNTIME_POLICY` (`:725-741`) and `break`s (`:782`).
  Terminal actions from the adapter are `SUMMARIZE_WITH_EVIDENCE` and `STOP_NO_PROGRESS` (`policy.py:52-61`, `:97-101`, and `:69-74`); the no-policy fallback also returns them terminal (`kernel_policy.py:344-363`).
- **(d) block turn / completion gap?** The `terminal` path ends the tool batch and sets `no_progress_stop` (`:768`), which disables tools for the wrap-up model call (`turn_phases.py:151-155`, `:446`). It does not suspend/abort. No `CompletionGap`.
- **(e) retry?** No forced retry; may force one wrap-up model round.
- **wiring:** `composition.py:1092` `registry.register(StopOrPivotPolicyPort, RuleBasedStopOrPivotPolicy())`. **Optional / None-tolerant**: `kernel_policy.py:343-367` has a full `if policy is None:` legacy branch (derives `SUMMARIZE_WITH_EVIDENCE`/`STOP_NO_PROGRESS`/`CONTINUE`), and `:372-382` returns `CONTINUE, "policy_failed"` on exception.

---

### 8. `exploration_outcome.evaluated`

- **(a) computed by:** `PolicyMixin._update_exploration_after` (`core/kernel_policy.py:308-336`) → `ExplorationCoordinator.after_result` (`core/exploration_coordinator.py:124-155`) → `RuleBasedExplorationOutcomePolicy.evaluate` (`adapters/evidence_guided/strategies.py:209-237`).
- **(b) decision values:** `ExplorationOutcomeAction` (`ports/evidence_guided_exploration.py:153-159`): `CONTINUE, CHANGE_METHOD, STOP_ROUTE, WRAP_UP, ASK_USER`.
- **(c) block tool?** Not the current call (evaluated after execution), but **it disables subsequent tool calls** in the turn: `turn_tool_batch.py:980-985`:
  ```
  if outcome.action in {
      ExplorationOutcomeAction.STOP_ROUTE,
      ExplorationOutcomeAction.WRAP_UP,
      ExplorationOutcomeAction.ASK_USER,
  }:
      state.budget_wrap_up = True
  ```
  `budget_wrap_up` → `state.wrap_up` (`turn_phases.py:151-155`) → `allow_tool_calls=False` (`turn_phases.py:446`). No synthetic failure for the current call.
- **(d) block turn / completion gap?** Forces wrap-up (no more tools), no suspend/abort, no `CompletionGap`. It also rewrites the result meta with `exploration_outcome` (`turn_tool_batch.py:972-979`) and advances checkpoint state (`evidence_relation_state`/`rejection_loop_state`/`exploration_outcome_state`, `:963-971`).
- **(e) retry?** No forced retry; forces one wrap-up model round.
- **wiring:** `composition.py:1087` → `_register_exploration_profile` `:566-568` (`ExplorationOutcomePolicyPort, RuleBasedExplorationOutcomePolicy()`). **Optional / None-tolerant**: coordinator is `None` unless relation+rejection+outcome policies all exist (`core/kernel.py:947-952`); call site guarded by `if kernel._exploration_coordinator is not None:` (`turn_tool_batch.py:956`).

---

### 9. `policy.evaluated` (contrast: real permission check)

- **(a) computed by:** `CoreToolPolicy.evaluate` (`core/policy.py:76-129`); event emitted in `Kernel._invoke_agent_tool` (`core/kernel_tool.py:1128-1143`).
- **(b) decision values:** `PolicyAction` (`core/policy.py:17-20`): `allow, require_approval, deny` (serialized in `decision.to_data()`).
- **(c) block tool?** **YES, both non-allow values.**
  - `kernel_tool.py:1177` `if decision.requires_approval:` builds an `ApprovalRequest`, persists it, and `raise ApprovalRequired(request)` (`:1177-1249`) — the tool does **not** run.
  - `kernel_tool.py:1251` `if not decision.allowed:` returns a synthetic `PERMISSION_DENIED` `ToolResult` (`:1252-1283`) — the tool does **not** run.
  - An extra approval path (external read root) raises before this too (`kernel_tool.py:1147-1176`).
- **(d) block turn / completion gap?** **YES for approval**: `ApprovalRequired` propagates to `turn_tool_batch.py:880-896` and returns `AgentTurnSuspended` (turn suspends until approval). Denial does not suspend. No `CompletionGap`.
- **(e) retry?** No automatic retry. Approval suspends and waits for a human decision; denial returns a terminal-ish error result the model may react to.
- **wiring:** not an adapter. Hardcoded: `Kernel.__init__` sets `self._tool_policy = CoreToolPolicy()` (`core/kernel.py:928`). **Required.**

---

## Where layer outputs are CONSUMED as gates (`.action` compared before a tool executes)

| # | Layer | Consuming call site | Effect |
|---|-------|---------------------|--------|
| 1 | evidence_relation | `core/turn_tool_batch.py:194-196` | replace call (`exploration_decision.call != call`) |
| 2 | evidence_relation | `core/turn_tool_batch.py:208-253` | synthetic `EXPLORATION_ROUTE_REJECTED` + skip (`RETURN_FEEDBACK`, `STOP_ROUTE`, `ASK_USER`) |
| 3 | exploration_budget | `core/turn_tool_batch.py:334` + `:377` | synthetic `EXPLORATION_FOCUS_REQUIRED` + skip when `FOCUS` and `not focus_allows_call` |
| 4 | exploration_budget | `core/turn_tool_batch.py:419`, `:449`, `:460`, `:501` | `WRAP_UP` → `CONTINUE` resets streak; `CHANGE_METHOD`/other → synthetic failure + skip; wrap-up flag |
| 5 | stop_or_pivot | `core/turn_tool_batch.py:449/460/501` and `:720` | synthetic failures; `terminal` → cancel all pending calls + break |
| 6 | read_hits | `core/turn_tool_batch.py:546` | `REQUIRE_READ` → synthetic `READ_HITS_REQUIRED` + skip |
| 7 | artifact_read | `core/turn_tool_batch.py:617` | `REQUIRE_REUSE` → synthetic `ARTIFACT_ALREADY_READ` + skip |
| 8 | plan | `core/turn_tool_batch.py:695-709` → `:720` | `should_stop`/`plan_finished` signals drive `stop_or_pivot.terminal` |
| 9 | exploration_outcome | `core/turn_tool_batch.py:980-985` | `STOP_ROUTE/WRAP_UP/ASK_USER` → `budget_wrap_up = True` (disables tools next round) |
| 10 | policy | `core/kernel_tool.py:1177` and `:1251` | approval suspension / `PERMISSION_DENIED`, tool not run |

(`core/flow.py:926` and `:1396-1419` only *project* `policy.evaluated` and `plan.action_evaluated` into flow nodes; they are not gates.)

## Summary table

| layer | can block tool | can block turn | can retry | wired by default |
|---|---|---|---|---|
| `semantic.action_classified` | no | no | no | yes — `composition.py:1077` (readonly `:909`); optional |
| `evidence_relation.evaluated` | **yes** (`turn_tool_batch.py:208-253`, rewrite `:194`) | no (per-call only) | no | yes — `composition.py:1087` → `:562-563`; optional group |
| `exploration_budget.action_evaluated` | **yes** (`:334/377`, `:419/460/501`) | forces wrap-up, no abort | no | yes — `composition.py:1088-1090`; optional |
| `read_hits.action_evaluated` | **yes** (`:546`) | no | no | yes — `composition.py:1079`; optional |
| `artifact_read.action_evaluated` | **yes** (`:617`) | no | no | yes — `composition.py:1080`; optional |
| `plan.action_evaluated` | via `stop_or_pivot` terminal (`:695-720`, `:782`) | forces wrap-up, no abort | no | yes — hardcoded `core/kernel.py:3458`; required |
| `stop_or_pivot.decision_made` | **yes** (`:460/501`, cancel all at `:720-741`) | forces wrap-up, no abort | no | yes — `composition.py:1092`; optional |
| `exploration_outcome.evaluated` | **yes, subsequent calls** (`:980-985` → `turn_phases.py:446`) | forces wrap-up, no abort | no | yes — `composition.py:1087` → `:566-568`; optional group |
| `policy.evaluated` | **yes** (`kernel_tool.py:1177`, `:1251`) | **yes — suspends on approval** (`turn_tool_batch.py:882-896`) | no | yes — hardcoded `core/kernel.py:928`; required |

- **Completion gaps:** none of the nine layers creates a `CompletionGap`; `_evaluate_completion_readiness` starts from `gaps = ()` (`core/kernel.py:1855`). No layer adds a required gap that blocks finalization.
- **Retries:** none of the nine layers triggers a model/provider retry. Retry machinery is `RuleBasedModelRecoveryPolicy` + `ResilientModelProvider` (`composition.py:869-878`), independent of these decisions. Synthetic-failure and wrap-up decisions only feed the normal loop (`turn_phases.py:86-89` → `_prepare_model_call`), i.e. one more ordinary model round, not a retry attempt.
