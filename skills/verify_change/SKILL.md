---
name: verify-change
description: Use when implementing or repairing code, configuration, integration, or UI behavior; select and execute relevant checks before reporting completion. Includes short follow-ups continuing that work. Excludes analysis-only requests and ordinary prose edits.
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

Use this Skill when a requested implementation, configuration, build, test, or UI behavior change needs evidence before reporting completion. Do not use it merely to answer, analyze, or edit prose without a behavior claim.

## Decide from the actual change

0. Load `references/workflow.md` with `skill.load(resource="references/workflow.md")` before selecting or running a check. Establish the executable entry and working directory from observed project instructions, runner configuration, wrappers, or environment facts. A test framework imported by a file does not establish that a similarly named executable exists.
1. Read the current user request, the final relevant change, trusted project instructions (in this runtime, `.agent/projectInstructions.md` when present), and existing project entry points. Inspect available tools before assuming a browser, test runner, service, or MCP capability exists.
2. Choose the smallest verification that can support the behavior being claimed. Reuse project commands and existing tests when they fit. Do not choose a command because its executable name looks like a test.
3. For UI behavior, use an available browser, DevTools, Playwright, MCP, or existing end-to-end path when it can exercise the requested flow. Start a local service only when the project and available tools support it. A source read or successful build alone does not prove an interactive UI flow.
4. Inspect actual output from a failed, partial, or running operation before diagnosing it. Repair and rerun only the checks made stale or relevant by the repair.
5. In the final response, separate what was changed, what was actually checked, the coverage that check provides, and any remaining limitation. Never convert a write receipt, an unrelated zero exit code, or an unexecuted plan into a verification claim.

Once the
last relevant edit succeeds, carry out that check with the available execution tools and inspect
its actual result. A promise to run it or instructions for the user do not complete this workflow.
Choose according to the behavior changed, not just a file extension: a Python file can contain
JavaScript, generated templates, or configuration that needs a different test path.

If an attempted command did not start, the check remains unexecuted. Continue discovering the
project's actual invocation through available reads or environment observations; do not guess a
replacement executable, treat one missing executable as proof that all verification is impossible,
or finish by asking the user to run an available check. Establish any remaining blocker with evidence.

## When this follows a diagnosis

If the `diagnosis` field from `core.working_memory_read` shows an investigation in `repair` mode, prioritize replaying the reported failing scenario or its nearest executable equivalent. A build, import check, or unrelated passing test may complement that replay but does not replace it. After a passed replay, update the diagnosis with the replay evidence before claiming the repair worked. If it fails or cannot run, record that result and keep the diagnosis open or blocked rather than marking it repaired.
