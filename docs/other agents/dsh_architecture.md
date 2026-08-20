# DeepSeek Harness (`dsh`) — Architectural Facts

> Source: `git@github.com:deepseek-ai/deepseek-harness.git` cloned into `/tmp/dsh`.
> Primary references: `docs/architecture.md`, `docs/subsystems/core.md`, `docs/subsystems/agent-team.md`, `docs/subsystems/subagent.md`, `docs/glossary.md`.
> Note: file is a vendor reference for the WHALE project, not a substitute for reading upstream docs.

`dsh` is a TS/Node multi-agent harness built on the [Cordis](https://github.com/cordiverse/cordis) plugin framework. The whole product — model adapter, tool registry, agent loop, CLI, Web UI — is implemented as Cordis plugins; there is no privileged core. The Web UI lives in `apps/web`, the CLI in `apps/cli`; shared logic is in `packages/*`.

---

## 1. Agent DAG / Topology — what kind of agent system is this?

It is **not a single DAG**. The harness composes several distinct, layered topologies. The clean way to read it:

### a. Loop hierarchy (intra-agent)
The fundamental unit is the **session log** (append-only `SessionEvent`s: `turn/start`, `step/start`, `assistant/chunk`, `tool/call`, `tool/result`, `turn/end`, …). Within one session:

```
turn → step → LLM request → tool calls → next step (if tool continuations) → turn end
```

A `turn` is zero or more `steps`. A `step` is one model request plus the tools it called. Whales's "act → observe → decide → repeat with exit criteria" maps cleanly onto dsh's `step → tool → next step`. The driver re-enters the same loop only while tools or queued inbox work owe more; it exits when `agent/turn-stopping` has nothing to drain.

### b. Agent tree (inter-agent)
Each `Agent` is a node holding one session. Hierarchy is expressed as a **tree rooted at one Lead Session**, with the tree edges recorded as durable `SessionHeader.parentSession` / `delegationDepth` data. A `TeamId` is the root `SessionId` under a distinct brand. Teams implement a **directed, acyclic task DAG** (`blockedBy` edges, must remain acyclic) layered over continuable subagents.

### c. Continuable-child roster
Continuable children are long-lived, addressable sub-sessions. They live as **roots in the live `AgentRegistry`** (not nested objects) and are discovered by `parentSessionId`. Each child has zero or one live **Activation** (process-local residency). The parent cannot "settle" while it owns a non-empty `ownedChildren` set.

### d. Multi-provider fan-out (one-shot delegates)
One-shot subagents are sibling nodes registered to a **named provider** (e.g. `spawn`, `fork`, `acp`, `codex`, `claude-code`, `dsh-sdk`). They live in parallel with other siblings but have no inbox-based durability — they are fire-and-collect runs that resolve to one `SubagentResult`.

So: **inter-agent = acyclic agent tree on durable parent-of relations, with a DAG of tasks binding teammates.** It is **not a free-form message-passing mesh**, and it is **not a generic cognitive DAG like LangGraph**. Code, not declarative graph wiring, chooses topology per spawn.

---

## 2. ReAct / Step pattern

`dsh` is **a step loop, not pure ReAct**. The body of every step is:

```
agent/pre-step (waterfall, may reject/rewrite)
  → assemble prompt sections + tool schemas (ctx.systemPrompt)
  → agent/request (waterfall, can replace LlmCallConfig)
  → llm/stream
  → assistant/chunk* → assistant/message
  → tool/call* → tools/pre-execute → tools/execute → tools/post-execute → tool/result*
  → step/end
```

The model produces structured tool calls (the `tool/call` events) that the framework executes; tool results are appended to the log and the next step re-derives history from that log. This is the **ReAct skeleton (Thought/Tool/Act/Observe)** but extended with:

- **Hard pre-step policy** via the `agent/pre-step` waterfall: listeners can rewrite or reject the claimed message batch — there is a typed `PreStepDecision = reject | enter(messages)` return.
- **Cross-step waterfalls** (`tools/pre-execute`, `tools/post-execute`) are scoped extension points for deterministic intervention.
- **Steering** at boundaries: `Agent.steer(message)` injects into the nearest step; the driver consumes it on the next admission.
- **Inbox as a durable queue**: every pending message has identity (`MessageId`), lives on `next-turn` or `next-step` lists, and is claimed with `agent/inbox/spliced` mutations.

There is **no separate "plan/act" ReAct phase** in dsh; planning is a separable subsystem (`packages/plan`) that surfaces a *plan mode* over the same agent loop rather than a second loop engine.

---

## 3. Nuances

These are the design choices that matter for comparison with WHALE:

1. **`agent.ctx` is per-agent scope, not inheritance.** Scoped registrations replace globals for that agent alone (shadowing); scoped tools do **not** auto-flow to descendants. Lineage (`parentSession`, `delegationDepth`) is pure data. There is no scope-tree invisibility leaking capability down — children get their composition from explicit `composeFrom(parentCtx)` (a bind to the parent's standing preset) or from their own setup.
2. **`model-visible means logged`** is a runtime invariant. Every fact the model can see must be reproducible from the session log (via `deriveMessages()`). Hooks that want to mutate model-visible content must extend `SessionEventMap` and render from the log.
3. **Everything is a plugin / capability seam.** A capability has three roles — Service Definition (`ctx.<key>` and the vocabulary), one or more Service Providers, one or more Consumers. Filesystem and subprocess share one execution world, so swapping one provider (e.g. to a sandbox) moves Bash, PTY, and LSP with it. This is how the harness claims one-provider-swap-changes-the-whole-product.
4. **Append-only session log + append-only roster.** `foldTeam()` replays one root Session into the roster, task board, and queued-minus-delivered mailbox. `revision` is compare-and-set on every task mutation; `blockedBy` edges are validated to keep the DAG acyclic.
5. **Branded ids** — `SessionId`, `CallId`, `TeamTaskId` are all `Branded<B extends string>` so they don't interchange at compile time.
6. **Map → derived-union pattern.** Plugin authors extend sum types with TypeScript declaration merging (e.g. extend `SessionEventMap` to add a new event variant) — no upstream edits.
7. **Inbox is a durable projection.** `next-turn` and `next-step` lists are owned by the agent and reconciled via normalized `agent/inbox/spliced` mutations. `Agent.inject()` lands context in the inbox without waking the driver; `followup()` queues a turn + wakes; `steer()` enters at the next step.
8. **Cancellation is typed (`AgentCancelCause`)** — `user | parent | hook | disposed`. The cause flows through `AbortSignal.reason` at runtime; the durable `turn/end` only knows `{ kind: 'aborted' }`.
9. **Lifecycles are event-driven + scope-filtered.** `agent/created`, `agent/disposed`, `agent/error`, `agent/status` are agent-scoped emissions; `agent/pre-step`, `agent/request`, `agent/request-error`, `tools/*` are *waterfalls* — listeners must call `next()` to delegate. `agent/turn-stopping` is serial with no `next()`.
10. **`SubagentDescriptorData` is the durable identity for session-backed children.** Two modes: `one-shot` (optional label) and `continuable` (required delegation description + provider/model/persona/toolFilter snapshot). The descriptor is **folded last-wins** in foldTeam so a child's own descriptor overrides any fork-seeded ancestor's. Cold resume trusts the persisted header's `delegationDepth` as the monotone floor.
11. **Continuation manager owns continuable children, not the provider.** Providers only contribute `prepareContinuable()` (a data-only spec); identity reservation, composition, Agent creation, inbox delivery, cold resume, ownership, and disposal live in `SubagentRuntime`. This means the same provider can serve both one-shot and continuable starts, with clearly separated responsibilities.
12. **Drain semantics on parent settle.** A parent cannot dispose while it owns non-empty `ownedChildren`; drain is child-first, awaits every branch despite individual failures, awaits `ctx.sessions.flush()` per child but ignores its participation boolean, propagates cancellation top-down.
13. **Driver is swappable.** `ctx.agents.setFactory(loop)` is the only seam — extension plugins depend on `core/agent` (interface + events), never on `core/agent-loop` (the default driver).
14. **`agent/*` hooks are type-bearing.** `agent/pre-step` carries `messages: UserMessage[]` (typed identified input), not opaque blobs. Hook bridges have to map their decision onto `PreStepDecision`.

---

## 4. Agent swarm

dsh exposes a **team-shaped swarm** — not a generic worker-pool:

| Concept | dsh equivalent |
|---|---|
| Swarm | `ctx.agentTeams` — `TeamService` on a single root `SessionId` |
| Worker | A **teammate** = continuable child Agent, durable session + one live Activation |
| Topology | Acyclic **task DAG** (subject, description, status, ownerId, `blockedBy[]`, advisory `writeScopes[]`) |
| Identification | `TeamId` = root SessionId; `TeamTaskId` = `task-<n>` (monotone, Team-local); `TeamMessageId` = globally random |
| Lifecycle | `provisioning` → `active` \| `failed` (terminal roster phases). Runtime `running`/`idle`/`inactive` is derived from Agent status and never rewrites the roster. |
| Mailbox | Durable: **Lead stores full queued message first**, then target acknowledges only after pending inbox item or recorded `user/message` is durable. `TeamMessageSnapshot` is retained until its target session records it. `TeamMessageSource` carries the de-dup key across inbox and history. Recovery mailbox = `queued − delivered`. |
| Messaging | `sendMessage(caller, targetName, content, delivery: 'quiet' \| 'wakeup')` — quiet delivery does not wake the receiver. |
| Coordination primitives | `spawnTeammate`, `sendMessage`, `createTask`, `updateTask` (CAS via `revision`), `getTask`, `listTasks`, `waitForChange(timeoutMs)`, `interrupt(targetName)` |
| Shared filesystem | Advisory `writeScopes: string[]` on tasks; runtime views surface overlap warnings without changing the durable snapshot. |

Important contrasts:

- **Implicit-root domain**: agent-team is "experimental, private, opt-in"; documented as **Agent Teams**. Teams are not the default harness mode — dsh is fundamentally **session-centric**; teams are a *coordinating layer* over continuable subagents on top of the same `ctx.agents`.
- **No free-form task graph library**: tasks have `subject`, `description`, `ownerId`, `blockedBy`, `writeScopes` — there is no generic graph-of-arbitrary-nodes, no GOTO/branch DSL. The DAG is a **task-ready queue**, not a control-flow graph.
- **Membership is one root**: `membership(agent)` looks up one agent's role (root/team/non-team). A team membership is a structural fact, not a runtime capability.
- **Concurrency model**: many sibling teammates may run in parallel (each is its own Agent + its own turn/step loop); consensus and join-on-task is via `blockedBy`. The Lead Session is not a controller-loop — it is a peer mailbox with one durable `agentTeams` service that everything else rides on.
- **No worker-pool language**: there is no `submitexec` queue with N workers. One can implement one on top of `spawnTeammate`, but the primitive is named teammate.

### One-shot delegations (related, not the same)
For non-team parallel work, the **subagent seam** gives you several registered providers. One-shot runs return a `SubagentRun` with `result: Promise<SubagentResult>`. Providers:
- `spawn` (in-process, fresh)
- `fork` (in-process, seeded with parent history)
- `acp` (Agent Client Protocol; remote)
- `codex` (delegates to a Codex turn)
- `claude-code`
- `dsh-sdk`

Multiple providers coexist. The runtime validates requested start-time capabilities against the provider's descriptor and rejects loud (`UNSUPPORTED_CAPABILITY`) — there is no silent degradation. Continuable start requires a provider with `prepareContinuable`. That method's **presence** is the capability (TS-narrowed discovery, not a flag).

---

## 5. Other architectural facts worth noting for WHALE

- **Profiles and bundles**: a running dsh is a plugin tree composed at boot from ordered layers. A **profile** lists bundles; a **bundle** is a distribution format for Cordis config rows + the code they mount. Layers apply in order: bundles in profile order → profile `cordis.patch.yml` → home-level `cordis.patch.yml` → `--patch` overlay. Patch targeting: a row by id replaces its whole config.
- **Presets** (`ctx.agentPresets`) are per-agent composition stacks — `mount()`, `composeFrom(parentCtx)` (bind), `recompose()` (re-link, only valid pre-step), `serviceFor(agent, name)` (read into an `isolate` realm). A preset publishes services behind `isolate` realms invisible outside the group.
- **Goals** (`ctx.goals`) implement a persistent same-session objective with a goal-round cap (`active`/`paused`/`blocked`/`complete`). A "goal round" is one continuation turn with the same driver; unrelated human turns don't consume the cap. Activation is `armed`/`disarmed` and is deliberately not durable — resume/fork always require a later mutation through `/goal` or the model tool.
- **Ralph**: the "fresh-agent Ralph loop" is a model-facing tool policy composed from workflow + subagent primitives, not a same-session goal, not an agent-loop mode, not a scheduler. Ralph rounds are fresh sessions sharing a workspace and one normalized bounded structured handoff — different from continuable subagents (which persist one session across rounds).
- **Lifecycle events are tiered**: durable session events (`turn/*`, `step/*`, `user/message`, `assistant/*`, `tool/*`) replay from the log; live `agent/*` events carry an `Agent` in hand and are scope-filtered; capability events (`fs/*`, `tools/*`, `telemetry/*`) attach policy/adapters to a seam without the loop importing them.
- **Driver admission** is one inbox boundary. Some messages wake the driver immediately; injected context sits in the inbox until another message does.
- **Disposal order is explicit**: `dispose()` stops the loop → awaits exit → unregisters agent → removes session → unwinds scoped world.
- **Plug-and-play memory**: `ctx.sessions` is the durable store; `SessionPersistence` interface supports JSONL and SQLite backends; `session/flush` is a checkpoint; resume/fork are primitive ops on the same store.
- **Concurrency**: the in-process driver runs each Agent inside `ctx.agents.withInitiator()`. Tokens like `currentInitiator()` / `requireInitiator()` / `withoutInitiator()` are process-local and explicit; ambient presence is not liveness or authorization.

---

## 6. Cross-check against WHALE README

Comparing `README.md` (WHALE) against dsh's architecture, the closest shape is:

| WHALE README concept | Closest dsh analog | Coverage in dsh |
|---|---|---|
| Multi-agent Harness | Plugin-based harness with `ctx.agents` registry | Full — but session-centric, not "multi-agent" in the swarm sense by default |
| LLM backbone | `ctx.llm` adapter seam + many named providers | Full — LLM provider is a plugin |
| Pick up / retrieve from memory | `ctx.sessions` log + `deriveMessages()` projection + per-session `SessionPersistence` | Full (but memory is event log, not semantic/RAG) |
| Learn (high-level understanding loop) | Same agent loop re-entered; no separate "learn" phase | Partial — only one loop |
| Plan (loop, possibly swarm / tree graph) | `packages/plan/plan-mode` (plan mode over the same loop) + `ctx.agentTeams` (task DAG) | Partial — plan mode isn't a separate loop; team DAG is a task-ready queue, not a plan tree |
| Research (further, with swarm) | Continuable subagents + `ctx.subagents` providers; or Agent Teams | Full — sibling teammates + one-shot providers |
| Execute via swarm | One-shot subagent providers (`spawn`, `fork`, `acp`, `codex`, `claude-code`, …) + teammates | Full |
| Register completion fact | Durable `tool/result` + `session/flush` checkpoint; `foldTeam()` for team-level | Full |
| Decide / provide output | Final `assistant/message` + `turn/end`; Lead's view of teammates | Full |
| Loop with exit criteria | `agent/turn-stopping` (serial, no `next()`) + typed `CancelCause` | Full |
| Risome-like structure | Continuable subagent roster (each as its own root) + task DAG | Partial — there is no explicit DAG composition runtime; topology is data-driven per spawn |
| Loop Engineering (act → observe → decide → repeat with exit) | Identical metaphor; dsh's `step → tool → next step` until `turn/end` | Full |
| Stop criteria | `agent/turn-stopping`, `CancelOptions.keepInbox`, typed `CancelCause` | Full — explicit, not policy-by-prompt |
| Tools, Skills, MCPs | `ctx.tools` registry + `tools/*` waterfalls; MCP is `packages/mcp` | Full |
| Filesystem / sandbox / browser | Capability-seam providers, all sharing the same execution world | Full |
| Hooks / middleware for deterministic steps | `agent/*` waterfalls and `tools/*` waterfalls | Full |
| Self-learning (hermes-inspired) | **Not a feature.** dsh is plug-in, not self-evolving. The plugin ecosystem is the growth path. | Gap |
| Dynamic tool creation by specialized subagent | Not present as a built-in — only via plugin authoring | Gap |
| Ontology / semi-structured memory | Session log is the memory; structure comes from event types. No ontology layer shipped. | Partial — structured by event types, no OWL/RDF |
| Native memory engine integration (e.g. openwolf) | Adapter seam exists for anything you write; nothing shipped | Gap (intentionally — bring your own) |
| CLI bundling | `apps/cli` (`bin.ts`, `args.ts`, `dump-config.ts`) | Full |
| UI Tool | `apps/web` (Vite + custom Web UI) | Full |
| Cheap-to-run | Provider-config driven; default composition is lean | Achievable |
| Feedback loop | Same loop / `tools/*` pipelines / `agent/request-error` retry hook | Full |

**Net**: dsh is the most directly comparable existing multi-agent harness to WHALE. Where dsh wins: deterministic seams, plugin composability, durable session log, scope model, continuation manager. Where dsh is silent: self-evolution, ontology memory, dynamic tool creation by an in-loop subagent (you ship this by writing a plugin). WHALE's "tree-like plan" and "self-learning" goals would map to plan-mode + a feedback/learning plugin on top of the same scaffold, not as new core runtime.
