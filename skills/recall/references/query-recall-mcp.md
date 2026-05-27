# Querying the total-recall MCP

Per-tool signatures, response shapes, and worked examples. All tools are namespaced `mcp__total-recall__*`; this doc drops the prefix for readability.

## Shared parameters

| Param | Values | Default | Notes |
| --- | --- | --- | --- |
| `scope` | `this_cwd` \| `this_project` \| `global` | `this_cwd` | `this_project` widens to all cwds whose slug shares the project's root dir (e.g. monorepo subpackages). `global` searches all ~92 sessions — expensive. |
| `since` | ISO date or `"7d"`, `"30d"`, `"all"` | `"all"` | Drop ancient sessions when intent is recency-biased. |
| `limit` | int | 10 | Hard cap on returned hits. Keep low. |
| `kind` | see [insights-catalog.md](insights-catalog.md) | unset (any) | Tightens recall to one insight type. |

> **Note:** v0.3 tools do NOT all accept `scope` — that param applies to the v0.1 generic-recall tools (`recall`, `find_user_preferences`, `find_failed_attempts`, `search_messages`). v0.3 operator-aware tools have their own scoping semantics; see per-tool sections below.

---

## `recall(topic, kind?, scope?, since?, limit?)`

General-purpose retrieval. Hybrid keyword + embedding. Returns ranked insight records.

**Response shape (per hit):**

```json
{
  "kind": "decision",
  "text": "Use manylinux2014 container instead of cargo-zigbuild — zigbuild produced glibc 2.34 symbols that broke Ubuntu 20.04 targets.",
  "session_id": "0f3a…",
  "session_title": "goose release pipeline rework",
  "cwd": "/home/operator/goose",
  "timestamp": "2025-03-18T22:11:04Z",
  "score": 0.81,
  "source_uuid": "…"
}
```

**Example 1 — user says "didn't we already decide on this":**

```
recall(topic="release container base image", kind="decision", scope="this_cwd", limit=3)
```

Then quote: *"On 2025-03-18 you decided manylinux2014 over cargo-zigbuild — zigbuild's glibc 2.34 symbols broke Ubuntu 20.04. Still relevant?"*

**Example 2 — about to suggest a provider the operator may have banned:**

```
recall(topic="hosting provider ban", kind="correction", scope="global", limit=5)
```

If it returns a hit, the user has already banned that provider somewhere. Prefer `find_user_preferences(domain="provider")` for this case — it's purpose-built.

**Example 3 — narrowing on a miss:**

If `recall(topic="logging", scope="this_cwd")` returns nothing useful, widen one step (`scope="this_project"`) rather than jumping to `global`. If still nothing, the right answer is "no prior context" — stop, don't invent.

---

## `prior_sessions_for_cwd(cwd?, limit?)`

Session-level orientation. Cheap. Safe at session start.

**Response shape:**

```json
[
  {
    "session_id": "8c12…",
    "ai_title": "wireguard relay handshake debugging",
    "last_prompt": "ok push it and let's see if the relay picks up",
    "started_at": "2025-05-20T14:02:11Z",
    "ended_at":   "2025-05-20T17:48:55Z",
    "turn_count": 134,
    "compaction_count": 2
  }
]
```

**Example:**

```
prior_sessions_for_cwd(limit=5)
```

Use the returned `ai_title` and `last_prompt` to produce a 3-line summary for the user: *"Your last 3 sessions here were: (1) wireguard relay handshake, last said 'ok push it'; (2) CI scheduling, …; (3) webhook signature. Pick one to resume?"*

---

## `find_failed_attempts(topic, scope?)`

Clusters errors, abandoned approaches, and self-corrections. Use before retrying anything.

**Response shape:** same as `recall` but pre-filtered to `kind ∈ {self_correction, correction}` plus tool-error context lines.

**Example — "the LAN scan keeps timing out":**

```
find_failed_attempts(topic="LAN scan timeout", scope="this_cwd")
```

Might return: *"On 2025-04-10 user said 'no you fucking crazy — they are static ips'. The LAN scan approach was wrong; nodes were reachable via fixed IPs documented in `~/.ssh/config`."* → stop scanning, read the SSH config first.

---

## `find_user_preferences(domain?)`

Stable across-project preferences. **Call this before suggesting a default.** Cheap and cacheable.

**Domains observed:** `provider`, `language`, `editor`, `shell`, `formatter`, `git_workflow`, `secrets_manager`, `os`, `package_manager`.

**Response shape:**

```json
[
  {
    "domain": "provider",
    "preference": "provider-a preferred; banned-provider banned (account locked twice).",
    "first_seen": "2024-11-02",
    "last_reaffirmed": "2025-05-08",
    "session_refs": ["…", "…"]
  }
]
```

**Example — before suggesting a deploy target:**

```
find_user_preferences(domain="provider")
```

---

## `get_session_digest(session_id)`

Full structured digest of one session. Use only when the user names a specific past thread, or after `prior_sessions_for_cwd` returned a candidate.

**Response shape:**

```json
{
  "session_id": "…",
  "ai_title": "…",
  "decisions": [...],
  "corrections": [...],
  "progress": [...],
  "domain_facts": [...],
  "away_summary": "Markdown narrative recap written by Claude at session end.",
  "final_state": "…"
}
```

**Example:**

```
get_session_digest(session_id="8c12abcd-…")
```

Read `away_summary` first — it's the narrative recap and the highest-fidelity single field.

---

## `search_messages(query, role?, cwd?, since?)`

Raw line-level search. Last resort. Returns transcript lines, not extracted insights.

**Example — user remembers an exact phrase but not the context:**

```
search_messages(query="exposed via cloudflared tunnel", role="user")
```

**Response shape:** array of `{session_id, timestamp, role, text_excerpt, source_uuid}`. Follow up with `get_session_digest` on the most relevant hit.

---

---

# v0.3 operator-aware tools

These wrap the operator-context index built on top of the raw transcript extractions. They are cheaper than generic `recall` for the questions they specifically answer.

## `get_operator_context(cwd)`

**The unified one-call SessionStart bundle.** Returns identity, active goal for the cwd, top bans, voice cheat sheet, recent model corrections, and machine inventory in a single response. Use this once per fresh session or post-compaction instead of fanning out to `get_operator_profile` + `find_user_preferences` + `get_voice_profile` + `prior_sessions_for_cwd`.

```
get_operator_context(cwd="/home/operator/my-project")
```

If you only need session-thread orientation (which past thread to resume), follow up with `prior_sessions_for_cwd`. Don't re-call this within the same session — it's stable.

## `check_banned(thing)`

Pre-suggestion check. Pass any provider/tool/pattern name; returns `{banned: bool, reason?, first_seen?, last_reaffirmed?}`. Call this *before* suggesting a default in your reply.

```
check_banned("banned-provider")
check_banned("banned-payment-processor")
check_banned("emoji in commit messages")
```

A `true` here means stop — don't suggest it, don't even soften-suggest it. Pick the un-banned alternative silently.

## `recall_corrections_about(topic)`

Past pushback on a topic, with verbatim user quotes. Returns paired (rejected_approach, user_correction) records. Use when you suspect you're about to repeat a mistake but want to confirm.

```
recall_corrections_about("LAN scanning")
recall_corrections_about("cloud provider")
```

Quote the user back to themselves so they recognize their own correction — don't paraphrase.

## `get_decision_for_topic(topic)` and `list_standing_decisions(topic?, scope?)`

`get_decision_for_topic` returns the *current* standing decision for a single topic (e.g. "hosting provider" → "provider-a > provider-b"). `list_standing_decisions` returns the full list, optionally filtered by topic substring or scope.

```
get_decision_for_topic("payment processor")
list_standing_decisions(scope="global")
list_standing_decisions(topic="container", scope="this_project")
```

Standing decisions accumulate a "money-burn" score (how much wasted time/money each repeat-violation has cost). Higher score = louder pushback last time = treat as harder rule.

## `assess_escalation_risk(last_user, draft_response)`

**Pre-send self-check.** Lives in the `detector/` namespace, not `extras/`. Pass the last user message and your *draft* response; returns:


```json
{
  "risk": "low" | "medium" | "high" | "critical",
  "state": "calm" | "frustrated" | "angry" | "nuclear",
  "recommended_action": "ship_as_is" | "trim_to_5_lines" | "run_command_paste_output" | "silence_then_act",
  "banned_phrases_in_draft": ["...", "..."]
}
```

```
assess_escalation_risk(
  last_user="still broken ffs",
  draft_response="I understand your frustration. Let me explain what I did..."
)
```

Act on `recommended_action` *before* sending. If `banned_phrases_in_draft` is non-empty, rewrite to remove them.

## `get_active_goal(cwd)`

Returns the currently-active goal from the cwd's goal stack — what the operator is actually trying to accomplish in this project. Use at session start (or call `get_operator_context` instead, which bundles this).

```
get_active_goal(cwd="/home/operator/my-project")
```

Goals have a status state machine (`active` / `paused` / `done` / `abandoned`). Only `active` goals come back here; use `list_goals(cwd, status=...)` to see the full stack.

## `get_operator_profile()`

Identity + infra summary + project roster. One call, no params. Stable per session — read once.

```
get_operator_profile()
```

## `get_voice_profile()`

Voice cheat sheet: lowercase pct, median user-turn length, top first-words, signature typos. Use to calibrate tone-matching. Bundled inside `get_operator_context` — don't double-call.

```
get_voice_profile()
```

## `get_machine_inventory()`

Hosts, IPs, services, role. Use when about to suggest a deploy target or reference an existing machine.

```
get_machine_inventory()
```

## `define_term(term)`

Operator vocabulary glossary. Returns the operator's working definition of a term as used in *their* domain (not Wikipedia). Use when a term in the user's message could have multiple interpretations.

```
define_term("relay")
define_term("agent")
```

---

## Narrowing strategy

When the first call returns too much noise:

1. Tighten `kind` (e.g. `decision` only).
2. Narrow `scope` from `global` → `this_project` → `this_cwd`.
3. Add `since="30d"` to drop ancient hits.
4. Lower `limit` and re-rank — top 3 with reasoning beats top 10 with no judgment.

When it returns nothing:

1. Widen `scope` one step.
2. Drop `kind` filter.
3. Try `search_messages` with a tighter exact phrase.
4. Accept "no prior context" — don't fabricate.
