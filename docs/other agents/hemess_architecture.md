# Hermes Agent — Architecture Notes (cloned from NousResearch/hermes-agent)

Captured from the live tree at `temp/hermes-agent/` (shallow clone, HEAD `27562ad`).
Findings cross-referenced against our own README at `../../README.md`.

## TL;DR

Hermes is a **single-agent tool-calling loop at the core** with two orthogonal
fan-out mechanisms layered on top: **Mixture-of-Agents (MoA) advisory
parallelism** inside the model call, and a **single-level subagent delegation
swarm** outside the loop. It is not a DAG planner, not a ReAct paper-faithful
implementation, and not a multi-agent framework in the LangGraph/AutoGen sense.
It is an opinionated, narrow-waist runtime: the core loop is simple, the
capability surface (skills, plugins, MCP, tools, platforms) is huge and lives
at the edges.

## Repository facts (numbers from the cloned tree, not the README)

- HEAD: `27562ad fmt(js): npm run fix on merge (#90637)`
- 93 top-level entries; `agent/` package has 150 .py files; `tools/` has 129
  auto-discovered tool files; `hermes_cli/` has 215 entries; ~9,879 tracked files.
- One monolithic core: `run_agent.py` is **~12k LOC** (`AIAgent` class).
- Main loop extracted into `agent/conversation_loop.py` (~8.4k LOC).
- Companion monstrosity: `cli.py` ~11k LOC; `hermes_state.py` ~590k LOC
  (SQLite session store w/ FTS5).
- Test suite advertised as ~17k tests across ~900 files.

## Agent loop — what kind is it?

From `run_agent.py` (declared in `AGENTS.md`, visible in
`agent/conversation_loop.py`):

```python
while (api_call_count < self.max_iterations and self.iteration_budget.remaining > 0) \
        or self._budget_grace_call:
    if self._interrupt_requested: break
    response = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas)
    if response.tool_calls:
        for tool_call in response.tool_calls:
            result = handle_function_call(tool_call.name, tool_call.args, task_id)
            messages.append(tool_result_message(result))
        api_call_count += 1
    else:
        return response.content
```

Classification:

- **Single-agent iterative tool-use loop.** Not ReAct (no
  explicit Thought/Action/Observation prompts; messages are plain
  OpenAI chat format with `system / user / assistant / tool` roles;
  reasoning content lives in `assistant_msg["reasoning"]`, not in a
  parsed thought stream).
- **Not a DAG / not a planner.** No plan graph, no node-based control
  flow, no explicit task graph state. The only "graph" is the linear
  `messages` list.
- **Not an MRKL / not a chain.** It's a free-form tool-call loop where
  the model itself decides when to stop.
- **One graceful concession**: a one-turn "grace call" is allowed after
  the iteration budget is exhausted (`_budget_grace_call`), purely to
  let the model finish an in-flight tool result.

Bounded by:
- `max_iterations` (default 500).
- `iteration_budget` (per-turn token / cost ceiling).
- `run_budget_seconds` (wall-clock; at 80% threshold a `RUN_BUDGET_WRAPUP_NOTICE`
  is appended to the newest `role:"tool"` message in a cache-safe way).
- Interrupt checks (`_interrupt_requested`) every iteration.

## Prompt caching is sacred (per `AGENTS.md`)

Two policies shape almost every design decision:

1. **The system prompt and tool schema are byte-stable for the life of a
   conversation.** Mid-conversation invalidation of the cached prefix
   multiplies cost; only context compression is allowed to mutate past
   messages.
2. **Mid-loop steer messages ride the existing `role:"tool"` channel**
   (e.g. the 80% budget wrap-up notice is *appended* to the newest tool
   message, not injected as a new synthetic user message). Role
   alternation (`assistant → tool → assistant`) is treated as a hard
   invariant — never two same-role messages in a row, never a synthetic
   user message injected mid-loop.

This rules out ReAct-style scaffolding where the loop injects a
"Thought: ... Action: ..." prefix each step; Hermes keeps raw chat
completions instead.

## Subagent delegation — `delegate_task` (`tools/delegate_tool.py`)

A `delegate_task` tool exists in the `delegation` toolset. Quotes from
its module docstring:

> Spawns child AIAgent instances with isolated context, inherited
> toolsets, and their own terminal sessions. Supports single-task and
> batch (parallel) modes.

Each child receives:
- Fresh conversation (no parent history).
- Its own `task_id` (own terminal session, file-ops cache).
- Parent's toolsets with child-blocked tools stripped.
- A focused system prompt built from the delegated `goal` + `context`.

The parent's context only ever sees the call and a returned summary —
never the child's intermediate tool calls or reasoning.

**Blocked tools in subagents** (`DELEGATE_BLOCKED_TOOLS`):
`delegate_task`, `clarify`, `memory`, `send_message`, `cronjob` —
i.e. children cannot recursively delegate, cannot ping the user, cannot
schedule work, cannot DM across platforms, cannot mutate shared
`MEMORY.md`. (piece of memory isolated? limited nr of tools isolated?)

**Roles**:
- `role="leaf"` (default) — focused worker, no `delegate_task`,
  no `clarify`, no `memory`, no `send_message`, no `cronjob`;
  keeps `execute_code` (programmatic tool calling).
- `role="orchestrator"` — retains `delegate_task` so it can spawn
  its own workers. Gated by `delegation.orchestrator_enabled`
  (default true) and bounded by `delegation.max_spawn_depth`
  (default **2**).

Two execution shapes:
- **Single:** `goal` (+ optional `context`, `toolsets`).
- **Batch (parallel):** `tasks: [...]` — each gets its own subagent
  running concurrently. Concurrency is capped by
  `delegation.max_concurrent_children` (default **3**).

DEPTH x CONUCRENCY
2 x 3 At a time?

**Durability caveat** (relevant to our README's "loop forever" claim):
background `delegate_task` is detached from the current turn but still
**process-local**. For work that must survive a restart, Hermes routes
through `cronjob` or `terminal(background=True, notify_on_complete=True)`
instead. Our README assumes persistence we should not borrow from this
path.

Classification: this is a **single-level delegation tree** (parent →
children, depth ≤ 2) — **not** a swarm in the sense of many autonomous
peers electing leaders or gossiping. Parents always own the loop; children
are leaf workers or one-level-down orchestrators.

## Mixture-of-Agents (MoA) — advisory fan-out at the model-call boundary

`agent/moa_loop.py` plus `/moa` slash command. Architecture:

> The slash command is deliberately **not a model tool**. It marks one
> user turn as MoA-enabled; **the normal Hermes agent loop still owns
> tool calling and turn termination**, while this module gathers
> reference-model context before each model iteration.

So MoA is a pre-iteration enrichment, not a replacement loop:

1. For each model call, fan out to N **reference ("advisor") models**
   in parallel via `ThreadPoolExecutor`.
2. Each reference gets a *different* system prompt (an "advisory view")
   and emits a text response.
3. The reference outputs are redacted (`agent.redact.redact_sensitive_text`
   + PII patterns for emails / formatted phone numbers) and bundled
   into a "guidance block" that is injected into the aggregator's
   context.
4. The aggregator model (the one actually driving the loop) calls with
   that guidance block in front of it, then continues the normal
   tool-calling loop until `max_iterations`.

This is the **MoA architecture from the "Mixture-of-Agents" paper**
(Wang et al., 2024) — layered LLM aggregator over parallel LLM
proposers — adapted to be **non-invasive**: it never touches the
core loop, only the model-call surface.

Persistence: `moa.save_traces` (off by default) writes one JSONL line
per MoA turn to `<hermes_home>/moa-traces/<session_id>.jsonl` with
the exact messages each reference received. Crucially this is a
**side-channel** — it is NOT the conversation `messages` table and
never enters replay, because MoA references are advisory side-calls
with their own system prompts, not conversation turns.

## Kanban — multi-agent *work queue* (closest thing to a DAG)

From `AGENTS.md`, "Kanban (multi-agent work queue)" section:

> Durable SQLite-backed board that lets multiple profiles / workers
> collaborate on shared tasks. Users drive it via `hermes kanban
> <verb>`; workers spawned by the dispatcher drive it via a dedicated
> `kanban_*` toolset so their schema footprint is zero when they're
> not inside a kanban task.

- Hard boundary = **board** (workers spawned with `HERMES_KANBAN_BOARD`
  pinned in their env so they can't see other boards).
- Soft boundary = **tenant** (one specialist fleet can serve multiple
  businesses with workspace-path + memory-key isolation).
- Dispatcher: long-lived loop that reclaims stale claims, promotes
  ready tasks, atomically claims, and spawns assigned profiles. Runs
  in the gateway by default.
- Failure budget: `kanban.failure_limit` (default **2**) consecutive
  non-success attempts → auto-block.

Worker toolset names:
`kanban_show, kanban_complete, kanban_request_review,
kanban_request_changes, kanban_block, kanban_heartbeat,
kanban_comment, kanban_create, kanban_link, kanban_attach,
kanban_attach_url, kanban_attachments`; profiles that explicitly
enable the `kanban` toolset outside a dispatcher-spawned task also
get `kanban_list` and `kanban_unblock`.

Classification: **asynchronous work-queue orchestration**, not an
in-loop DAG planner. Tasks go through state transitions
(create → block → review → complete), but the runtime model is
"durable queue + polling dispatcher," not "graph executor."

## Memory — closed learning loop

`AGENTS.md` calls this out explicitly:

> Agent-curated memory with periodic nudges. Autonomous skill
> creation after complex tasks. Skills self-improve during use. FTS5
> session search with LLM summarization for cross-session recall.
> Honcho dialectic user modeling.

Concrete subsystems:

- **Memory providers** (plugin-orchestrated, MemoryProvider ABC at
  `agent/memory_provider.py`; manager at `agent/memory_manager.py`):
  `honcho, mem0, supermemory, byterover, hindsight, holographic,
  openviking, retaindb` — discovery is bundled-first (reverse of the
  general plugin system) precisely so a dropped-in user directory
  can't shadow a shipped provider.
- **Periodic nudges** during turns — the agent is prompted to persist
  knowledge and create skills autonomously.
- **Skill self-improvement** — skills created by the agent can be
  patched in place; the **Curator** background loop
  (`agent/curator.py` + `agent/curator_backup.py`) auto-archives
  stale skills, never deletes (max destructive action is archive).
- **FTS5 session search** with LLM summarization
  (`hermes_state.py` — a 590k-line session DB).
- **Honcho dialectic user modeling** — keeps two-views of the user
  (the user's self-model vs. the agent's model of the user) and
  reconciles them.

This is the part most relevant to our "**inspired by hermes**"
line in the README — see "What we should borrow" below.

## The narrow-waist principle

The most load-bearing architectural fact, from `AGENTS.md`:

> The core is a narrow waist; capability lives at the edges. Every
> model tool we add is sent on every API call, so the bar for a new
> *core* tool is high.

The "Footprint Ladder" (each rung adds more permanent surface):

1. Extend existing code.
2. CLI command + skill.
3. Service-gated tool (`check_fn`).
4. Plugin.
5. MCP server (in the catalog).
6. New core tool (last resort).

Most capability expansion goes through plugins and skills rather than
growing the core toolset. This is the architectural lens for any
change — and it's also why Hermes can ship 40+ tools without blowing
the prompt-cache budget on every turn: tools are split into ~30
`toolsets` (`browser`, `clarify`, `cronjob`, `delegation`, `file`,
`homeassistant`, `image_gen`, `kanban`, `memory`, `messaging`, `moa`,
`skills`, `terminal`, `todo`, `tts`, `vision`, `web`, ...) and each
platform's adapter picks a base toolset.

## Tool surface (auto-discovered)

The registry (`tools/registry.py`, no deps) auto-imports every
`tools/*.py` file at module load via top-level
`registry.register()` calls. Wiring into an *exposed* toolset is the
manual step — `_HERMES_CORE_TOOLS` is the default bundle every
platform's base toolset inherits from, and it is explicitly **not**
dead code: it's the gate.

Notable tools shipped (from the directory listing):
- `terminal_tool.py`, `file_tools.py`, `web_tools.py`,
  `browser_tool.py`, `computer_use_tool.py`, `vision_tools.py`
  — IO surface.
- `delegate_tool.py` — subagent spawning.
- `kanban_tools.py` — kanban worker surface.
- `todo_tool.py`, `clarify_tool.py`, `memory_tool.py`,
  `skills_tool.py`, `skill_manager_tool.py`, `tool_search.py`
  — agent-level (intercepted by `run_agent.py` *before*
  `handle_function_call()`, not dispatched through the generic
  handler).
- `code_execution_tool.py` — programmatic tool calling (lets
  subagents batch many tool calls into a single Python RPC, per our
  README's "collapse multi-step pipelines" line).
- `tts_tool.py`, `image_generation_tool.py`,
  `video_generation_tool.py`, `x_search_tool.py`,
  `microsoft_graph_*`, `feishu_*` — integrations.
- `mcp_tool.py` — MCP client (Hermes connects to *other* MCP servers).

MCP is integrated *as a client*, not as a server.

## Plugin system

Two surfaces, both rooted under `plugins/`:

1. **General plugins** — `hermes_cli/plugins.py` + `plugins/<name>/`
   each with a `register(ctx)` function exposing lifecycle hooks:
   `pre_tool_call, post_tool_call, pre_llm_call, post_llm_call,
   on_session_start, on_session_end`, plus
   `ctx.register_tool(...)` and `ctx.register_cli_command(...)`.
2. **Memory providers** — `plugins/memory/<name>/`,
   `MemoryProvider` ABC. Bundled-first precedence.
3. **Model providers** — `plugins/model-providers/<name>/`,
   each calls `providers.register_provider(ProviderProfile(...))`.
   Lazy, separate discovery (so it doesn't double-instantiate).
   User-installed of the same name **override** bundled.
4. **Context engines, image-gen providers, kanban, observability,
   etc.** — same ABC + orchestrator pattern.

Policy (May/June 2026, per AGENTS.md): **no new in-tree third-party
memory or product plugins**. They must ship as standalone plugin
repos that users install into `~/.hermes/plugins/` or via pip entry
points, because every product absorbed into the tree becomes the
core team's burden to maintain against a fast-moving core.

## Skills

Two parallel surfaces:

- `skills/` — built-in, loadable by default. Categorized directories
  (`apple, autonomous-ai-agents, creative, devops, email, github,
  index-cache, media, mlops, note-taking, productivity, research,
  smart-home, social-media, software-development`).
- `optional-skills/` — bundled but **not active by default**; installed
  explicitly via `hermes skills install official/<cat>/<skill>`.

Each skill is a `SKILL.md` plus optional `scripts/`, `references/`,
`templates/`. SKILL.md frontmatter includes
`name, description, version, author, license, platforms` and
`metadata.hermes.{tags, category, related_skills, config}`. The
**author** is a hardline field: human contributors first, "Hermes
Agent" as secondary.

**Hardline rule from `AGENTS.md`**: every new or modernized skill
must have `description` ≤ 60 characters, one sentence, ends with a
period, with no marketing words. Tests live at
`tests/skills/test_<skill>_skill.py`, stdlib + pytest + mock only, no
network.

The skill-creation + self-improvement + FTS5-search + curation loop
is the single biggest "self-evolving" idea our README cites. **See
"Curator" below.**

## Curator — skill lifecycle

Background loop, not a user gesture. From `AGENTS.md`:

- Core: `agent/curator.py` (review loop, auto-transitions, LLM review
  prompt) + `agent/curator_backup.py` (pre-run `tar.gz` snapshots).
- CLI: `hermes curator <verb>`: `status, run, pause, resume, pin,
  unpin, archive, restore, prune, backup, rollback`.
- Telemetry: `tools/skill_usage.py` owns the sidecar
  `~/.hermes/skills/.usage.json` with `use_count, view_count,
  patch_count, last_activity_at, state (active | stale | archived),
  pinned`.

Hardline invariants:
- Curator only touches skills with `created_by: "agent"` provenance.
- **Never deletes**; max destructive action is archive.
- Pinned skills are exempt from every auto-transition and from the
  LLM review pass.

## Cron — scheduled automations

`cron/jobs.py` + `cron/scheduler.py`. Schedule formats:
duration (`"30m"`), `"every"` phrase (`"every 2h"`, `"every monday 9am"`),
5-field cron expression, or ISO timestamp (one-shot).

Per-job fields: `skills` (load specific skills), `model`/`provider`
overrides, `script` (pre-run data collection; `no_agent=True` makes
the script the entire job), `context_from` (chain prior job's
output into this job's prompt), `workdir` (load that workdir's
`AGENTS.md`/`CLAUDE.md`), multi-platform delivery.

Hardening invariants (relevant to our "self-evolving + always-on"
goal): **3-minute hard interrupt** on cron sessions — runaway agent
loops cannot monopolize the scheduler; file lock at
`~/.hermes/cron/.tick.lock` prevents duplicate ticks; cron sessions
pass `skip_memory=True` by default (memory providers do **not** run
during cron).

## Multi-platform delivery / agent surface

Hermes runs the **same agent core** across five surfaces:

- CLI (`hermes`, prompt_toolkit).
- TUI (`hermes --tui`, Ink + React in `ui-tui/`, JSON-RPC over stdio
  to `tui_gateway/` Python backend).
- Messaging gateway (`hermes gateway` — Telegram, Discord, Slack,
  WhatsApp, Signal, Email, plus ~20 more adapters under
  `gateway/platforms/`).
- Electron desktop app (`apps/desktop/`, `@assistant-ui/react`).
- Web dashboard (`hermes dashboard` → embeds the real `hermes --tui`
  via xterm.js + PTY, **does not** reimplement the chat surface).

The same `AIAgent` core is invoked regardless of surface; the
platform only changes which toolsets are enabled and which
prompts/handlers wrap requests and responses. The `apps/desktop/`
docs say it explicitly: "**Do not re-implement the primary chat
experience in React.**"

A process-local **profile** system (`hermes -p <name> ...`) gives
multiple fully-isolated instances each their own `HERMES_HOME`
(config, keys, memory, sessions, skills, gateway). Per-profile
secrets are **scope-installed per turn** (`_profile_runtime_scope`),
and `os.environ` reads of profile-scoped config must fail closed —
not borrow from the default profile, because under
`gateway.multiplex_profiles` `os.environ` holds the **default
profile's** values.

## Comparison to our README (`../../README.md`)

| Our spec line | What Hermes does | Verdict |
|---|---|---|
| "Multiagent" | Single core loop + `delegate_task` (depth ≤ 2) + MoA advisory fan-out + Kanban work queue. **Not** graph-based. | Same surface, narrower design. |
| "Memory (semistructured, llm wiki, semantic)" | Plugin-managed memory providers (`honcho, mem0, supermemory, ...`) + FTS5 session search + Honcho dialectic user modeling. | Strong overlap. Our README's "ideally structured" is conservative — Hermes already does it. |
| "Learning capabilities / self-evolving, inspired by Hermes" | Curator background loop + skill self-improvement + nudges + Honcho dialectic user modeling. | This is the **borrow-here** row. Our `self-learning` doc is vague — Hermes specifies the mechanism concretely. |
| "Powerful on simple fast cheap LLM backbones" | MoA is exactly that — advisor fan-out to multiple fast/cheap models for context, aggregator for synthesis. | Strong alignment, but MoA defaults off; users opt in via `/moa`. |
| "Swarm (multiple agents in parallel)" | `delegate_task` batch mode (parallel) + MoA reference fan-out (parallel) + Kanban dispatcher (queued). Concurrency caps: `delegation.max_concurrent_children=3`. | Yes, but with hard caps — not unlimited swarm. |
| "Risome-like / tree-like graph in plan step" | **No graph state.** No plan-DAG layer. The model decides ordering. | Hermes does not have this. **We would be inventing it if we want it.** |
| "Loop with success criteria" | Bounded by `max_iterations`, `iteration_budget`, `run_budget_seconds`, interrupt, grace call. | Roughly same. Hermes does not have a "goal-satisfaction function" — termination is "model emits final response" + tool budgets. |
| "Tools, Skills, MCPs" | Toolsets, Skills, MCP client, Plugins — built and mature. | Strong overlap. |
| "Bundled infrastructure (filesystem, sandbox, browser)" | Seven terminal backends: local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox. Browser via CDP. | Strong overlap. |
| "Orchestration logic (subagent, handoffs, model routing)" | Delegation + MoA + Kanban + provider plugins (`plugins/model-providers/`). | Same vocabulary, different shape — no formal handoff API, no smart routing layer (only `fallback_model` per agent). |
| "Hooks/middleware (compaction, continuation, lint)" | Plugin lifecycle hooks (`pre_tool_call, post_tool_call, ...`) + context compression + repetition guard + thinking-scrubber. | Same idea, named explicitly. |
| "CLI + UI" | CLI + TUI + Desktop + Web dashboard + 20+ messaging platforms. | Much bigger than ours. We can borrow CLI+TUI first. |

## Nuances and gotchas worth knowing

1. **No DAG planner.** If our README says "spin up a swarm to plan
   tree-like," Hermes does not actually do that. The plan is whatever
   the model writes into its messages; the loop just executes.
2. **Prompt-cache-stability changes the tool-onboarding strategy.**
   Mid-conversation additions to the tool schema are cache-breaking
   and almost always disallowed. New tools show up *next session*
   (or after explicit `/compress`). Hermes's "Footprint Ladder"
   follows directly from this.
3. **MoA references are advisory side-calls with their own system
   prompts** — not conversation turns. They never appear in the
   message history or replay; if `moa.save_traces` is on, they're
   written to a separate JSONL file. This is a non-trivial
   architectural choice.
4. **Subagent depth is hard-bounded at 2** (`max_spawn_depth=2`) and
   concurrency at 3 (`max_concurrent_children=3`). Not a free swarm.
5. **`delegate_task` is process-local** — not durable across restarts
   (durability goes through cron / `terminal(background=True,
   notify_on_complete=True)`).
6. **Children cannot write shared memory.** `DELEGATE_BLOCKED_TOOLS`
   excludes `memory`. Workers cannot corrupt the parent's `MEMORY.md`.
7. **Curator never deletes, only archives; only touches
   `created_by: "agent"` skills.** Bundled/hub skills are
   immutable to it.
8. **The core is one `AIAgent` class plus extracted `conversation_loop.py`**
   — everything else is layered at the edges. If we copy Hermes,
   copying the loop + toolset + profile scoping is most of the work;
   everything else is integration.

## What we should borrow (concrete, prioritized)

1. **Skill self-improvement + Curator loop.** Single highest-leverage
   idea for "self-evolving"; concretely specified in Hermes.
2. **MoA as a per-turn enrichment layer.** Cheap to implement on top
   of any LLM backend; doubles as our "powerful on cheap backbones"
   claim.
3. **Footprint Ladder** as a contribution policy. Keeps the system
   prompt from bloating.
4. **FTS5 session search + LLM summarization** for cross-session
   recall. Direct lift, no special integration.
5. **Profile-scoped `HERMES_HOME` + fail-closed secret scoping.**
   Solves "multiple isolated instances" cleanly. Our equivalent
   would be `WHALE_HOME`.
6. **Toolset system** with `check_fn` for service-gated tools. Lets
   us ship many capabilities without paying for them on every prompt.
7. **Plugin lifecycle hooks** (`pre_tool_call`, `post_tool_call`,
   `pre_llm_call`, `post_llm_call`, `on_session_start`,
   `on_session_end`) for deterministic middleware (lint, compaction,
   redaction, audit) without forking the core.

## What we should NOT borrow

1. The `risome/tree-like plan` claim — Hermes doesn't have it, and
   pretending otherwise is misleading.
2. MoA as default-on — it's off by default in Hermes for good reason
   (cost, PII surface). Slash command only.
3. Tool proliferation past what's actually load-bearing. The "narrow
   waist" lesson is the *constraint*, not a feature list.
4. "Same agent core across 5 surfaces" — that's a month of integration
   per surface. Start with CLI + one chat surface.
5. The process-local subagent model — if our README implies durable
   cross-restart workers, that's a different system (cron, queue,
   etc.), not a `delegate_task` clone.
