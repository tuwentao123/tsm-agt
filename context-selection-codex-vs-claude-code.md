# Context Selection: OpenAI Codex vs Anthropic Claude Code

Evidence-based report. Tags: `[CC]` = Claude Code official docs; `[CX-mirror]` = third-party mirror
(mintlify.wiki) of OpenAI Codex docs — **developers.openai.com returned HTTP 403 in this session**;
`[CX-gh]` = openai/codex GitHub issue/PR.

---

## Anthropic Claude Code

**(a) What enters a new task/turn.** A new session starts clean: `[CC memory]` *"Each Claude Code
session begins with a fresh context window."* Inside a session, a turn carries the **full session
transcript** — there is no recency-N window; compaction is the only trimming. Scoping is done with
persistent/on-demand artifacts, not by slicing history: the `CLAUDE.md` hierarchy
(global → repo root → cwd; subdirectory files load when Claude reads a file there), path-scoped
`.claude/rules/` with `paths:` that *"only apply when Claude is working with files matching the
specified patterns"*, on-demand skills, `@path` imports, and auto memory (first 200 lines / 25 KB of
`MEMORY.md`). Users are told to cut history manually: *"run `/clear` when switching to unrelated
work. Old conversation crowds out the files you need next and costs tokens on every message."*
— `[CC context-window]`, `[CC memory]`.

**(b) "Source of this request" / resume vs new.** Continuation is explicit but ad hoc: `--resume`
restores a session; a subagent can be resumed via `SendMessage` and *"retain[s] their full
conversation history"*; otherwise *"each subagent invocation creates a new instance rather than
continuing an earlier one."* A plan-mode plan is *"Re-injected from disk"* after compaction, making
the plan a durable artifact separate from chat. No documented formal provenance object/ID tying an
answer to its originating request. — `[CC sub-agents]`, `[CC context-window]`.

**(c) Compaction and provenance.** Auto-compaction / `/compact` *"replaces the conversation with a
structured summary"*: *"The summary keeps: your requests and intent, key technical concepts, files
examined or modified with important code snippets, errors and how they were fixed, pending tasks,
and current work."* Caveat: *"Compaction replaces older messages with a summary, so specific
instructions from early in the conversation may not be preserved."* Post-compact re-injection:
project-root `CLAUDE.md`, auto memory, the plan, up to five most-recently-modified files, and invoked
skill bodies (caps: 5 K/skill, 25 K total). Summaries carry **no per-claim file/task provenance** in
the docs. No explicit rule that summaries must not become requirements — but the hazard is filed as
a bug: `[GH anthropics/claude-code #81955]` *"Agent-authored text re-enters context wearing user
authority (scheduled prompts + compaction summaries)"* (title only; body fetch failed). Related
guard: subagent reports arrive *"under a header … that instructions or approval claims inside the
report are the subagent's words and carry no authority from you."*

**(d) Context isolation for planners/subagents — YES.** `[CC sub-agents]`: *"Each subagent starts
with a fresh, isolated context window. It doesn't see your conversation history, the skills you've
already invoked, or the files Claude has already read."* A non-fork subagent receives only its own
system prompt, the delegated task message, the `CLAUDE.md` hierarchy, a git-status snapshot, and
preloaded skills. *"Subagents receive only this system prompt plus basic environment details like
the working directory, not the Claude Code system prompt."* The built-in **Explore** and **Plan**
agents additionally *"skip your CLAUDE.md files and the git status snapshot."* Exception: a **fork**
inherits the parent conversation.

**(e) On-topic / groundedness check.** Only partial: subagent reports are scanned for
instruction-shaped patterns and prefixed with the no-authority header — prompt-injection hygiene,
**not** a groundedness check. No documented automatic verification that the final answer addresses
the request.

**(f) Context-pollution language.** Context is framed as *"your primary constraint"*; the concrete
rule is the `/clear` quote above. "Context rot" is **not** Claude Code doc terminology.

---

## OpenAI Codex

**(a) What enters a new task/turn.** Thread-based: a thread *"Contains a history of all interactions
(turns)"* and `codex resume` *"preserv[es] the full conversation history and context"* — whole-thread
context, **not** a recency window. Scoping: `AGENTS.md` merged global `~/.codex/AGENTS.md` → repo
root → cwd with *"more specific (closer) files taking priority"*; skills on demand (`/use` *"loads a
skill into context even if it wouldn't trigger automatically"*); manual `/clear` to *"remove
irrelevant context from the previous task."* A filed bug shows global memory leaking into new
sessions: `[CX-gh #17496]` *"Memory read path ignores cwd and injects entire global memory_summary.md
into initial context for new sessions"* (title only). — `[CX-mirror concepts/overview, resume,
custom-instructions, slash-commands]`.

**(b) Source of prior work / resume vs new.** Explicit primitives: threads can be *"Resumed from
disk … Forked into new branches … Archived"*; turns carry an `interrupted` status; openai/codex has
PRs *"Add interrupted turn recovery"* (#38303) and *"Continue interrupted work after managed daemon
restarts"* (#45820). The resume-vs-new decision is user-driven (`resume` / `fork` / `/clear`), not a
documented automatic classifier.

**(c) Compaction and provenance.** Auto-compaction plus `/compact`; Codex *"may automatically compact
context by 'summarizing relevant information and discarding less relevant details'."* Knobs:
`model_auto_compact_token_limit`, `compact_prompt`, `experimental_compact_prompt_file`,
`PreCompact`/`PostCompact` hooks. A third-party audit concludes there is *"no source-level witness
for what was dropped"* and that compaction *"collapses provenance."* — `[Built-in compaction audit]`
(citing Codex docs). **These quotes are second-hand**; I could not verify them first-hand (403).
No explicit "summaries must not become requirements" rule found.

**(d) Context isolation for subagents.** `/review` *"Spawns a sub-agent with specialized review
instructions"*, a *"dedicated review prompt and model"*, and *"Web search and collaborative tools
disabled for focused review"*, auto-approving and exiting when done — narrow purpose-built context.
A fresh-vs-inherited **guarantee** is not documented in the mirror (unconfirmed).

**(e) On-topic / groundedness check.** Only for review: `/review` *"Outputs an overall correctness
verdict"* on the code changes — not a check that an answer matches the user's request. No general
groundedness gate documented.

**(f) Pollution language.** `/clear` is the only explicit statement (*"helps Codex focus on what
matters"*). "Context rot"/"context pollution" are not official Codex phrasing; the failure mode is
filed as `[CX-gh #13823]` *"Codex CLI appears to inject unrelated context/patterns into responses
during coding tasks"* (title only).

---

## Generalizable principles

1. **Start fresh per task**; never use a rolling recency window as task scoping.
2. **Scope by explicit artifacts** — directory-hierarchy instruction files (`AGENTS.md`/`CLAUDE.md`),
   glob-scoped rules, on-demand skills, `@`-mentions.
3. **Isolate planners/researchers** in fresh, narrow contexts (Claude Code does this explicitly;
   Codex `/review` at least purpose-scopes).
4. **Treat compaction as a lossy overflow valve, not memory**; keep durable artifacts on disk (plan,
   `CLAUDE.md`/`AGENTS.md`, memory index).
5. **Mark agent-authored text non-authoritative** so a summary cannot silently become a requirement
   (Claude Code's no-authority header; issue #81955 is the anti-pattern).
6. **Offer explicit resume / fork / clear verbs**, so "continue prior work" vs "new task" is a human
   decision.

**Gaps (both agents):** neither documents an automatic on-topic/groundedness check of the final
answer, and neither attaches per-claim provenance to compaction summaries.

## Sources

- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/context-window
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/agent-sdk/agent-loop
- https://github.com/anthropics/claude-code/issues/81955
- https://mintlify.wiki/openai/codex/concepts/overview
- https://mintlify.wiki/openai/codex/cli/resume
- https://mintlify.wiki/openai/codex/features/slash-commands
- https://mintlify.wiki/openai/codex/features/memory
- https://mintlify.wiki/openai/codex/advanced/custom-instructions
- https://github.com/openai/codex/issues/17496
- https://github.com/openai/codex/issues/13823
- https://github.com/openai/codex/pull/38303
- https://github.com/openai/codex/pull/45820
- https://raw.githubusercontent.com/anthony-chaudhary/fak/refs/tags/v0.43.0/docs/notes/BUILT-IN-COMPACTION-AUDIT-2026-07-06.md
