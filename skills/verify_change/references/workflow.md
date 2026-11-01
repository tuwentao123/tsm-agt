# Select evidence, execute, observe

1. Resolve the current outcome from the user's request and relevant follow-ups. State what
   observable result would distinguish success from the reported defect or prior behavior.
2. Read project instructions, manifests, existing tests, and their invocation wrappers as needed.
   Reuse an applicable project entry point. A test file may require an environment prepared by
   another runner; do not assume it is independently executable.
3. Select the smallest sufficient check. For logic, exercise the affected behavior; for a repair,
   reproduce the failing scenario or its nearest executable equivalent. For UI work, use an actual
   browser or existing interaction path when available, and relevant state/event tests as needed.
   Configuration changes need evidence from parsing/loading and affected consumers. Ordinary prose
   changes need accuracy, links, or rendering checks appropriate to the claim, not mandatory tests.
4. Complete the change, execute the selected check after the last relevant edit, then read output
   and terminal status. A background process still running is not a passed check; observe its
   existing status/output rather than launching duplicate runs.
5. If it fails, use actual output to distinguish source defects, wrong invocation, and environment
   constraints. Repair within the authorized scope and rerun checks affected by the repair. Do not
   broaden into unrelated failures, install dependencies without an established need, or repeat
   passing checks without a change that invalidates them.
6. If blocked, establish the blocker with observations and perform independent feasible checks.
   Report checked behavior, observed outcomes, and remaining coverage precisely. Do not present a
   proposed command, successful write, import check, or unrelated passing suite as proof of the
   requested interactive behavior.

The primary model chooses and executes this workflow using ordinary available tools. Loading
these instructions does not run any command, require a particular runner, or override permissions.
