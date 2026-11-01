---
name: diagnose-failure
description: Investigate a reported failure, regression, error, missing behavior, or ineffective prior repair before changing code.
kind: plan
tags:
  - diagnose
  - diagnosis
  - debug
  - repair
  - 排查
  - 定位
  - 报错
  - 异常
  - 没生效
safety_level: read_only
approval_required: false
---

# Diagnose a failure

Use this Skill when the user asks why something failed, did not take effect, regressed, or still fails after a repair. It applies both to diagnosis-only requests and to requests that authorize diagnosis followed by a repair.

1. Call `core.working_memory_read` when it is available. Record only concise, user-inspectable state in the `diagnosis` field of `core.working_memory_update`: the observed symptom, expected behavior when known, a reproduction or observed path, at most three falsifiable hypotheses, evidence references, and the next discriminating probe. Preserve the existing scratchpad fields and revision. Do not record chain-of-thought, raw logs, source bodies, or secrets.
2. Treat a user report and a prior assistant conclusion as leads, not proof. Trace the relevant input, state producer, update, and consumer. Prefer the smallest observation that distinguishes plausible causes over broad repository exploration.
3. Do not edit files for a diagnosis-only request. State the supported causal chain, rejected alternatives, and any remaining uncertainty using actual evidence.
4. For a diagnosis-and-repair request, edit only after a hypothesis is supported by evidence. If a repair fails, record the failed replay and reassess the premise; do not add unrelated patches to make a symptom disappear temporarily.
5. After a repair, use the relevant verification capability and replay the original user-reported scenario. If the `verify-change` Skill is available, load it for selecting that check. Mark the investigation `repaired` only when that replay passes with an evidence reference; otherwise leave it open or blocked and report the verified boundary.
