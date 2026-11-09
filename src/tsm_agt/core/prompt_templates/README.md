# Prompt templates

The loader in `../prompt.py` reads the six template files in its explicit
`_PROMPT_FILES` order. Markers identify independently gated segments; they are
not sent to the model. Keep tool-specific guidance behind its capability gate.

- `base.md`: safety and request execution.
- `communication.md`: response and execution discipline.
- `workspace.md`: workspace, edit, command and Git capabilities.
- `external.md`: external source, document and runtime fact routing.
- `tool_coordination.md`: independent read batches, evidence reuse, working memory and plans.
- `coordination.md`: reply style.

File boundaries do not define a provider cache boundary. Preserve assembled
content and ordering unless an intentional prompt revision is needed. Do not
insert request IDs, timestamps or other changing data in stable sections.
Dynamic runtime and conversation context is assembled separately. The receipt's
stable-prefix and ordered-toolset hashes describe internal assembly, not a
provider cache hit or its exact wire-level prefix. Provider usage is the source
of truth for cache effectiveness.

Read batching is model guidance, not an execution gate or a minimum batch size.
Known independent observations belong in one assistant message; dependent work
waits for the preceding result. Location/count questions can end at a sufficient
search result. A single read remains valid when that is all the task needs.
Use `python -m scripts.evaluate_read_batching --help` for opt-in real-model
checks in synthetic read-only workspaces; compare the same profile across runs.
