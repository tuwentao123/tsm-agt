# Approval resume diagnostics and evaluation

The deterministic regression `tests/test_approval_context_continuity.py` compares
the request following a simulated successful first operation with and without an
approval suspension. The model is scripted; this proves request continuity and
separate approval behavior, not real-model task completion. The no-approval
fixture is read-only, and neither case modifies real files.

## Explicit local request capture

The OpenAI-compatible adapter accepts an optional programmatic
`request_diagnostics=RequestDiagnosticCapture(acknowledge_sensitive_data=True)`.
No capture is enabled by default and it is not exposed through the web UI or
environment variables. Enable it only in a local diagnostic process whose
requests and operator are explicitly authorized. The capture is in memory only,
holds one snapshot, expires after 60 seconds by default (maximum 300), and
`take()` returns and removes it. Call `clear()` when finished. Requests over the
configured maximum are not captured. HTTP authorization headers and provider
API keys are not included intentionally. The snapshot contains the internal
messages and tool schemas and the provider-transformed request payload.

**Redaction is best effort and not a security guarantee.** Code, personal data,
unrecognized credentials and information in tool results may remain. Do not log,
print, persist, transmit, or use real confidential data in captured snapshots;
prefer a disposable synthetic workspace. Captured data belongs only to the
caller, and any copies made by the caller cannot be expired by this facility.
Errors in capture do not intentionally block requests; capture is not sent to
the model. Only the OpenAI-compatible adapter supports this opt-in hook.

## Real-model experiment

Use an explicitly disposable workspace and the existing operator-run script:

```sh
python scripts/evaluate_execution_plan_behavior.py \
  --workspace /path/to/disposable-workspace \
  --allow-workspace-mutations \
  --prompt 'Implement the two specified changes and run the relevant checks.' \
  --expect-file-change first.txt --expect-file-change second.txt \
  --expect-mutation --expect-verification
```

The command may call a remote model and may modify the selected workspace. It
does not auto-approve operations. The report separates attempted tools, returned
results, approval requests, and byte-level file changes. A changed hash is not
proof of correctness or completion; inspect the files, verification output and
final response. Run multiple independent trials to estimate behavior. Configure
the fixture and explicit expected changes for each case; do not use TODO as a
completion requirement. A waiting approval or timeout is not a completed trial.
