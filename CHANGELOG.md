# Changelog

## Unreleased — Reasoning traces capture the model's own thinking + human feedback

Reasoning traces recorded per chat turn now use the agent's **actual reasoning** as each step's `thought`, instead of a rationale templated from the tool inputs. The Anthropic-backed agents enable extended thinking, capture the `thinking` blocks, and feed them into the trace via the result collector.

### Features

- **Extended thinking captured for reasoning traces.** The two raw-Anthropic agents (`anthropic-tools`, `claude-agent-sdk`) now pass `thinking={"type": "enabled", "budget_tokens": …}` on every `messages.create`/`messages.stream` call (`max_tokens` is bumped by the budget so the answer keeps its headroom). After each iteration the `thinking` content blocks are extracted (`_extract_thinking`) and handed to the collector via `record_thinking()`. Thinking is captured from the final message only — it never leaks into the visible `text_delta` stream. (`templates/backend/agents/anthropic_tools/agent.py.j2`, `templates/backend/agents/claude_agent_sdk/agent.py.j2`)
- **PydanticAI captures thinking too, via a post-hoc path.** PydanticAI runs its whole tool loop inside `agent.run()`, so the pending-thought mechanism can't apply. Instead it enables Anthropic thinking through model settings (`anthropic_thinking`, passed as a plain dict so no hard import is needed) and reconstructs the trace afterwards from `result.new_messages()` — pairing each `ThinkingPart` with the `ToolCallPart`s in the same `ModelResponse`, then matching those onto the collected tool calls by name + order (correctly skipping uncollected calls like `get_schema`). Entirely best-effort: if the installed pydantic-ai doesn't expose thinking parts, the templated rationale stands. (`templates/backend/agents/pydanticai/agent.py.j2`)
- **Collector carries reasoning into trace steps.** `CypherResultCollector` gained `record_thinking()` / `drain_thinking()` and now stamps the most-recent thought onto each `collect_tool_call` record (`"thought"`). (`templates/backend/shared/context_graph_client.py.j2`)
- **`_record_chat_reasoning_trace` prefers the captured thought.** Each tool step's `thought` is now `call["thought"]` (the model's words); a tool-less answer uses the joined `reasoning_steps`. `_public_tool_rationale()` is retained as the fallback for frameworks that don't expose thinking (the other six) and for turns where thinking is disabled. The "private reasoning is never stored" comment is gone — storing the model's reasoning is now the point. (`templates/backend/shared/routes.py.j2`)
- **Interleaved thinking → distinct per-step trace thoughts.** Standard extended thinking only reasons once, up front, so multi-tool turns ended up with the *same* thought repeated on every reasoning-trace step (or, before carry-forward, the templated rationale). All three Anthropic-backed agents now enable interleaved thinking (`anthropic-beta: interleaved-thinking-2025-05-14`; `anthropic_betas` for PydanticAI) so the model reasons *before each tool call* — each trace step gets its own thought. PydanticAI's `_capture_run_thinking` also now carries the last thought forward across thought-less turns as a safety net. (`templates/backend/agents/{claude_agent_sdk,anthropic_tools,pydanticai}/agent.py.j2`)
- **Thinking streamed on its own channel + rendered distinctly in chat.** The raw-Anthropic streaming loops now emit `thinking_delta` SSE events (new `CypherResultCollector.emit_thinking_delta`) for reasoning, separate from `text_delta` (the answer). `ChatInterface` accumulates it into `streamingThinking` and renders a dimmed, labeled **Reasoning** block *above* the tool timeline and answer (instead of blending reasoning into the answer text at the bottom); the finished message keeps the full reasoning under "Show reasoning" via an explicit `message.thinking` field. (`templates/backend/shared/context_graph_client.py.j2`, `templates/frontend/components/ChatInterface.tsx.j2`, `templates/backend/shared/routes.py.j2`)
- **Opt-out + tuning.** `ANTHROPIC_THINKING=0` disables capture (falls back to the templated rationale); `ANTHROPIC_THINKING_BUDGET` tunes the token budget (floored at 1024). The `anthropic` dependency floor for these two frameworks is raised to `>=0.49` so the `thinking` parameter is guaranteed available. (`config.py`)
- **Tool steps now show the actual Cypher that ran.** `execute_cypher()` reports its tool-call inputs as `{"query": …, "parameters": …}` instead of the bound parameters alone — domain tools bind their query internally, so the old inputs were often just `{"domain": …}`, hiding the query that actually executed. The query now flows through every consumer: the chat tool-call timeline renders it as a `Cypher` code block (with `parameters` listed separately), and each reasoning-trace step's `action` includes it. `_public_tool_rationale()` reads `inputs["parameters"]` for its fallback prose (the verbose query is already in the action, so it isn't repeated) and stays backward-compatible with flat inputs dicts. (`templates/backend/shared/context_graph_client.py.j2`, `templates/frontend/components/ChatInterface.tsx.j2`, `templates/backend/shared/routes.py.j2`)
- **Reasoning-trace detail opens in a modal.** Clicking a trace in the Traces panel previously expanded a cramped bottom-docked box (`maxH 50%`) inside the narrow right column. It now opens a centered, scrollable Chakra v3 `Dialog` (modal) — the step `thought`/`action`/`observation` get room to breathe and the now-longer `action` (with its Cypher) wraps cleanly in a mono block. (`templates/frontend/components/ReasoningTracePanel.tsx.j2`)
- **Chat reasoning is split into blocks around tool calls (no more merged thinking).** With interleaved thinking the model reasons *before each tool call*, but the chat UI merged every `thinking_delta` into one block rendered above a separate tool timeline — losing the think→tool→think→tool sequence. The streaming state is now an ordered `ReasoningItem[]` (`steps`): a `thinking_delta` appends to the open reasoning block, or starts a **new** block if a tool ran since the last reasoning; `tool_start`/`tool_end` push/complete tool items in place. A single `ReasoningTimeline` renders thinking blocks and tool calls in chronological order (the in-flight reasoning block shows a spinner), replacing the old separate `streamingThinking` blob + `ToolCallTimeline`. Finished messages persist the ordered `steps`; legacy persisted messages (`toolCalls`/`thinking`) still render via a fallback. Parallel tool calls (one reasoning block, then several tools) naturally share the single block. (`templates/frontend/components/ChatInterface.tsx.j2`)
- **Reasoning traces no longer repeat the thought across parallel tool calls.** When the model fires several tool calls in one turn, the captured reasoning is stamped on each call (`collect_tool_call` reuses the pending thought), so the trace showed the *same* thought on every step. `_record_chat_reasoning_trace` now records the reasoning once and marks the parallel siblings ("Parallel tool call — continues the reasoning above."), while each step keeps its own distinct `action`. (`templates/backend/shared/routes.py.j2`)
- **Human-in-the-loop feedback on reasoning traces.** The Traces panel modal now carries a **Human feedback** section: a 👍/👎 verdict, toggleable issue tags (`Correct`, `Wrong tool`, `Bad query`, `Hallucinated`, `Incomplete`, …), a free-text guidance note ("what should the agent do next time?"), an optional reviewer name, and a per-step note under each reasoning step. Reviewed traces get a **Reviewed** badge in the list. Feedback persists in a new **backend-agnostic store** (`app/feedback.py`) as an append-only `.context-graph/trace_feedback.jsonl` log — the same sidecar dir the import pipeline uses — folded last-write-wins on read, so editing just re-saves. It lives *beside* the graph rather than in it because neither the NAMS REST surface nor the neo4j-agent-memory reasoning API exposes post-hoc trace metadata, so the feature works identically on both backends. Three new routes: `POST /traces/{trace_id}/feedback` (save), `GET /traces/feedback` (export the signal-carrying records newest-first — the dataset for curating agent guidelines / eval sets), and `GET /traces` now attaches any stored feedback inline to each trace. (`templates/backend/shared/feedback.py.j2` (new), `templates/backend/shared/routes.py.j2`, `templates/frontend/components/ReasoningTracePanel.tsx.j2`, `renderer.py`)

### Testing

- **`TestReasoningTraceThinkingCapture`** (`tests/test_generated_project.py`) — asserts the two raw-Anthropic agents enable thinking, extract it, and record it on the collector; that PydanticAI enables `anthropic_thinking` and wires `_capture_run_thinking` (pairing `ThinkingPart`s with tool calls); that the collector exposes `record_thinking`/`drain_thinking` and stamps `thought` onto tool calls; and that routes prefer the captured thought. The name+order matching in `_capture_run_thinking` was also validated out-of-band against synthetic Anthropic message shapes (incl. the uncollected-`get_schema` gap).
- **`test_chat_step_falls_back_to_rationale_without_thinking`** + an assertion on `test_chat_records_native_reasoning_trace` (`tests/test_routes_integration.py`) — pin that a captured `thought` becomes the step thought, and that a missing one falls back to the inputs-derived rationale.
- **`test_execute_cypher_captures_query_in_tool_inputs`** + **`test_tool_rationale_summarizes_parameters_not_query`** (`tests/test_generated_project.py`) pin the `{query, parameters}` inputs shape and the rationale's parameter-summary; **`TestReasoningTraceModal`** + **`TestToolTimelineShowsCypher`** (`tests/test_frontend_logic.py`) pin the modal dialog and that the chat timeline reads the query off `tc.inputs`.
- **`TestInterleavedReasoningSteps`** (`tests/test_frontend_logic.py`) ports the streaming step-builder to Python and asserts thinking splits into separate blocks around tools (and that consecutive deltas of one block still merge); **`TestChatInterleavesThinkingTemplate`** pins the template's append-or-new rule, the ordered `streamingSteps` state, and the single `ReasoningTimeline`. `test_chat_interface_renders_thinking_channel` (`tests/test_generated_project.py`) was updated to the ordered-steps model.
- **`test_parallel_tools_share_one_thinking_block`** (`tests/test_frontend_logic.py`) confirms parallel tool calls render under one reasoning block in chat; **`test_parallel_tool_calls_dont_repeat_thought`** (`tests/test_routes_integration.py`) confirms the trace records the shared reasoning once and marks the sibling while keeping each step's distinct action.
- **`TestTraceFeedback`** (`tests/test_routes_integration.py`) — mounts the generated app and round-trips feedback: submit → inline on `GET /traces` → exported by `GET /traces/feedback`, plus last-write-wins and "empty record carries no signal, excluded from export" cases (on both NAMS and bolt scaffolds). **`TestTraceFeedbackUI`** (`tests/test_frontend_logic.py`) pins the panel's POST target, rating/tag/note/per-step controls, the Reviewed badge, and a **frontend↔backend contract check** that every field the panel sends is declared on `TraceFeedbackRequest`/`StepFeedback`. `backend/app/feedback.py` was added to `TestGeneratedPythonFiles` for compile-checking.

## v0.13.1 — v0.13.0 feedback report triage (unreleased)

Addresses the May 20, 2026 v0.13.0 feedback report. The report mixed verified issues with claims that don't match the current codebase; each claim was verified before scoping work. This release closes every real issue, makes the generated `app.models` module load-bearing, and adds regression tests so the v0.12.0/v0.13.0 fixes can't silently come back.

### Bug Fixes

- **`create-context-graph --dry-run` no longer demands a NAMS API key.** The non-interactive path validated `MEMORY_API_KEY` before reaching the `--dry-run` branch, so users couldn't preview a scaffold without first signing up. The credential gate is now scoped to non-dry-run flows; `--dry-run` skips it entirely. (`cli.py:392`)
- **Dead `template_id` parameter removed from `list_documents_nams`.** The NAMS branch of `GET /documents` already raises HTTP 501 when `template_id` is supplied, so the parameter could never reach the function meaningfully. Signature is now `list_documents_nams(skip, limit)`. (`templates/backend/shared/memory_adapter.py.j2`, `templates/backend/shared/routes.py.j2`)
- **DocumentBrowser entity badges use a stable composite key.** The mentioned-entities map was the last `key={\`${e.name}-${i}\`}` (index-tainted) site in the frontend; switched to `key={\`${selectedDoc.document.title}-${e.name}\`}` so badges don't collide when the user navigates back to the same document. (`templates/frontend/components/DocumentBrowser.tsx.j2`)

### Code Quality

- **Generated `models.py` uses `Field(...)` for required fields.** Previously, `generate_pydantic_models` emitted `name: str = ...` (bare Ellipsis literal). Valid Pydantic v2, but unfamiliar to contributors and harder to extend with constraints (`Field(..., min_length=1)`). Required fields now emit `name: str = Field(...)`. (`ontology.py:478`)
- **New `GET /schema/models` endpoint wires `app.models` into the runtime.** Returns the JSON Schema for every Pydantic entity model generated from the domain ontology, useful for frontend codegen and OpenAPI clients. Makes the previously-unused `app.models` module load-bearing. (`templates/backend/shared/routes.py.j2`)

### Testing

- **`TestCompositeKeyRegressions`** (`tests/test_frontend_logic.py`) — five new assertions pin the composite-key patterns introduced in v0.13.0 (`ChatInterface` entity/preference/tool-call badges, `DecisionTracePanel` step keys) plus the new `DocumentBrowser` fix.
- **`TestV0131ModelsPolish` and `TestV0131TemplateIdRemoval`** (`tests/test_generated_project.py`) — assert that the generated `models.py` uses `Field(...)` (and never bare `= ...`), that the `/schema/models` endpoint compiles, and that the `list_documents_nams` signature no longer takes `template_id`.
- **Three new Playwright tests** (`templates/frontend/e2e/app.spec.ts.j2`) — watch the browser console for React duplicate-key warnings across a multi-prompt chat sequence, verify the decision trace step list renders without page errors, and verify the document browser entity badges render without page errors.

### Not Changed (and why)

The v0.13.0 feedback report flagged several issues that don't reflect the current code. Documenting here so contributors don't re-litigate:

- **"Backend connectors removed from scaffolded projects" — false.** `templates/backend/connectors/` still exists and `renderer.py:474-514` renders connector modules + `backend/scripts/import_data.py` whenever `--connector` is supplied. Runtime `make import` / `make import-dry-run` / `make import-retry` targets continue to work on every scaffold.
- **"`pyproject.toml.j2` bloats NAMS users with `sentence-transformers`" — false.** The template already branches on backend mode at lines 16-20: NAMS scaffolds pin `neo4j-agent-memory[litellm]`, self-hosted scaffolds pin `neo4j-agent-memory[litellm,sentence-transformers,extraction,fuzzy]`. NAMS users never pull PyTorch.
- **"Restore media / insurance / supply-chain domains" — these never existed.** `git log --all -- src/create_context_graph/domains/*.yaml` shows no history for these labels. v0.13.0's "restored domains" are `legal`, `education`, `cybersecurity`, `government` — total domain count remains 27.
- **"Unused `i` in `ContextGraphView` line 210" — leave as-is.** The unused index is in a fallback `extractNodesAndRels` parser, not a render hot path, and removing it would churn a path that hasn't drifted in months.

## v0.13.0 — v0.12.0 feedback report fixes (unreleased)

Addresses the May 2026 v0.12.0 feedback report: one runtime bug on the `--self-hosted` ingest path, one React state bug in the streaming chat, four `key={i}` re-render hazards, dead/over-fetching code in the document adapter, four restored domains, and documentation for the `ccg-edges` encoding strategy.

### Bug Fixes

- **`_ingest_via_bolt()` now async (scaffolded `import_data.py`).** `app.context_graph_client.get_driver()` returns an `AsyncDriver`, but the generated `_ingest_via_bolt()` was `def` and called `with driver.session() as session:` followed by sync `session.run(...)`. Every self-hosted `make import` / `make import-retry` failed at runtime with a coroutine-not-iterable error. Function is now `async def`, uses `async with driver, driver.session() as session:` to own the driver lifecycle, and `await`s every `session.run(...)`. Both call sites (`main()`, `_retry_deadletter()`) wrap with `asyncio.run(...)` to mirror the NAMS branch.
- **`ChatInterface` "done" handler no longer reads stale streaming state.** The `setMessages` callback in the `done` SSE branch referenced `streamingEntities`/`streamingPreferences` from closure — these are `useState`-backed and could be one render behind the most recent `entities_extracted`/`preferences_detected` event. Accumulators are now `useRef`-backed (`streamingEntitiesRef`, `streamingPreferencesRef`); the `done` handler reads from `.current` and the two-step `setMessages` was collapsed into one.
- **`list_documents_nams()` pushes the `OBJECT` filter to the server.** Previously, the over-fetch buffer (`skip + limit + 50`) could be exhausted by unrelated POLE+O entities, silently dropping valid documents on busy NAMS instances. Now passes `entity_type="OBJECT"` to `search_entities`. Dead `template_id` filter at the same site removed.
- **`useEffect` for `externalInput` now also depends on `loading`.** An "Ask about this" click that landed mid-stream was silently dropped because the effect didn't re-fire when `loading` flipped back to false.

### Breaking Changes

- **`--framework maf` alias removed.** Use `--framework anthropic-tools` instead. Click now rejects `maf` with the standard "Invalid value for '--framework'" error. The `FRAMEWORK_ALIASES` dict and the `resolved_framework` property on `ProjectConfig` are gone — call sites use `config.framework` directly.

### Domains

- **Restored 4 domains as YAML definitions:** `legal`, `education`, `cybersecurity`, `government`. Each ships with the full schema (entity_types, relationships, document_templates, decision_traces, demo_scenarios, agent_tools, system_prompt, visualization) plus a deterministic static-fallback fixture. Domain count: 23 → 27.

### Code Quality

- **`key={i}` replaced with stable identifiers in 4 locations.** `ChatInterface.tsx`: suggested-prompt buttons keyed by prompt text; entity/preference badges keyed by `${type}-${name}-${i}` and `${category}-${preference}-${i}`. `DecisionTracePanel.tsx`: trace steps keyed by `step-${i}-${action.slice(0, 32)}`.

### Documentation

- **`ccg-edges` encoding strategy documented.** New Docusaurus page (`docs/docs/explanation/ccg-edges.md`) explains the fenced YAML block inside entity descriptions, fragility (what happens if descriptions are edited or truncated), the migration playbook once NAMS adds native `add_relationship`, and the parity test that pins the format. Scaffolded README gains a short "How NAMS stores relationships" section linking to the page.
- **Custom domain generation walkthrough.** New Docusaurus page (`docs/docs/how-to/custom-domains.md`) covers `create-context-graph --custom-domain "..."` end-to-end, including the few-shot prompting strategy, the validate-retry loop, and how to convert a generated domain into a permanent contribution.

### Testing

- **Bolt ingest parity test.** New `tests/test_bolt_ingest_parity.py` mirrors `test_nams_ingest_parity.py` — renders the scaffolded template, exec's it against a mock `AsyncDriver`, and asserts the Cypher statement + parameter sequence matches a frozen golden file.
- **AST async-shape check.** `tests/test_generated_project.py` now parses generated `import_data.py` and asserts `_ingest_via_bolt` is `AsyncFunctionDef`, every `session.run` call is inside an `Await`, and both call sites wrap with `asyncio.run`. `backend/scripts/import_data.py` was added to `PYTHON_FILES` so it gets `py_compile`-checked on every scaffold.
- **Streaming ref-accumulation test.** `tests/test_frontend_logic.py` now AST-checks that the `done` handler in `ChatInterface.tsx.j2` reads from `streamingEntitiesRef.current`/`streamingPreferencesRef.current`, not the `useState` values.
- **E2E coverage for new panels.** Playwright tests for `DecisionTracePanel` (tab-switch + step rendering) and `DocumentBrowser` (pagination + markdown detail view).

## v0.12.0 — NAMS-native connector ingest + `ccg-edges` relationship encoding (2026-05-19)

The big shift in v0.12.0 is that connector ingest (`make import` in a scaffolded project) now writes through NAMS REST on NAMS scaffolds — previously the generated `import_data.py` only spoke bolt Cypher, so the SaaS connectors didn't actually work on the default backend. The two ingest paths (CLI demo-fixture seeding and connector-driven imports) now share a single write shape pinned by a contract test.

### New Features

- **`ccg-edges` relationship encoding for NAMS.** NAMS REST has no `add_relationship` endpoint yet, so each entity's outbound edges are now encoded into the source entity's `description` as a fenced ```ccg-edges``` YAML block (deterministically sorted by `type` then `target`). The frontend graph view recognizes this marker and renders edges from it; the agent reads them out naturally as part of the description. A one-shot migration replays them as native edges once NAMS gains `add_relationship` — the seam is `_build_ccg_edges_block()` in both `src/create_context_graph/ingest.py` and `src/create_context_graph/templates/backend/connectors/import_data.py.j2`.
- **Dual-tracked documents on NAMS.** Documents now land as both `long_term.add_entity(name=title, type=OBJECT, description=...)` AND `short_term.add_message(role="document", content=...)`. The long-term entity is the queryable source of truth (matches the bolt `:Document` shape and powers `/documents`); the short-term message is extraction fuel for the NAMS server-side extractor. The document browser now reads from long-term entities and strips the `ccg-edges` / `_pole_type:` markers for a clean preview.
- **Per-connector `BODY_FIELDS` mapping.** Each connector declares `BODY_FIELDS: dict[str, str]` mapping `entity_label → property_name` for the prose body. The ingestor pipes that body through `short_term.add_message` so the NAMS extractor can mine it for secondary entities. Wired up for `Comment.body` (Linear), `Message.content` (Claude AI / ChatGPT / Claude Code), `DecisionThread.content` / `Reply.content` (Google Workspace), and `Document.description` / `Section.description` (local-file). Pure-metadata entities (Person, Project, Label) opt out by omission.
- **Idempotent connector imports.** Generated `import_data.py` now tracks per-connector watermarks in `.context-graph/watermarks.json` so re-runs only fetch deltas. Failed records (one at a time) are appended to `.context-graph/deadletter.jsonl` and the watermark only advances on a clean run. NAMS `add_entity` is trusted to merge by `name` for entity-level idempotency.
- **`--dry-run` and `--retry` modes for `import_data.py`.** `--dry-run` fetches connectors and writes `data/fixtures.json` without touching the memory backend (useful for sanity-checking a large pull before paying the write cost). `--retry` drains `.context-graph/deadletter.jsonl` back through the writer.
- **New Make targets.** `make import-dry-run` (fetch-only, writes fixtures.json), `make import-retry` (drain deadletter). The legacy `make import-and-seed` target collapsed into `make import` — it now both fetches and ingests in one step on either backend.
- **`run_nams_ingest()` extracted as a reusable async function** in `ingest.py`. Takes an already-open `MemoryClient` plus the fixture dict, ontology, optional `body_fields`, and optional `on_event` callback; returns counts and a list of failure records. Used by both the CLI's Rich-progress path and the scaffolded `import_data.py`. Pinned to the same call sequence as the generated importer via the new `tests/test_nams_ingest_parity.py` contract test (~526 LOC, 14 assertions).

### Breaking Changes

- **`make import-and-seed` removed from generated Makefiles.** Replaced by the now-idempotent `make import`, which both fetches from connectors and ingests on either backend. Existing scaffolds with the old target keep working until regenerated; the generated `import_data.py` is the source of truth.
- **`/documents?template_id=...` returns HTTP 501 on NAMS.** Template-based filtering relied on the `MENTIONS` graph edges that NAMS doesn't yet have. The bolt path is unchanged; un-filtered `GET /documents` works on both backends.

### Bug Fixes

- **`ingest_data()` legacy bolt signature restored.** v0.11.0 had changed the signature to take a `ProjectConfig` exclusively, breaking library callers that were on the older `(neo4j_uri, neo4j_username, neo4j_password)` triple. `ingest_data()` now accepts either: a `ProjectConfig` instance OR the legacy three positional bolt args; `_coerce_ingest_config()` synthesizes a bolt `ProjectConfig` from the legacy triple. CLI usage is unaffected.
- **Cypher identifier injection guards on bolt fallback.** `_ingest_with_driver` and `_ingest_with_memory_client` now validate relationship types, source labels, and target labels against `[A-Za-z_][A-Za-z0-9_]*` before string-interpolating them into a Cypher template. Unsafe identifiers log a warning and skip the record instead of executing. Closes the CodeQL `py/code-injection`-shaped finding flagged on the bolt ingest path. (NAMS path was never affected — it never builds Cypher.)
- **Labeled MATCH on bolt relationship ingest.** The bolt connector path was falling back to a label-less `MATCH (a {name: $name})` for relationship endpoints, which would match across labels and silently mis-merge. Now uses `MATCH (a:SourceLabel ...)` / `MATCH (b:TargetLabel ...)` when the connector supplies both labels, with the existing sanitization guard.
- **Deadletter retry on partial-failure imports.** Records that failed during a connector run (rate limit, transient 5xx) are appended to `.context-graph/deadletter.jsonl` with their original payload; `make import-retry` (or `python scripts/import_data.py --retry`) replays them through the same write path. Records whose failure category isn't retryable (e.g. a permanently unsupported entity shape) are logged but kept in the deadletter for inspection rather than dropped silently.

### Documentation

- **`docs/docs/explanation/memory-backends.md`** — replaced the "best-effort B-partial port" framing with the actual hybrid write shape: `ccg-edges` encoding, dual-tracked documents, `BODY_FIELDS` extractor channel. Added a note about the `test_nams_ingest_parity.py` contract test that pins the two ingest paths together.
- **`docs/docs/how-to/use-nams.md`** — rewrote the "Seeding a relationship-rich graph" section to reflect that NAMS scaffolds now do encode relationships (just inside descriptions) rather than dropping them, with a `ccg-edges` example block. Kept the `--self-hosted --demo` recommendation for native-edge workflows (`expand_node`, GDS, arbitrary Cypher).
- **`src/create_context_graph/ingest.py`** — module docstring rewritten end-to-end documenting the new NAMS write shape, the `BODY_FIELDS` extension point, and why the two ingestors are duplicated by design (rendered template vs. runtime CLI consumer).

### Internal / CI

- **New `tests/test_nams_ingest_parity.py` contract test** — drives `run_nams_ingest` and the rendered `import_data.py` against a shared fixture and asserts identical call sequences against a mock `MemoryClient`. Pins entity → ccg-edges → body → document → trace ordering, fenced-block format, body-field routing, and decision-trace shape.
- **Expanded NAMS test coverage.** ~1,500 new test lines across `test_ingest_nams.py`, `test_memory_adapter.py`, `test_nams_ingest_parity.py`, `test_routes_integration.py`, and `test_doc_snippets.py` — covering `ccg-edges` round-trips, dual-tracked document reads, `BODY_FIELDS` per connector, deadletter retry behavior, watermark persistence, the 501 guard on template-filtered `/documents`, and the bolt Cypher identifier sanitizers.

## v0.11.3 — Streaming for CrewAI/Strands + NAMS hardening (2026-05-19)

Rolls up streaming chat for the last two non-streaming frameworks, a much better NAMS failure-mode UX (classified errors surfaced in `/health`, memory-backend auto-detection from `.env`), lighter NAMS dependencies, custom-domain robustness fixes, and silent-failure cleanup across all 8 agent templates.

### New Features

- **Streaming chat for CrewAI and Strands.** Both frameworks now implement `handle_message_stream()`, so the `/chat/stream` SSE endpoint streams text deltas as the model produces them — previously these two frameworks emitted tool events live but text only arrived at the end of the run. CrewAI subscribes to `LLMStreamChunkEvent` on the crew event bus; Strands iterates `agent.stream_async()`. Both include a 60s timeout, partial-text fallback assembly, and emit `entities_extracted` / `preferences_detected` events. This closes out the streaming matrix — all 8 frameworks now stream text.
- **`--import-preview` CLI flag.** Parses a chat export file (`--import-file …` + `--import-type …`) and prints a sanity-check summary (entity counts, conversation date range, sample titles) without scaffolding or ingesting. Useful before committing to a long import of a 1 GB+ ChatGPT/Claude AI export. Implemented in `cli.py::_run_import_preview()`.
- **NAMS error classification surfaced in `/health`.** New `_classify_memory_error()` in `memory.py.j2` buckets NAMS init failures into `auth` / `rate_limit` / `network` / `config` / `unknown`, with human-readable messages mapped per category (e.g. "NAMS authentication failed — verify MEMORY_API_KEY at https://memory.neo4jlabs.com"). The `/health` endpoint now returns `nams_error`, `nams_error_message`, `nams_error_detail`, and `nams_dashboard` so the frontend can show a useful diagnostic instead of a generic "memory unavailable". Exposed via `get_error_category()` / `get_error_message()` / `get_error_detail()` for the FastAPI startup banner.
- **Memory-backend auto-detection from `.env`.** New `@model_validator` in `config.py.j2` reconciles `memory_backend` with the credentials actually present in `.env`: flips `nams → bolt` if `MEMORY_API_KEY` is blank but `NEO4J_URI` is set (and vice-versa), printing a warning. An explicit `MEMORY_BACKEND` env var still wins. Default Neo4j credentials in generated `.env` are now empty rather than baked-in placeholder passwords.
- **Lighter NAMS dependency footprint.** Generated `pyproject.toml` for NAMS scaffolds drops the `sentence-transformers` extra (NAMS does embeddings server-side) — extras shrink from `[litellm,sentence-transformers]` to `[litellm]`. `_resolve_embedding_model()` short-circuits to `None` on NAMS so the generated venv no longer pulls `torch` for a backend that doesn't use local embeddings.

### Bug Fixes

- **Custom-domain renderer crash.** `_get_domains_path()` was imported inside a narrow `try:` scope in `renderer.py`, raising `UnboundLocalError` in the success path after partially writing `ontology.yaml`. Import hoisted to module scope.
- **Custom-domain generation produced silently-truncated ontologies.** `custom_domain.py` now checks the LLM response `stop_reason` for truncation, validates with Pydantic, and asserts completeness (non-empty `system_prompt` / `visualization` / `agent_tools`) before accepting. Provides actionable retry messages and clearer errors when the Anthropic/OpenAI SDK isn't installed.
- **Agent-template degradations across all 8 frameworks.** Jinja conditionals that checked `param.default` truthiness were rewritten to `param.default is defined`, and a silent fallback that swallowed Jinja syntax errors was removed so render failures now surface instead of degrading to stub code. Affects every framework template (`anthropic_tools`, `claude_agent_sdk`, `crewai`, `google_adk`, `langgraph`, `openai_agents`, `pydanticai`, `strands`).
- **Partial streamed text discarded on agent errors.** Strands and CrewAI now accumulate emitted text deltas and return the partial response when a generator raises mid-stream, instead of throwing away accumulated output. Also drops a redundant `RuntimeError` branch from the memory error classifier.
- **NAMS Docker builds crashed on `spacy download`.** The v0.11.2 fix for the generated `Makefile` is now mirrored in `Dockerfile.backend.j2` via `{% if not is_nams %}` — NAMS images no longer fail at build time on a download command for a package they don't depend on.
- **Frontend "Ask about" button rendered on non-string entity names.** `ContextGraphView.tsx.j2` adds a `typeof === "string"` guard before reading `.properties.name`.
- **E2E selector regex out of date.** `e2e/app.spec.ts.j2` regexes updated from `/try a demo scenario/i` to `/try these/i` to match the current welcome card label.

### Documentation

- **`docs/docs/how-to/use-nams.md` — new "Seeding a relationship-rich graph" section.** Documents NAMS's current lack of `add_relationship` REST support and shows two working patterns: (Option A) scaffold with `--self-hosted --demo` for the rich dev experience, flip `MEMORY_BACKEND=nams` for production reads; (Option B) seed bolt first then migrate. Includes a `TODO(nams-relationships)` pointer for the future server-side API.
- **Generated README (`base/README.md.j2`)** — minor wording updates for the NAMS sign-up flow and the `--import-preview` workflow.

### Internal / CI

- **CodeQL `py/incomplete-url-substring-sanitization` cleanup.** Test assertions in `test_nams_adapter.py` switched from URL substring checks (`"memory.neo4jlabs.com" in env`) to full-URL or content-phrase matches.
- **Frontend devDeps pinned (`package.json.j2`).** Added `@types/react-dom ^19.0.0`; `overrides` section pins `lodash ^4.17.24` and `postcss ^8.5.10` to dodge known vulnerable transitive versions.
- **New test coverage:** ~600 new test lines spread across `test_nams_adapter.py` (NAMS error classification, backend auto-detection, Dockerfile spacy guard), `test_renderer.py` (template-degradation guards, custom-domain regression), `test_custom_domain.py` (truncation/completeness validation), `test_generated_project.py` (CrewAI/Strands streaming surface), `test_chat_import.py` (`--import-preview`), `test_cli.py`, `test_doc_snippets.py`, and `test_routes_integration.py`.

## v0.11.2 — Post-release stabilization + pre-release smoke-render target (2026-05-19)

Rolls up three follow-up fixes surfaced by running v0.11.0/v0.11.1 end-to-end on a fresh machine, plus a durable safeguard against the same class of bugs.

### Bug Fixes

- **Full matrix + performance test suites broken on CI.** `test_matrix.py` (184 combos) and `test_performance.py` (23 domains) defined their own local `runner = CliRunner()` fixtures that bypassed the auto-`--self-hosted` shim added to `test_cli.py` in v0.11.0. With NAMS as the new default, every matrix/perf invocation hit the "NAMS API key required for non-interactive mode" guard and failed. 207 of 1,398 slow-suite tests failed on the v0.11.1 tag. **Fix:** moved `_AutoSelfHostedRunner` and the `runner` / `nams_runner` fixtures to `tests/conftest.py` so every test file inherits the auto-self-hosted behavior. Removed the now-duplicate fixtures from `test_cli.py`, `test_matrix.py`, and `test_performance.py`.

- **`make install` and Docker builds crashed on NAMS scaffolds** with `No module named spacy`. Both the generated `Makefile`'s `install-backend` target and the generated `Dockerfile.backend` ran `python -m spacy download en_core_web_sm` unconditionally, but spacy is only present in the `[extraction]` extra which NAMS scaffolds correctly omit (entity extraction happens server-side on NAMS). **Fix:** wrapped the `spacy download` line with `{% if not is_nams %}` in both `Makefile.j2` and `Dockerfile.backend.j2`. On bolt scaffolds the Makefile path is additionally guarded by an `import spacy` check so it stays robust even if the user uninstalls the extraction extras post-scaffold. Four regression tests in `test_nams_adapter.py::TestBoltRenderedTemplates`: `test_makefile_skips_spacy_download_on_nams`, `test_makefile_guards_spacy_download_on_bolt`, `test_dockerfile_skips_spacy_download_on_nams`, `test_dockerfile_includes_spacy_download_on_bolt`.

- **`make test` in generated projects crashed** with `No module named pytest`. The generated `pyproject.toml` didn't declare pytest or httpx anywhere, so `uv sync` never installed them — the generated `tests/test_routes.py` scaffold couldn't run. **Fix:** added `[project.optional-dependencies] dev = ["pytest>=8.0", "httpx>=0.27"]` to `pyproject.toml.j2`, and changed the generated Makefile's `install-backend` target from `uv sync` → `uv sync --extra dev`. Generated projects can now run `make test` out of the box.

### New Tooling

- **Root `make smoke-render` target** — full scaffold → install → import-check → run-generated-tests sweep for both backends, in `<1 min`, no Neo4j / NAMS / LLM keys required. Catches the class of breakage the mocked unit suite can't see:
  - dep-resolution failures (`uv sync` conflicts)
  - install-time crashes (e.g. spacy download on NAMS)
  - import-time failures in generated `app.main` (e.g. questionary default validation, framework SDKs that validate API keys at module-load time)
  - generated test-scaffold regressions
- Sub-targets `make smoke-render-nams` and `make smoke-render-bolt` for per-backend runs. `make smoke-render-clean` removes the scratch directory (`/tmp/ccg-smoke-render` by default).
- Verified passing locally:
  - NAMS: render → install (no spacy) → import-check → 2 generated tests pass
  - Bolt: render → install (with guarded spacy download) → import-check → 2 generated tests pass

### Process Note

The three issues bundled here all surfaced from running the actual product end-to-end on a fresh machine after the v0.11.0/v0.11.1 tags. Each was a class of issue the mocked unit suite couldn't catch by design (CLI fixture bypass, install-time shell commands, generated-project deps). `make smoke-render` is the durable answer — run it before tagging future releases.

## v0.11.1 — Wizard framework-prompt fix (2026-05-19)

### Bug Fixes

- **Interactive wizard crashed at the framework prompt** with `ValueError: Invalid 'default' value passed. The value ('Strands') does not exist in the set of choices.` — `questionary.select(default=...)` validates the default against `Choice.title`, not `Choice.value`. The wizard was passing the display label (`"Strands"`) but choices used the framework key (`"strands"`) as their value. Fixed by removing the `default=` argument entirely and reordering the choices list so `DEFAULT_FRAMEWORK` is first (questionary highlights the first row on entry). Regression test `TestQuestionaryConstruction` added to `tests/test_wizard.py` — exercises the real `questionary.select` constructor (only `.ask` is stubbed) so any bad `default=` argument fails at construction time. Total test count: 1,177 → 1,179.

## v0.11.0 — NAMS by Default + LiteLLM Provider Injection (2026-05-19)

### Breaking Changes

- **Default memory backend flipped from self-hosted Neo4j to NAMS** — `create-context-graph my-app` now scaffolds against the hosted [Neo4j Agent Memory Service](https://memory.neo4jlabs.com) by default. The wizard collects a NAMS API key as its memory step. Use `--self-hosted` (or any explicit `--neo4j-*` flag) to opt into the legacy bolt path. Existing scaffolded projects are unaffected; only newly generated projects pick up the new default.
- **Generated `pyproject.toml` pins `neo4j-agent-memory>=0.4.0,<0.6.0`** (was `>=0.1.0`). Extras conditional on backend: NAMS scaffolds get `[litellm,sentence-transformers]`; self-hosted scaffolds additionally get `[extraction,fuzzy]` for local entity extraction.
- **`ingest_data()` library signature changed** — now takes a `ProjectConfig` instead of separate Neo4j credentials. CLI users see no change; programmatic users of the `create_context_graph` package must update callers.
- **`MEMORY_API_KEY` env var added** to generated `.env` files. When set, the library auto-routes to NAMS even if `MEMORY_BACKEND` is unspecified.

### New Features

- **NAMS hosted backend support** — Generated `app/memory.py` now constructs `MemoryClient(MemorySettings(backend="nams", nams=NamsConfig(api_key=...)))` on the NAMS path. Sign-up panel printed in the wizard with the `https://memory.neo4jlabs.com` landing URL.
- **`--self-hosted` CLI flag** — Preserves the legacy bolt-Neo4j path with full demo fixtures, schema DDL, and relationship-rich graph view. Recommended for workshops, screen recordings, demos, and air-gapped use.
- **LiteLLM provider injection** — Generated memory layer reads `MEMORY_LLM` and `MEMORY_EMBEDDING` env vars (LiteLLM-style provider strings, e.g. `anthropic/claude-haiku-4-5`, `bedrock/anthropic.claude-3-haiku-20240307-v1:0`, `vertex_ai/gemini-1.5-flash`, `ollama/llama3`). Native adapters resolve first (Anthropic, OpenAI, Bedrock, Vertex AI, SentenceTransformers); everything else routes through LiteLLM. Default fallback: `sentence-transformers/all-MiniLM-L6-v2` for embeddings, `anthropic/claude-haiku-4-5` (or `openai/gpt-4o-mini`) for entity extraction.
- **Streamlined 6-prompt wizard** — Collapsed from 11 prompts. Domain picker switched to `questionary.autocomplete`. Inline Anthropic-key prompt removed (deferred to post-scaffold `.env` editing with a prominent reminder panel). Advanced settings (MCP toggle, extraction toggles, extra API keys) gated behind a single Y/N prompt. Median wizard run is now ~6 prompts vs ~11.
- **Default agent framework: AWS Strands** — was previously unselected; the wizard now suggests `strands` as the default. All 8 frameworks remain supported.
- **Backend-aware route adapters** — Generated `app/routes.py` dispatches `/expand`, `/documents`, `/traces`, `/schema/visualization`, `/entities/{name}`, `/search`, `/cypher` to the NAMS REST adapter (`app/memory_adapter.py`) or bolt Cypher path based on `MEMORY_BACKEND`. `/gds/*` returns 501 on NAMS.
- **Backend-aware MCP config** — `claude_desktop_config.json` ships in NAMS or bolt shape depending on the scaffold. NAMS forces `mcp_profile=core` because extended-profile tools rely on unsupported endpoints (preferences/facts).
- **Backend-aware `make reset`** — On NAMS, enumerates entities via REST and deletes one-by-one (slow but correct, with a printed warning). On bolt, retains today's `MATCH (n) DETACH DELETE n`.
- **NAMS-aware `make seed`** — Generated `generate_data.py` branches on `settings.memory_backend`. On NAMS, delegates to `memory_adapter.ingest_fixtures_nams()` which does the B-partial port (see below). On bolt, applies schema + ingests via Cypher.
- **Health endpoint backend-aware** — `/health` returns `{"memory_backend": "nams", "nams": <bool>}` on NAMS or `{"memory_backend": "bolt", "neo4j": <bool>}` on self-hosted.

### NAMS Write-Path Caveats (v0.4)

The NAMS REST API exposes a narrower write surface than bolt Cypher. The CLI does **best-effort B-partial ingest** with these documented gaps:

- **Relationships are dropped on NAMS** — `add_relationship` is not yet exposed by NAMS REST. The CLI logs a single warning per ingest run. The graph view shows entities but no edges.
- **Entity properties collapse into `description`** — NAMS REST accepts only `{name, type, description}` per entity. All other properties (status, severity, blood_type, etc.) are serialized into a markdown block inside `description`. The frontend property panel renders this markdown so the data remains readable.
- **Preferences and facts are unsupported** — `auto_preferences=True` is forced off on NAMS via `ProjectConfig.effective_auto_preferences`. `auto_extract=True` still runs but extracted relationships are silently dropped.
- **Schema DDL skipped on NAMS** — NAMS owns its schema. `CREATE CONSTRAINT`/`CREATE INDEX` statements from `generate_cypher_schema()` are no-ops on the NAMS path.

For the full relationship-rich demo experience, scaffold with `--self-hosted --demo`.

### New CLI Flags

| Flag | Purpose |
|---|---|
| `--self-hosted` | Use self-hosted Neo4j instead of NAMS (the v0.10 default behavior) |
| `--nams-api-key` | NAMS API key (also reads `MEMORY_API_KEY` env) |
| `--nams-endpoint` | Override NAMS endpoint URL (defaults to `https://memory.neo4jlabs.com/v1`) |
| `--memory-llm` | LiteLLM provider string for memory entity extraction |
| `--memory-embedding` | LiteLLM provider string for memory embeddings |

### New Docs Pages

- [Use NAMS](docs/docs/how-to/use-nams.md) — sign-up, API key, switching between NAMS and self-hosted, troubleshooting
- [Configure Memory Providers](docs/docs/how-to/configure-memory-providers.md) — LiteLLM provider strings, native adapters, default fallback behavior, per-provider auth examples
- [Memory Backends](docs/docs/explanation/memory-backends.md) — conceptual NAMS vs self-hosted comparison, choosing per-project, frontend dispatch architecture

### Tests

- **1,177 passing fast tests** (was 1,102 in v0.10). 50 new tests across 4 files:
  - `test_ingest_nams.py` (15) — `_ingest_with_nams` dispatch, entity serialization, document/trace ingestion, relationship-skip warning, missing-API-key error path, `reset_memory_store` for both backends.
  - `test_wizard.py` (7) — drives the interactive wizard via patched questionary; covers NAMS happy path, NAMS+advanced, self-hosted Docker, self-hosted existing-Neo4j, and edge cases.
  - `test_memory_adapter.py` (18) — renders a project, imports the generated `memory_adapter.py` via `importlib`, exercises every adapter function with `AsyncMock` MemoryClient.
  - `test_routes_integration.py` (10, gated by `pytest.importorskip("fastapi")`) — renders a NAMS or bolt project, mounts the generated FastAPI app via `TestClient`, asserts correct dispatch on `/health`, `/documents`, `/search`, `/schema/visualization`, `/expand`, `/traces`, `/gds/*`.
  - `test_nams_adapter.py` extended with 5 runtime dispatch tests.
- **`scripts/e2e_smoke_test.py`** — added `--backend {bolt,nams}` flag. `bolt` default preserves existing flow; `nams` exercises the hosted-memory scaffold path (requires `MEMORY_API_KEY` env).
- **`[dev]` extras** — now include `fastapi`, `httpx`, `pydantic-settings` so route integration tests run in CI.

### Implementation Notes

- **`MemorySettings` / `MemoryClient` / `MemoryIntegration` construction split** in `memory.py.j2` — explicit `MemoryClient(settings)` then `MemoryIntegration(client=client, ...)` (instead of having `MemoryIntegration` build the client implicitly). Enables NAMS backend + LiteLLM provider injection in one place.
- **CodeQL false-positive fix** — `test_nams_adapter.py` assertion changed from substring host check (`"memory.neo4jlabs.com" in env_example`) to full URL match (`"https://memory.neo4jlabs.com/v1" in env_example`) to satisfy `py/incomplete-url-substring-sanitization`.
- **Generated test scaffold** (`backend/tests/test_routes.py`) — fixture patches both bolt and NAMS connect/close paths so the generated test suite works regardless of backend.

## v0.10.0 — Local-File Document Connector (2026-05-18)

### New Connector
- **local-file connector** — Deterministic ingestion of local Markdown, PDF, HTML, AsciiDoc, and Word documents into `:Document` → `:Section` hierarchies, with `LINKS_TO` edges between sections and documents. No LLMs, no embeddings, no randomness. URI-keyed nodes integrate with the existing MERGE-on-`(name, domain)` pipeline without changes to `ingest.py`. Section URIs use GitHub/Pandoc slug rules (NFKD-normalize, ASCII-lower, `[a-z0-9_]` runs collapse to `-`); duplicate-heading collisions disambiguate per-parent (`-1`, `-2`, …); skipped heading levels (e.g. H1 → H3) preserve the original level on the child node rather than synthesizing intermediate H2s. Parser strategy per format: markdown-it-py with GFM extensions (Markdown); three-tier fallback pypdf outline → `/StructTreeRoot` → pdfplumber font-size heuristic (PDF); BeautifulSoup + lxml (HTML); pure-Python regex with block-delimiter state tracking (AsciiDoc); python-docx (Word). Adds 8 optional dependencies to the `connectors` extra: `markdown-it-py`, `mdit-py-plugins`, `pdf-oxide`, `pypdf`, `pdfplumber`, `beautifulsoup4`, `lxml`, `python-docx`. Implementation in `connectors/local_file_connector.py` and `connectors/_local_file/` subpackage (parser, mapper, slug, link-resolver). 1,721-line `test_local_file_connector.py` plus `test_local_file_vault.py` functional test (`make test-functional`) round-trips a 14-file fixture vault.

### Bug Fixes
- **`reset_database()` connection lifecycle** — Generated `context_graph_client.py` previously assumed a driver was already open. It now opens its own driver when `_driver` is `None` and closes it via `try/finally`, preserving any pre-existing connection. `TestResetDatabase` regression coverage added in `tests/test_generated_project.py`.

### Tests
- 1,102 passing fast tests (1,321 collected including slow/integration/functional).

## v0.9.5 — Options Intelligence Domain & GitHub Connector Enhancements (2026-04-29)

### New Domain
- **options-intelligence** (23rd domain) — Options market intelligence covering 0DTE analysis, dealer positioning (GEX/DEX/VEX/CHEX), gamma regime classification, key levels, and trading strategies. 8 entity types (Underlying, OptionsContract, ExposureLevel, Regime, KeyLevel, Trade, MarketEvent, Strategy), 17 relationships (HAS_OPTION, EXPOSURE_AT, IN_REGIME, TRIGGERED_BY, FLIPPED_TO, PRECEDED_BY, etc.), 10 agent tools (`get_regime`, `get_key_levels`, `get_exposure_by_strike`, `get_trades_by_strategy`, etc.), 5 document templates (market briefs, trade journals, regime analysis), 9 decision traces (trade entry, regime flip, level breach, VIX spike, etc.), 4 demo scenarios. Pre-generated fixture: 65 entities, 125 relationships, 25 documents. 17 new property clamp ranges in `generator.py` for options-specific values (delta `[-1, 1]`, gamma `[0, 0.15]`, IV `[0.05, 2.0]`, strike, GEX, etc.). 8 label-specific name pools and ID prefixes added in `name_pools.py`. Tickers (SPX, SPY, QQQ, IWM, AAPL) added to the global `_TICKER_POOL`.

### GitHub Connector Enhancements
- **Configurable per-resource limits** — `GITHUB_LIMIT=20` sets the default cap for issues, PRs, and commits. Override individually with `GITHUB_ISSUES_LIMIT`, `GITHUB_PRS_LIMIT`, `GITHUB_COMMITS_LIMIT`. Pagination now uses `itertools.islice` to stream results instead of materializing full pages.
- **Issue/PR body import toggle** — `GITHUB_IMPORT_BODY=true` (default) controls whether issue and PR bodies are imported as `Document` nodes. Issue/PR bodies are now labeled "Body" rather than "Document" to better reflect their source.
- **Cross-link issues, PRs, and commits** — `GITHUB_LINK_ISSUES_PRS=true` (default) creates `CLOSES` and `REFERENCES` edges between commits, issues, and PRs. Regex matches `(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved) #N` → `CLOSES`; bare `#N` → `REFERENCES`. GraphQL `closingIssuesReferences` provides authoritative PR closures. `GITHUB_LINK_SOURCE=both` selects `regex` / `graphql` / `both` (falls back to `both` on invalid values). `CLOSES` takes precedence over `REFERENCES` for the same pair, regex and GraphQL results are deduped, and references to numbers outside the fetched set are silently skipped. GraphQL failures log a warning and return empty rather than hard-failing.

### Bug Fixes
- **Connector relationship field names** — All 7 connectors (GitHub, Notion, Jira, Slack, Gmail, Google Calendar, Salesforce) now emit `source_name`/`target_name` in relationship dicts (was `source`/`target`), aligning with the ingest schema.
- **Generated `config.py` Settings fields** — Added env-var fields for all 7 connectors (GitHub, Notion, Jira, Slack, Salesforce, Linear, etc.) so credentials and toggles are honored. Previously silently dropped by `extra: "ignore"`.
- **Generated `pyproject.toml`** — Added connector package dependencies (`PyGithub`, `notion-client`, `atlassian-python-api`, `slack-sdk`, etc.) so generated projects install required SDKs.
- **`memory.py.j2` boolean rendering** — Switched `| tojson` to `| capitalize` so Jinja-emitted booleans render as Python `True`/`False` rather than JSON `true`/`false`.
- **CLI command/comment alignment** — Fixed inconsistent spacing in CLI help output so commands and comments line up.
- **Test patch target for `is_connected`** — Tests now patch `app.main.is_connected` (where it's used after `from … import is_connected`) rather than `app.context_graph_client.is_connected` (where it's defined). Generated `test_routes.py` mock fixture also sets dummy API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) before importing `app.main`, since PydanticAI's `Agent(...)` validates the key at module import time. Added regression guard `test_test_file_mocks_is_connected` in `tests/test_generated_project.py` so accidental removal is caught in the fast unit suite.
- **options-intelligence fixture quality** — Rewrote fixture with consistent per-underlying scoping. Cross-underlying contamination eliminated (each Underlying now has scoped `OptionsContract`, `ExposureLevel`, `KeyLevel`). Strike scales realistic per underlying (SPX ~5800, SPY ~580, QQQ ~500, IWM ~212, AAPL ~230). Regime timestamps chronologically consistent with `FLIPPED_TO`/`PRECEDED_BY` edges. Document content references title entities. Decision-trace placeholders replaced with concrete values from fixture entities. Greeks normalized (delta `-1..1`, gamma `0..0.15`, proper put delta signs).

### Improvements
- **`make schema` target** — Added to the generated `Makefile`. Comments on `make seed` and `make import-and-seed` clarified.
- **Tests** — 12 new tests for GitHub linking (regex/GraphQL/dedupe/precedence). 89 new tests for body-import toggle and configurable limits. 54 additional renderer tests covering connector field renames, memory boolean rendering, config Settings fields, and the new Makefile target.

## v0.9.4 — GitHub Actions release workflow bugfixes (2026-04-17)

### Bug Fixes
- **Upgrade npm cli for trusted publishing** - project currently uses npm 20 but we need the latest npm cli in order to perform the trusted publishing OIDC workflow.

## v0.9.3 — GitHub Actions release workflow bugfixes (2026-04-17)

### Bug Fixes
- **Target npm-wrapper directory for node pkg** - previously workflow was looing for package.json in top-level directory, but it lives in npm-wrapper.

## v0.9.2 — Batch Seeding Performance (2026-04-15)

### Bug Fixes
- **Batch entity seeding with `UNWIND`** — `make seed` previously executed one MERGE query per entity. With large Claude Code imports (27k+ entities), this caused seeding to appear to hang. Entity, relationship, and document creation now use `UNWIND $batch` with batches of 500, reducing ~27,000 round-trips to ~55.
- **Batch ingestion in `import_data.py`** — The `--ingest` path in generated import scripts also uses `UNWIND` batching.

## v0.9.1 — Claude Code Connector Fixes & Docs (2026-04-15)

### Bug Fixes
- **Fix `make import` crash** — The scaffolded Claude Code connector crashed with `int(None)` when `max_sessions` was passed as `None` from `import_data.py`. All credential reads now use the `or` idiom for None-safety.
- **Fix missing Settings fields** — Added `claude_code_scope`, `claude_code_since`, `claude_code_max_sessions`, `claude_code_content_mode`, `claude_code_base_path` to the generated `config.py` Settings class so `.env` variables are honored (previously silently dropped by `extra: "ignore"`). Also added `google_client_id`, `google_client_secret`, `gws_folder_id` for the Google Workspace connector.
- **Fix dict vs attribute access** — All template connectors return `dict` from `fetch()`, but `import_data.py` used attribute access (`data.entities`). Changed to `data["entities"]` across the board.
- **Don't write empty `fixtures.json` on crash** — The import script now skips writing `fixtures.json` when no data was collected, instead of silently overwriting it with empty lists.
- **Fix `make test-connection`** — Combined two sequential `asyncio.run()` calls into one, fixing the "Event loop is closed" error that made it print both "successful" and "failed".

### Improvements
- **Expanded scaffolded connector** (268 → 457 lines) — The template connector now extracts 9 entity types (added GitBranch, Error, Decision, Preference, Alternative) with 14 relationship types (added ON_BRANCH, ENCOUNTERED_ERROR, MADE_DECISION, CHOSE, REJECTED, NEXT, PRECEDED_BY, USED_TOOL, EXPRESSES_PREFERENCE). Includes secret redaction, language detection from file extensions, file path validation, and `[rerun: bN]` suffix stripping.
- **Claude-Code-specific demo scenarios** — When `--connector claude-code` is active, the "Try these" prompts now show relevant questions ("What files have I modified?", "Show me decisions", "What are my coding preferences?") instead of generic software-engineering prompts about PRs and incidents.

### Documentation
- Fixed dark-mode announcement bar (was white on dark background)
- Added explicit `@easyops-cn/docusaurus-search-local` plugin configuration
- Marked `ANTHROPIC_API_KEY` as required for the chat agent in the Claude Code tutorial
- Replaced "copy `.env.example` to `.env`" with "edit the generated `.env`" in the tutorial
- Added CLI flag literals next to connector display names in the intro page
- Added "Focus" column with one-line disambiguators to the domain catalog
- Custom 404 page with links to Introduction, Quick Start, and "Report Broken Link"
- "See all 22 domains →" link on the homepage carousel
- Tighter scroll transitions for the memory-type explainer
- Higher-contrast step numbers on the "How it works" section

### Tests
- 7 new tests (1060 total): Settings fields, dict access, entity types, redaction, scenario override

## v0.9.0 — Claude Code Connector, Google Workspace & Security (2026-04-02)

### New Connectors
- **Claude Code connector** — Reads local session JSONL files from `~/.claude/projects/` with no authentication required. Parses user/assistant messages, tool_use/tool_result blocks, and progress entries. Extracts 7 entity types (Project, Session, Message, ToolCall, File, GitBranch, Error) with 10 relationship types. Includes heuristic **decision extraction** (user corrections, deliberation markers, error-resolution cycles, dependency changes) and **preference extraction** (explicit statements, package frequency). Secret redaction (API keys, tokens, passwords, connection strings) applied by default. 8 session intelligence agent tools injected via the renderer. 5 CLI flags (`--claude-code-scope`, `--claude-code-project`, `--claude-code-since`, `--claude-code-max-sessions`, `--claude-code-content`). Implementation split into `connectors/claude_code_connector.py` and `connectors/_claude_code/` subpackage (parser, redactor, decision_extractor, preference_extractor).
- **Google Workspace connector** — Imports from 6 Google APIs (Drive Files, Comments, Revisions, Activity, Calendar, Gmail) with OAuth2 authentication and dynamic scope building. Extracts **decision traces** from resolved comment threads in Google Docs (question, deliberation, resolution, participants). 10 decision-focused agent tools (`find_decisions`, `decision_context`, `who_decided`, `document_timeline`, `open_questions`, `meeting_decisions`, `knowledge_contributors`, `trace_decision_to_source`, `stale_documents`, `cross_reference`). Cross-connector linking detects Linear issue references in comment bodies, doc names, email subjects, and meeting descriptions. 9 CLI flags for scoping imports. Rate limiting (950 queries/100s with exponential backoff).

### Security
- **Replaced weak cryptographic hashing** — Switched from MD5/SHA1 to SHA-256 for content hashing in connectors (code scanning alerts #8 and #9).

### Bug Fixes
- **Session collision fix** — `Session.name` now uses `session_id` as the unique MERGE key to avoid cross-session collisions when importing Claude Code data.
- **Linear connector hardening** — Don't fail on blank Linear team key; improved robustness of Linear import with better error handling.

### Improvements
- Google Workspace connector template improvements and renderer integration
- Updated import_data.py template to handle new connectors
- **10 total SaaS connectors** (GitHub, Notion, Jira, Slack, Gmail, Google Calendar, Salesforce, Linear, Google Workspace, Claude Code)
- 955 passing tests (1,165 collected including slow/integration)

## v0.8.2 — Linear Connector (2026-04-02)

### New Features
- **Linear SaaS connector** — GraphQL-based connector with cursor-based pagination and rate limiting against `https://api.linear.app/graphql`. Maps 12 entity types (Issue, Project, Cycle, Team, Person, Label, WorkflowState, Comment, ProjectUpdate, ProjectMilestone, Initiative, Attachment) to the POLE+O entity model with 26 relationship types. Imports issue relations, threaded comments with resolution tracking, project updates with health status, milestones, initiatives, attachments, and Linear Docs. Issue history entries are transformed into decision traces capturing state transitions, assignment changes, and priority changes with actor attribution. Uses stdlib only (`urllib.request`).
- **Linear connector hardening** — Named constants, structured logging, URLError/JSONDecodeError/429 handling with retry, pagination safety limits (`MAX_PAGES`), null-safe field access, team key validation during `authenticate()`, incremental sync via `updated_after`.

### Bug Fixes
- **Fix traces silent failure** — Decision trace ingestion no longer silently fails on malformed data.

### Documentation
- New tutorial: `linear-context-graph.md` — end-to-end guide for importing Linear project data
- Updated CLI options reference and SaaS data import guide

## v0.8.1 — Schema Node Fix & Responsive Docs (2026-03-31)

### Bug Fixes
- **Click on schema node works again** — The `Button` component was added to the template for the "Ask about [entity]" feature but was never added to the `@chakra-ui/react` import. Clicking a schema node crashed the React component, preventing double-click expand from working.
- **Python 3.14 boundary** — Added `<3.14` to `requires-python` for forward compatibility.

### Improvements
- Improved responsive design for Docusaurus landing page

## v0.8.0 — Embedding Fix, Data Quality & Documentation (2026-03-28)

### Critical Fixes
- **neo4j-agent-memory no longer requires OpenAI API key** — Removed `[openai]` extra from the generated `pyproject.toml` dependency. Conversation memory now uses local `sentence-transformers` (`all-MiniLM-L6-v2`, 384 dims) by default. If `OPENAI_API_KEY` is set in the environment, automatically upgrades to OpenAI `text-embedding-3-small` (1536 dims). Added `sentence-transformers>=2.0` as an explicit dependency so local embeddings work out of the box with zero API keys.
- **openai-agents framework warns about missing API key** — CLI now displays a clear warning when `--framework openai-agents` is selected without `--openai-api-key`. The interactive wizard prompt text changes to indicate the key is "required" (not optional) for this framework.

### Data Quality
- **67 new entity name pools** — Added domain-appropriate names for every entity label across all 22 domains. `LABEL_NAMES` now has 118 entries (up from 51), eliminating all "Label 1" / "Label 2" fallback names. Covers agent-memory (Conversation, Memory, Session, ToolCall), digital-twin (Alert, Asset, Sensor, Reading, MaintenanceRecord, System), golf-sports (Round, Handicap, Hole, Course, Tournament), hospitality (Room, Reservation, Guest, Staff), oil-gas (Well, Equipment, Reservoir, Formation, Permit), personal-knowledge (JournalEntry, Note, Bookmark, Contact, Topic, Project), retail-ecommerce (Order, Product, Customer, Campaign, Category), vacation-industry (Booking, Package, Resort, Season), wildlife-management (Sighting, Camera, Habitat, Individual, Threat), conservation (Stakeholder), data-journalism (Correction), GIS (Boundary, Coordinate, Feature, Layer, MapProject, Survey), GenAI/LLM-Ops (Model, Prompt, Evaluation, Experiment), product-management (Epic, Metric, Objective, Release, Feedback, UserPersona), and scientific-research (Paper, Researcher, Grant, Institution).
- **Post-generation value clamping** — LLM-generated entities are now post-processed by `_validate_and_clamp()` in `generator.py`. Clamps 28 property types to domain-reasonable ranges (e.g., `price_per_night`: $30–$2,000; `duration_hours`: 0.25–24; `rating`: 1–5; `latitude`: -90–90). Also corrects taxonomy class mismatches (e.g., Bengal Tiger → "mammalia", not "aves").
- **Richer entity descriptions** — Added `_LOCATION_LABELS`, `_EVENT_LABELS`, and `_OBJECT_LABELS` sets (parallel to existing `_PERSON_LABELS`/`_ORGANIZATION_LABELS`) for POLE-type-aware descriptions. Added 7 label-specific description overrides for Medication, Permit, Sensor, Equipment, Paper, Model, and Species. Fallback descriptions no longer say "record tracked in the knowledge graph".
- **digital-twin fixture fix** — Fixed label casing in `digital-twin.json` (UPPERCASE → PascalCase) to match the domain YAML schema.
- **Domain-scoped entity MERGE keys** — Changed entity MERGE from `{name: $name}` to `{name: $name, domain: $domain}` in both `generate_data.py.j2` and `ingest.py`. Prevents constraint violation warnings when multiple domains share a single Neo4j instance.

### Framework Fixes
- **google-adk AttributeError guard** — Added `try/except AttributeError` around `runner.run_async()` in both `handle_message` and `handle_message_stream` to gracefully handle the `google-genai` SDK's `BaseApiClient` cleanup error when `_async_httpx_client` was never initialized.

### Documentation
- **Quick-Start page** — New `docs/quick-start.md` with a 5-step guide: scaffold → Neo4j setup → configure → seed → start.
- **use-neo4j-local guide** — New `docs/how-to/use-neo4j-local.md` covering `@johnymontana/neo4j-local` (npx), Neo4j Desktop, and Docker standalone with troubleshooting tips.
- **Domain catalog** — New `docs/reference/domain-catalog.md` listing all 22 domains with entity types, agent tool counts, sample questions, and scaffold commands. Auto-generated from domain YAML files.
- **Architecture diagram** — Mermaid flowchart added to the Introduction page showing CLI → Template Engine → Backend/Frontend → Neo4j data flow. Added `@docusaurus/theme-mermaid` for rendering.
- **switch-frameworks 404 fix** — Added `slug: switch-frameworks` to frontmatter so `/docs/how-to/switch-frameworks` resolves correctly.
- **Updated navigation** — Sidebar now includes quick-start, use-neo4j-local, and domain-catalog pages.

### Frontend UX
- **Larger status indicator** — Backend health dot enlarged from 8px to 12px with a text label ("Connected" / "Degraded" / "Offline").
- **Health check retry on initial load** — First page load now retries the health check 3 times with exponential backoff (1s, 2s, 4s) before showing "Offline". Prevents the transient "Internal Server Error" on initial Next.js compilation.
- **Improved empty graph state** — Empty knowledge graph panel now shows a link icon, "Your knowledge graph will appear here" heading, and actionable guidance text instead of a minimal "No graph data to display" message.

### Testing
- 691 passing tests (89 new), up from 602
- **New `tests/test_fixtures.py`** (88 tests) — Cross-validates all 22 domains:
  - Schema alignment: fixture entities have all required YAML properties
  - Agent tool property references: Cypher queries only reference properties that exist in schema or fixtures
  - Label coverage: fixtures include entities for every YAML-defined label
  - Data quality: numeric property values fall within reasonable ranges

## v0.7.0 — Documentation Site Redesign & CI (2026-03-28)

### New Features
- **Docusaurus landing page redesign** — New animated terminal hero section with domain-specific demo commands, improved hero animation timing, and terminal width/height fixes.
- **Mobile navigation** — Responsive nav bar with mobile layout improvements and design polish.
- **CI matrix job** — Full test suite (including domain × framework matrix, perf, and generated project tests) now runs in the `matrix` CI job on push to `main`.

### Documentation
- **4 new docs pages** — "Use Neo4j Aura", "Use Docker", "Why Context Graphs?", "Framework Comparison"
- Updated sidebars with all new pages

### Bug Fixes & Data Quality
- Bug fixes and data quality improvements across domains
- Updated docs and test coverage

## v0.6.1 — Stability, Data Quality & Tool Coverage (2026-03-28)

### Critical Bug Fixes
- **CrewAI dependency fix** — Changed `crewai>=0.1` to `crewai[anthropic]>=0.1` in framework dependencies. The crewai agent template uses `llm="anthropic/claude-sonnet-4-20250514"` which requires the anthropic extra. Without it, the generated project crashes on startup with `ImportError: Anthropic native provider not available`.
- **CLI non-interactive mode fix** — The CLI no longer requires a positional `PROJECT_NAME` argument when all flags (`--domain`, `--framework`) are provided. Auto-generates a slug like `healthcare-pydanticai-app`. Also added TTY detection with helpful error messages for CI/CD environments.

### Data Quality Improvements
- **Document Markdown rendering** — Static document content now uses Markdown headings (`##`) instead of RST-style `===`/`---` separators. The DocumentBrowser component renders content with ReactMarkdown.
- **Entity-derived document titles** — Document titles now reference primary entities: "Discharge Summary: Maria Elena Gonzalez" instead of generic "Discharge Summary #1".
- **Realistic entity descriptions** — Replaced generic "Comprehensive patient profile for..." with POLE-type-aware descriptions using domain roles and industries (e.g., "Dr. Sarah Chen, attending physician specializing in healthcare").
- **Domain-aware Organization.industry** — Added `DOMAIN_INDUSTRY_POOL` for all 22 domains. Healthcare organizations get "Hospital Systems" instead of "Technology".
- **Realistic decision trace observations** — Observations now reference actual entity names: "Verified Dr. Sarah Chen against healthcare standards" instead of generic "Found 7 relevant records".
- **Improved thinking text filter** — Added continuation patterns to catch multi-sentence agent thinking blocks between tool calls.

### New Agent Tools (All 22 Domains)
- **`list_*` tools** — Every domain now has a list tool for its primary entity type (e.g., `list_patients`, `list_players`, `list_accounts`) with sort and limit parameters.
- **`get_*_by_id` tools** — Every domain now has a direct ID lookup tool that returns the entity with all connections (e.g., `get_patient_by_id`, `get_player_by_id`).
- **Gaming-specific** — Added `get_top_players` tool (sort by level) for the gaming domain.

### Frontend Improvements
- **"Ask about this" button** — Clicking a node in the Knowledge Graph shows an "Ask about [entity]" button that sends a query to the chat.
- **Node hover tooltips** — Graph nodes show full name, labels, and key properties on hover.
- **Health polling optimization** — Reduced polling frequency from 30s to 60s.
- **Responsive hint text** — Keyboard shortcut hint hidden on small screens to prevent overlap.
- **Suggested question max width** — Pill buttons capped at 320px to prevent layout stretching.
- **Scrollable label badges** — Label filter badges in the graph panel scroll when they overflow.
- **Seed constraint fix** — Entity seeding now uses `ON CREATE SET / ON MATCH SET` to avoid constraint violations on re-seed.

### Documentation
- **4 new docs pages** — "Use Neo4j Aura", "Use Docker", "Why Context Graphs?", "Framework Comparison"
- **Updated sidebars** — All new pages added to Docusaurus navigation

### Testing
- 602 passing tests (57 new), up from 545

## v0.6.0 — Comprehensive Testing Feedback (2026-03-28)

### Framework Fixes
- **CrewAI no longer hangs** — Added explicit `llm="anthropic/claude-sonnet-4-20250514"` to prevent defaulting to OpenAI. Added request-level logging and reduced timeout to 60s.
- **Strands serialization fix** — Added `_extract_text()` helper that robustly extracts text from agent results, handling `ParsedTextBlock` serialization issues from newer Anthropic SDK versions.
- **Google ADK API key support** — Added `--google-api-key` CLI flag (`GOOGLE_API_KEY` env), wizard prompt when google-adk is selected, and `GOOGLE_API_KEY` in generated `.env`/`.env.example` templates.

### Document & Trace Ingestion Fix
- **`--ingest` now creates proper Document and DecisionTrace nodes** — Both ingestion paths now create `:Document` and `:DecisionTrace`/`:TraceStep` nodes using direct Cypher, matching the `generate_data.py` pattern that the frontend expects. Previously, Documents and Decision Traces panels appeared empty after `--ingest`.
- **Entity MERGE fix** — Direct driver ingestion now uses `MERGE (n:Label {name: $name}) SET ...` instead of `MERGE (n:Label {all_props})`, preventing duplicate nodes.

### Data Quality
- **Domain-aware base entities** — Person, Organization, Location, Event, and Object entities now use domain-specific names and roles (doctors for healthcare, traders for finance, game designers for gaming, etc.).
- **Fixed templated property values** — Properties like "Metformin 500mg - Contraindications" now replaced with realistic values. Added pools for contraindications, dosage_form, allergies, sector, lead_reporter, manufacturer, mechanism_of_action, population_trend, and habitat.

### Frontend UI Improvements
- **Redesigned chat input** — Bordered container with focus highlight and keyboard shortcut hint (Chakra UI Pro inspired).
- **Suggested questions redesign** — Pill-shaped buttons with full text (no 60-char truncation), "Try these" label with Sparkles icon.
- **Message avatars** — User and assistant messages now have Circle avatars with User/Bot icons.
- **Tool progress counter** — Shows "Running tool N of M..." during tool execution.

### CLI
- **`--demo` convenience flag** — Shortcut for `--reset-database --demo-data --ingest`
- **`--google-api-key` flag** — New CLI flag with `GOOGLE_API_KEY` env variable support

### Testing
- 545 passing tests (35 new), up from 510

## v0.5.3 — Agent Loop Breakout Fix (2026-03-27)

### Bug Fixes
- **Prevent agents from returning pre-tool text as the final answer** — PydanticAI's `run_stream` fires a `FinalResultEvent` the moment any `TextPart` begins streaming. When Claude emits "I'll search for..." alongside a tool call, `run_stream` treats that text as the final output and exits before tool results are incorporated. Replaced `agent.run_stream()` + `stream_text()` with `agent.run()` in `handle_message_stream`, which completes the full agent loop before emitting text.
- **Ruff lint fixes** — Resolved lint errors across connectors, tests, and scripts.
- **Test assertion fix** — Directory conflict test now asserts on "not empty" instead of "already exists" for better cross-platform compatibility.

## v0.5.2 — Agent Framework Refinements (2026-03-26)

- Improved Anthropic Tools and Claude Agent SDK agent templates
- Enhanced `context_graph_client` event handling and error recovery
- ChatInterface component improvements
- Better error handling in API routes
- `generate_data.py` improvements for data quality
- 74 new ontology validation tests

## v0.5.1 — UX Improvements & Bug Fixes (2026-03-25)

### Bug Fixes
- SSR hydration fix in frontend components
- PydanticAI tool serialization fix — agent tools now return JSON string types correctly
- Google ADK hyphenated domain name sanitization
- HuggingFace warning suppression in agent templates

### Improvements
- Retry button on chat errors
- Agent thinking text collapsible filter — reasoning steps render in a collapsible "Show reasoning" section
- Strands `max_tokens` configuration support
- Cypher query validation tests across all 22 domains

## v0.5.0 — Data Quality & Domain Completeness (2026-03-24)

### New Features
- **22 complete domain ontologies** with pre-generated LLM fixture data shipped for all domains
- **Domain-specific static name pools** — 200+ realistic names across 50+ entity labels (medical diagnoses, financial instruments, software repos, etc.)
- Label-aware ID prefixes (`PAT-` for Patient, `ACT-` for Account, etc.)
- 12+ domain-specific property pools (currency codes, ticker symbols, drug classes, medical specialties, severities)
- `domain` property on all ingested entities for cross-domain isolation when sharing a Neo4j instance
- Structured document templates for static fallback data generation

### Bug Fixes
- Fixed missing SSE event messages in chat streaming
- Float value clamping for confidence/rating/efficiency fields

### Testing
- 510 passing tests (145 new), up from 365

## v0.4.6 — Conversation Fetching Fix (2026-03-24)

- Fixed conversation history fetching in `context_graph_client` for multi-turn sessions

## v0.4.5 — Framework & Build Fixes (2026-03-24)

- Fixed `pyproject.toml` build configuration bug
- Strands framework default changed from Bedrock to AnthropicModel
- Agent template improvements across multiple frameworks
- Domain YAML fixes for gaming, genai-llm-ops, healthcare, personal-knowledge, product-management, retail-ecommerce, software-engineering, and trip-planning

## v0.4.4 — Strands Model Default (2026-03-24)

- Changed Strands agent framework default from AWS Bedrock to Anthropic native model (`AnthropicModel`)

## v0.4.3 — API Keys & Docker Support (2026-03-23)

- Fixed API key handling and validation across agent frameworks
- Added `Dockerfile.backend` template for Docker builds
- Makefile improvements for containerized deployments

## v0.4.2 — E2E Testing & Bug Fixes (2026-03-23)

- Bug fixes across agent templates
- Playwright e2e test scaffolding for generated projects (`app.spec.ts`, `playwright.config.ts`)
- Improved e2e smoke testing infrastructure

## v0.4.1 — Streaming & E2E Infrastructure (2026-03-23)

- **Server-Sent Events (SSE) streaming** for real-time chat responses and tool call visualization
- `POST /chat/stream` endpoint with `asyncio.Queue`-based event streaming
- Token-by-token text streaming for PydanticAI, Anthropic Tools, Claude Agent SDK, OpenAI Agents, LangGraph
- Real-time tool call events with Timeline/Spinner/Collapsible UI components
- Text delta batching (~50ms) to optimize React re-renders
- E2E smoke testing infrastructure (`scripts/e2e_smoke_test.py`)
- Documentation updates

## v0.4.0 — Hardening, Security & DX (2026-03-23)

### Bug Fixes
- **Critical:** Enum identifier sanitization — special characters (`A+`, `A-`, `3d_model`) in domain ontology enum values now generate valid Python identifiers with value aliases
- **Critical:** Graceful degradation when Neo4j is unavailable — backend starts in degraded mode, `/health` endpoint reports connectivity status
- **High:** Cypher injection prevention in GDS client — label parameters validated against entity type whitelist
- **High:** CrewAI async/sync deadlock resolved — replaced bare `asyncio.run()` with `nest_asyncio`-compatible helper, crew execution moved to thread
- **High:** Claude Agent SDK model version now configurable via `ANTHROPIC_MODEL` environment variable
- **Medium:** Silent exception swallowing replaced with structured warning messages in `ingest.py` and `vector_client.py`
- **Medium:** JSON parsing errors in agent tool calls now return helpful error messages instead of crashing
- **Medium:** Input validation (`max_length`) added to chat and search request models
- **Low:** CLI validates empty project names before entering wizard
- **Low:** Healthcare YAML blood type enums properly quoted

### New Features
- `--dry-run` CLI flag — preview what would be generated without creating files
- `--verbose` CLI flag — enable debug logging during generation
- `/health` endpoint in generated projects — returns Neo4j connectivity status and app version
- CORS origins configurable via `CORS_ORIGINS` environment variable
- `constants.py` module in generated projects — centralizes magic strings (index names, graph projections, embedding dimensions)
- Document browser pagination (page size 20, prev/next controls)
- Semantic HTML landmarks (`<main>`, `<section>`, `<aside>`) and ARIA labels in frontend
- Actionable error messages in chat interface — distinguishes backend errors, network failures, and Neo4j unavailability

### Security
- Query timeouts (30s default) on all Neo4j operations
- Credential warnings in generated `.env.example`
- CORS production configuration guidance

### Testing
- 365 passing tests (51 new), up from 314
- New: enum identifier sanitization edge cases
- New: models.py compilation across all 22 domains (prevents enum regression)
- New: v0.4.0 feature validation (health endpoint, constants, graceful degradation, input validation, CORS, pagination)
- New: CLI validation and flag tests

## v0.3.0 — Memory Integration, Multi-Turn & Graph Visualization (2026-03-23)

- neo4j-agent-memory integration for multi-turn conversations
- Interactive NVL graph visualization (schema view, double-click expand, drag/zoom, property panel)
- LLM-generated demo data (80-90 entities, 25+ documents, 3-5 decision traces per domain)
- Markdown rendering in chat with tool call visualization
- Document browser and entity detail panel
- Improved graph visualization and frontend styling
- Docusaurus documentation site setup and deployment
- Improved domain fixture data quality
- 314 passing tests

## v0.2.0 — Connectors & Custom Domains (2026-03-22)

### New Features
- **7 SaaS data connectors** — GitHub, Notion, Jira, Slack, Gmail, Google Calendar, Salesforce
- Each connector implements `BaseConnector` ABC with `authenticate()`, `fetch()`, and `get_credential_prompts()`
- Gmail/Google Calendar prefer `gws` CLI with Python OAuth2 fallback
- **Custom domain generation** — generate complete domain ontology YAMLs from natural language descriptions using LLM (Anthropic/OpenAI)
- Custom domains saved to `~/.create-context-graph/custom-domains/` for reuse
- Neo4j Aura `.env` import and `neo4j-local` support in wizard
- Documentation site (Docusaurus) with deployment configuration

## v0.1.1 — Initial Bug Fixes (2026-03-22)

- Bug fixes for CLI and template rendering
- Test improvements and expanded coverage
- Added `.gitignore` to generated projects

## v0.1.0 — Initial Release (2026-03-22)

- Interactive CLI scaffolding tool (`create-context-graph`) invoked via `uvx` or `npx`
- 7-step interactive wizard with Questionary prompts
- **8 agent frameworks:** PydanticAI, Claude Agent SDK, OpenAI Agents SDK, LangGraph, CrewAI, Strands, Google ADK, Anthropic Tools
- Domain ontology system with YAML definitions and two-layer inheritance (`_base.yaml`)
- Jinja2 template engine generating full-stack projects (FastAPI backend, Next.js + Chakra UI v3 frontend)
- Neo4j schema generation (constraints + GDS projections)
- Static and LLM-powered synthetic data generation pipeline
- Neo4j data ingestion via `neo4j-agent-memory` or direct driver fallback
- Domain-specific agent tools with Cypher queries
- NVL graph visualization component
