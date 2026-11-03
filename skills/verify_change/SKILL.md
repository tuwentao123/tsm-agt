---
name: verify-change
description: Use for behavior changes to code, UI, configuration, prompts, Skills, runtime templates, tests, or automation; discover and execute relevant checks after the last edit. Includes short follow-ups authorizing implementation. Excludes analysis-only requests and ordinary prose not consumed as runtime instructions.
kind: plan
tags:
  - verify
  - validation
  - test
  - build
  - browser
  - code
safety_level: read_only
approval_required: false
---

# Verify a change

Short approvals of an implementation plan include verification. Code, UI, configuration, tests,
automation and program-consumed Markdown prompts/Skills are behavior inputs. Ordinary prose and
analysis-only requests do not require runtime tests. Respect explicit user execution restrictions.

## Decide from the actual change

1. **Discover before editing.** Read the project's README, trusted instructions (such as
   `.agent/projectInstructions.md` when present), manifests or runner wrappers and relevant tests
   with available read tools. Identify the observable outcome, exact executable/arguments and cwd.
   Do not choose a runner from a filename or an imported framework. No project-instructions file
   is required. Use available environment observations when the invocation is not documented.
2. **Implement, then execute.** After the last relevant edit, invoke the smallest sufficient check
   with ordinary execution tools and read its terminal output. A write receipt, source inspection,
   loading this Skill, a pre-edit pass or an unrelated zero exit is not verification.
3. **Recover, then observe.** If the command did not start, verification has not run. Return to the
   README/wrappers and environment facts, correct the invocation and execute the available check.
   One missing executable does not show that the project cannot be checked. Do not stop at that
   first lookup failure or hand an available check to the user. Diagnose actual output, repair
   relevant failures and rerun only affected checks. Establish remaining blockers with evidence.
4. **Report evidence.** Describe the change, executed check, observed result and coverage gaps.
   Do not claim an interactive flow works from compilation alone. For UI, exercise the affected
   events/interactions; for prompts/Skills, check their actual loader/contracts. Claims about agent
   behavior additionally need representative model trajectories. Inspect snapshot changes before
   updating pins; a new hash alone is not behavior evidence.

This complete execution workflow does not require a second Skill load. For further check-selection
guidance, load `references/workflow.md` with
`skill.load(name="verify-change", resource="references/workflow.md")`.

## When this follows a diagnosis

If the `diagnosis` field from `core.working_memory_read` shows an investigation in `repair` mode, prioritize replaying the reported failing scenario or its nearest executable equivalent. A build, import check, or unrelated passing test may complement that replay but does not replace it. After a passed replay, update the diagnosis with the replay evidence before claiming the repair worked. If it fails or cannot run, record that result and keep the diagnosis open or blocked rather than marking it repaired.
