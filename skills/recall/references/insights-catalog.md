# Insights catalog

The total-recall extractors classify every interesting transcript fragment into one of twelve **kinds** — 6 v0.1 generic + 6 v0.3 operator-aware. This doc describes each kind, shows real examples from the corpus, and gives the exact call that surfaces it.

The `kind` parameter is the single most powerful filter on `recall`. When you know what you're looking for, always set it.

| Kind | One-liner | Typical reuse |
| --- | --- | --- |
| `correction` | User overruling something Claude said or did. | Very high — these are the rules. |
| `decision` | An option was picked over alternatives, with rationale. | High — explains "why X not Y". |
| `self_correction` | Claude said "you're right, that was wrong" and changed course. | Medium — flags fragile reasoning. |
| `progress` | State markers — "deployed", "tests pass", "still need to do X". | High at session-resume time. |
| `domain_fact` | Stable truths about the user, infra, or codebase. | Highest — reused across every session. |
| `away_summary` | Claude-written narrative recap at session boundaries. | High — verbatim trustworthy. |

---

## correction

User pushing back on Claude. Often emotional, often profane, always high-signal. **Quote verbatim** — the tone is part of the data.

**Example 1 — cluster cwd:**

> User: *"no you fucking crazy — they are static ips"*

Context: Claude was about to `nmap` the LAN. The user runs a static-IP topology with `~/.ssh/config` entries. The correction is general — any future "discover the nodes" task should read the SSH config, not scan.

Surfacing call:

```
recall(topic="node discovery", kind="correction", scope="this_cwd", limit=3)
```

**Example 2 — global, provider preference:**

> User: *"stop suggesting that provider, my account got locked twice"*

This applies across every project. Surfaced via:

```
find_user_preferences(domain="provider")
```

or:

```
recall(topic="banned provider", kind="correction", scope="global", limit=3)
```

**How to act on a correction hit:** silently apply it. Don't say "I see you previously said…"; just don't make the mistake again. If you must explain why you're doing the un-default thing, attribute it: *"Going with the preferred provider here since the other one was off the table in the April session."*

---

## decision

A choice was made with stated rationale. The kind that answers "why did we go with X".

**Example 1 — goose release pipeline:**

> Decision: *"Use the manylinux2014 Docker base for the release build instead of cargo-zigbuild. zigbuild emitted glibc 2.34 symbols which broke Ubuntu 20.04 targets; manylinux2014 pins glibc 2.17 and the wheels we ship link cleanly."*

Surfacing call:

```
recall(topic="release container base", kind="decision", scope="this_cwd")
```

**Example 2 — network relay config:**

> Decision: *"WireGuard handshake retries set to 25s, not the default 5s. Default caused thundering-herd reconnects when the relay node cycled."*

Surfacing call:

```
recall(topic="wireguard handshake interval", kind="decision", scope="this_project")
```

**How to act:** treat as a *fact* about the past, not a rule for the future. If today's question revisits the decision, surface it and ask *"Still applies, or has the constraint changed?"* — don't enforce silently.

---

## self_correction

Claude wrote something, the user pointed out it was wrong, and Claude said *"you're right"* and changed course. These flag *fragile* reasoning — areas where the model's first instinct was wrong on this codebase.

**Example 1 — generic claim contradicted by repo reality:**

> Claude: *"`pyproject.toml` doesn't support optional dependency groups, you need a `requirements-dev.txt`."*
> User: *"yes it does, look at \[tool.poetry.group.dev]"*
> Claude: *"You're right, my mistake — Poetry's group syntax handles this. Updating."*

Surfacing call:

```
find_failed_attempts(topic="pyproject optional deps", scope="this_project")
```

**Example 2 — wrong API shape:**

> Claude tried `client.messages.create(...thinking=True...)` (invented param).
> User: *"that's not a real parameter, check the SDK"*
> Claude: *"You're right — the param is `thinking={'type': 'enabled', 'budget_tokens': N}`. Fixing."*

Surfacing call:

```
recall(topic="anthropic SDK thinking parameter", kind="self_correction", scope="global")
```

**How to act:** when you're about to make a similar call on the same area, *verify against live docs* (per the user's "verify before asserting" rule). Self-corrections mark known-fragile areas.

---

## progress

State markers. "Deployed", "tests pass", "still need to do X". The session-resume backbone.

**Example 1 — partial deploy:**

> Progress: *"relay-a deployed and handshaking; relay-b deployed but not yet verified; relay-c still pending."*

Surfacing call:

```
recall(topic="relay deploy status", kind="progress", scope="this_cwd", since="14d")
```

**Example 2 — test state:**

> Progress: *"Wrote `TestWebhookSignatureDrift` and `TestWebhookReplayWindow`. First passes, second fails on the 5-minute window — investigating clock skew on the build host."*

Surfacing call:

```
recall(topic="webhook signature tests", kind="progress", scope="this_cwd")
```

**How to act:** at session resume, this is the *first* thing you read after `away_summary`. Tells you where to pick up the hammer.

---

## domain_fact

Stable truths. The user's name, infra topology, account constraints, repo conventions. **Highest reuse across sessions** — read these once, apply forever.

**Example 1 — operator identity / infra:**

> Domain fact: *"The operator runs a monorepo on a self-hosted GitLab instance with CI auto-deploying on master push. Several VPS relays form the WireGuard fleet."*

Surfacing call:

```
recall(topic="operator infrastructure", kind="domain_fact", scope="global", limit=3)
```

**Example 2 — repo convention:**

> Domain fact: *"This repo uses `uv` for Python deps, not pip or poetry. `pyproject.toml` is the source of truth; `requirements*.txt` files do not exist here."*

Surfacing call:

```
recall(topic="python package manager", kind="domain_fact", scope="this_cwd")
```

**How to act:** apply silently. These are the rules of the world. Don't surface unless asked — they're load-bearing, not interesting.

---

## away_summary

Claude-written narrative recap at session boundaries (compaction, Stop hook, manual snapshot). The richest single field per session. **Verbatim trustworthy** — these were written when full context was in scope.

**Shape:** a markdown paragraph or two, 200-600 words, covering: working theory, in-flight task, files of interest, concrete next action.

Surfacing call — only through `get_session_digest`:

```
get_session_digest(session_id="…")
```

then read the `away_summary` field first.

**How to act:** at session resume after a specific past thread, the `away_summary` is the single most efficient context recovery. Read it before *any* other field.

---

---

# v0.3 operator-aware kinds

These six kinds are extracted with operator-context awareness — they pair user signal with the structural meaning (rejected approach, banned thing, active goal, pushback category, etc.). They have dedicated surfacing tools rather than going through generic `recall`.

## model_correction

Pairs user pushback with the rejected approach. Lets you see not just "user said no" but "user said no to *this specific thing*".

**Shape:** `{user_pushback, rejected_approach, topic, session_id, timestamp}`.

**Example:**

> rejected_approach: *"running nmap to discover cluster nodes"*
> user_pushback: *"no you fucking crazy — they are static ips"*

Surfacing call:

```
recall_corrections_about(topic="node discovery")
```

**How to act:** before suggesting an approach in a topic that has model_corrections, check that yours isn't isomorphic to a rejected one.

---

## standing_decision

Stable operator decisions like provider-a > provider-b, billing-a > billing-b, manylinux > zigbuild. Accumulates a "money-burn" score each time the same decision is re-violated — higher score = harder rule.

**Shape:** `{topic, decision, alternatives_rejected, rationale, money_burn_score, last_reaffirmed}`.

**Example:**

> topic: `payment_processor`
> decision: `billing-provider-a`
> alternatives_rejected: `[billing-provider-b, billing-provider-c]`
> rationale: *"billing-provider-b caused account issues; billing-provider-a works."*
> money_burn_score: 4

Surfacing call:

```
list_standing_decisions(topic="payment", scope="global")
get_decision_for_topic("hosting provider")
```

**How to act:** treat high money_burn as binding. Don't re-suggest the rejected alternatives even with new framing.

---

## ban

Banned providers, tools, or patterns. Distinct from `standing_decision` — bans are absolute ("never suggest X"), decisions are comparative ("prefer A over B").

**Shape:** `{thing, reason, first_seen, last_reaffirmed, intensity}`.

**Example:**

> thing: `banned-provider`
> reason: *"account locked twice"*
> intensity: `hard`

Surfacing call:

```
check_banned("banned-provider")
```

**How to act:** `banned: true` means stop. Don't soften, don't suggest with caveats — pick a different option silently.

---

## goal

Per-project goal stack with status state machine. Each cwd has a stack of goals in states `active` / `paused` / `done` / `abandoned`. Tracks what the operator is actually trying to accomplish *right now* in this project.

**Shape:** `{cwd, goal, status, created_at, last_progress, parent_goal?}`.

**Example:**

> cwd: `/home/operator/my-project`
> goal: *"ship v0.4 of agent binary with WireGuard handshake retry tunable"*
> status: `active`

Surfacing calls:

```
get_active_goal(cwd="/home/operator/my-project")
list_goals(cwd="/home/operator/my-project", status="active")
```

**How to act:** at session start, anchor your default working assumption to the active goal. If the user's first message conflicts with it, ask whether the goal changed.

---

## truth_assertion

The 7-category operator pushback taxonomy. Captures *how* the user is pushing back, not just *what* about. Categories:

| Category | Meaning |
| --- | --- |
| `restatement` | User restating what they already said because Claude missed it. |
| `quote_back` | User quoting Claude's prior wrong claim back at it. |
| `standing_rule` | User invoking a previously-stated rule. |
| `past_logs_appeal` | "Check the logs / transcripts / history." |
| `drift_callout` | "You are drifting" / "stay on task". |
| `capability_insult` | "Are you stupid" / "literally any junior dev". |
| `verify_yourself_push` | "Just run it" / "test it yourself". |

**Shape:** `{topic, category, quote, session_id, timestamp}`.

Surfacing call:

```
get_past_truth_assertions(topic="dns", category="drift_callout")
```

**How to act:** category tells you the protocol shift. `capability_insult` and `drift_callout` mean drop process narration and *act*. `verify_yourself_push` means run the command, paste output.

---

## term_definition

Operator vocabulary glossary. The operator's working definition of a term in *their* domain, not the generic meaning.

**Shape:** `{term, definition, examples, scope}`.

**Example:**

> term: `relay`
> definition: *"one of the WireGuard relay nodes that the agent binary connects through"*

Surfacing call:

```
define_term("relay")
```

**How to act:** when the user uses an ambiguous term, check `define_term` before answering. Avoids the failure mode where Claude answers about a generic concept when the user meant their project-specific component.

---

## Quick reference — kind → call

| Question | Call |
| --- | --- |
| "What did the user push back on around X?" | `recall(topic=X, kind="correction")` or `recall_corrections_about(topic=X)` |
| "Why did we pick X over Y?" | `recall(topic=X, kind="decision")` or `get_decision_for_topic(X)` |
| "Where was I fragile on this topic?" | `recall(topic=X, kind="self_correction")` or `find_failed_attempts(topic=X)` |
| "Where did we leave off?" | `recall(topic=X, kind="progress", since="14d")` |
| "What's stable about this user / infra?" | `recall(topic=X, kind="domain_fact", scope="global")` or `find_user_preferences()` |
| "Give me the narrative recap of session S." | `get_session_digest(session_id=S)` → read `away_summary` |
| "Is X banned?" | `check_banned("X")` |
| "What's the current goal here?" | `get_active_goal(cwd)` |
| "How is the user pushing back?" | `get_past_truth_assertions(topic, category)` |
| "What does the user mean by X?" | `define_term("X")` |
