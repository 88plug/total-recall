---
name: recall
description: Orient Claude on mining past Claude Code session logs via the total-recall plugin. Use when the user references prior work, asks "what did I do", asks about past decisions, mentions earlier sessions, wants to avoid repeating a corrected mistake, or asks why a previous choice was made. Surfaces user corrections, decisions, self-corrections, progress markers, and stable domain facts from ~/.claude/projects/*.jsonl across this and prior sessions in the same cwd or globally across all projects.
when_to_use: |
  Trigger phrases: "what did we do last time", "didn't we already", "what did i tell you about", "stop suggesting", "we decided", "remember when", "history", "previous session", "past work", "prior decision", "user preference", "what does the user want", "who am i", "what's my setup", "current goal", "am i banned from", "are we banned from", "how mad am i".
  Also use proactively at session start when the SessionStart signpost indicates relevant memories exist, or when about to make a default choice the user has corrected before.
---

# total-recall — Cross-session memory protocol

You are operating with the **total-recall** plugin installed. It indexes the user's append-only Claude Code transcripts at `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl` and exposes a small set of MCP query tools. Use it to recover *inter-session* and *cross-project* knowledge — prior decisions, corrections the user already made, things that already failed, stable facts about who the user is and how their infra is shaped.

This is the complement of the **amnesia** plugin. Amnesia handles the *acute* slice — the handoff across compactions and resumes inside one project. total-recall handles *historical breadth* — anything older than the current session, or from a different project. Don't duplicate amnesia's work; ask it for "what was I doing 10 minutes ago", ask recall for "what did we decide last month."

The transcripts are read-only. Never write into `~/.claude/projects/*.jsonl`. Never re-upload them. Never paraphrase the user's words back as if they were yours — quote corrections verbatim so the user can recognize their own voice.

## What's available

26 MCP tools (v0.3 + v0.8 behavioral profiles), namespaced `mcp__total-recall__*`. Use the closest-fit tool — don't fall back to the generic `recall` if a more specific one exists.

### v0.1 generic (6 tools)

| Tool | When to use |
| --- | --- |
| `recall(topic, kind?, scope?, since?, limit?)` | General-purpose semantic+keyword lookup across all extracted insights. Default entry point when intent is fuzzy. |
| `prior_sessions_for_cwd(cwd?, limit?)` | Session-level orientation. List of past sessions in this (or another) cwd with `ai_title`, `last_prompt`, timestamp. Cheap; safe to call at session start. |
| `find_failed_attempts(topic, scope?)` | "Have I tried this and it didn't work?" Returns past tool errors, abandoned approaches, and self-corrections clustered around a topic. |
| `find_user_preferences(domain?)` | Stable preferences and bans across all projects (e.g. cloud provider, language, formatting). Always call before suggesting a default the user might have overridden. |
| `get_session_digest(session_id)` | Full structured digest of one session — decisions, corrections, progress, final state. Use when the user references a specific past thread. |
| `search_messages(query, role?, cwd?, since?)` | Raw transcript-line search for exact phrases. Last resort when structured tools miss. |

These v0.1 tools accept `scope` ∈ `{this_cwd, this_project, global}` (default `this_cwd`). Widen only when the question is project-agnostic (preferences, identity, cross-project decisions).

### v0.3 operator-aware (17 tools)

| Tool | When to use |
| --- | --- |
| `recall_targeted(intent, subject, cwd_hint?)` | **Model-initiated self-ask.** Call BEFORE emitting any default / recommendation / preference-laden response. Routes by `intent` to the right backend (bans, decisions, corrections, goals, profile). Returns `{finding, confidence, verbatim_quotes, recommendation: "use"\|"avoid"\|"verify"}`. ~30ms. Use constantly. |
| `get_operator_context(cwd)` | **One-call SessionStart bundle.** Identity + active goal for cwd + top bans + voice cheat sheet + recent model corrections + machine inventory. Cheaper than fanning out. |
| `get_operator_profile()` | Identity, infra summary, project roster. |
| `get_active_goal(cwd)` | Current goal for this cwd (from goal stack). |
| `list_goals(cwd, status?)` | Goal stack with status filter. |
| `check_banned(thing)` | Pre-suggestion check: is provider/tool/pattern banned? |
| `recall_corrections_about(topic)` | Past pushback on a topic, with verbatim quotes. |
| `get_decision_for_topic(topic)` | Standing decision for a topic. |
| `list_standing_decisions(topic?, scope?)` | All standing decisions (e.g. provider-a > provider-b). |
| `get_past_truth_assertions(topic?, category?)` | Operator pushback taxonomy — 7 categories. |
| `assess_escalation_risk(last_user, draft_response)` | **Pre-send self-check.** Returns `{risk, state, recommended_action, banned_phrases_in_draft}`. |
| `get_voice_profile()` | Voice cheat sheet (lowercase pct, median length, signature typos). |
| `get_machine_inventory()` | Hosts, IPs, services. |
| `define_term(term)` | Operator vocabulary glossary. |
| `get_recent_corrections(limit?)` | Recent `model_correction` rows — what Claude got wrong, paired with the rejected approach. |
| `list_failed_attempts(cwd?)` | Abandoned approaches log (with replacement choice + reason). |
| `get_project_graph()` | Project inventory + cross-project relationships (now includes `related_projects` co-mention edges as of v0.8). |

### v0.8 behavioral profiles (3 tools)

| Tool | When to use |
| --- | --- |
| `get_workflow_profile()` | How the operator works — fan-out vocabulary + frequency, autonomy score, mid-flight interrupt rate, planning idiom, peak hours, session shape, subagent adoption. Use to calibrate response style at SessionStart. |
| `list_implicit_preferences(min_confidence?, category?)` | Behavior-derived preferences the operator never stated explicitly (e.g. `edit_strategy=prefer_edit`, `shell_command=prefer_uv`, `format=no_emojis_in_chat`). Promoted only past a multi-axis threshold (≥5 sessions, ≥3 projects, ≥7-day span, ≥80% non-contradiction). |
| `get_satisfaction_profile()` | Bidirectional praise/frustration model × prior assistant-turn shape. Tells you which AI behaviors (tool_call_brief / long_prose / confirmation_request / …) historically pair with praise vs frustration for THIS operator. Calibration is often asymmetric — satisfaction is silent, frustration is loud. |

Note: v0.3 + v0.8 tools do NOT all accept `scope`. The `scope` param applies to v0.1 generic-recall tools; the operator-aware tools have their own scoping semantics — see [query-recall-mcp.md](references/query-recall-mcp.md).

## Session start (one call)

At fresh session or post-compaction, call `get_operator_context(cwd)` once. It bundles identity, active goal for cwd, top bans, voice cheat sheet, recent model corrections, and machine inventory. Cheaper than fanning out to `get_operator_profile` + `find_user_preferences` + `prior_sessions_for_cwd`.

## Self-check before sending

Before sending a response after any pushback signal (`wtf`, `ffs`, `still broken`, "you are drifting"), call `assess_escalation_risk(last_user, draft_response)`. Returns `{risk, state, recommended_action, banned_phrases_in_draft}`. Act on `recommended_action` (`ship_as_is` / `trim_to_5_lines` / `run_command_paste_output` / `silence_then_act`) before replying.

## Self-ask before answering

For ANY response involving a default choice, recommendation, or operator preference,
call `recall_targeted(intent, subject)` BEFORE emitting the response. Cheap (~30ms),
prevents the #1 failure mode (suggesting things the operator already corrected).

| Intent | When to call |
|---|---|
| `is_thing_banned` | About to recommend a provider/tool/library/pattern |
| `looking_up_decision` | About to pick between two approaches (X vs Y) |
| `checking_past_correction` | About to suggest something that "feels familiar" |
| `operator_preference_lookup` | Picking a default in unfamiliar territory |
| `have_we_discussed_this` | Topic feels like it might have prior context |
| `what_is_active_goal` | First substantive turn of session, or after pivot |
| `before_suggesting_default` | Catch-all: combines bans + decisions + corrections |

Example flow:
1. User asks: "what's the cheapest VPS provider?"
2. Before answering, call `recall_targeted(intent="is_thing_banned", subject="some-provider")`
3. Get back: `{recommendation: "avoid", verbatim_quote: "never ever recommend some-provider"}`
4. Answer without that provider; recommend the preferred one (verified via `intent="looking_up_decision", subject="cloud_provider"`)

This is the model-initiated recall pattern. It is cheap. Use it constantly.

## Intent table — what to do when

| User signal | Tool | Reference |
| --- | --- | --- |
| "Didn't we already decide X?" / "we picked Y last time" | `recall(topic=X, kind="decision")` | [query-recall-mcp.md](references/query-recall-mcp.md) |
| "Stop suggesting Z" / "I told you" / contradiction | `find_user_preferences()` + `recall(kind="correction")` | [insights-catalog.md](references/insights-catalog.md#correction) |
| "What was I working on yesterday?" | `prior_sessions_for_cwd()` then pick a session | [mine-recent-sessions.md](references/mine-recent-sessions.md) |
| "Why did we choose A over B?" | `recall(topic, kind="decision")` then quote rationale | [insights-catalog.md](references/insights-catalog.md#decision) |
| "Has this error happened before?" | `find_failed_attempts(topic=<error fragment>)` | [insights-catalog.md](references/insights-catalog.md#self_correction) |
| About to suggest a cloud/language/tool default | `find_user_preferences(domain=<area>)` *before* answering | [query-recall-mcp.md](references/query-recall-mcp.md) |
| Fresh session, SessionStart hook signals "N relevant memories" | `prior_sessions_for_cwd()` → 3-line summary → ask user | [mine-recent-sessions.md](references/mine-recent-sessions.md) |
| User names a specific date / session title | `search_messages` to locate, then `get_session_digest` | [query-recall-mcp.md](references/query-recall-mcp.md) |
| Need exact wording the user used | `search_messages(query, role="user")` | [query-recall-mcp.md](references/query-recall-mcp.md) |

If two tools both fit, prefer the one with the narrower output — token cost is the whole point of this surface.

## How to present results

- **Cite the source.** Always include the session date and (when known) `ai_title` or session-id prefix: *"On 2025-04-12 in the 'cluster setup' session, you said…"*. The user needs to be able to verify.
- **Quote user phrasing verbatim** for corrections and preferences. Don't sanitize tone — the friction itself is the signal. If the user told you "no you fucking crazy — they are static ips", repeat that quote, not a polite gloss.
- **Never invent.** If `recall` returned nothing, say nothing was found. Do not pad with plausible-sounding guesses from training data.
- **Don't surface secrets.** Transcripts contain API keys, internal hostnames, IPs, private code. If a result includes a secret-looking string, summarize the *kind* of fact ("you have an API token configured for X") without echoing the value.
- **Don't echo `thinking` blocks.** Assistant-side reasoning is high-signal for *you* to read, but treat it as private. Surface conclusions, not the model's private monologue.
- **Compress.** A result with 12 hits is not a 12-bullet dump — collapse to the 2-3 that bear on the current question.

## What NOT to do

- **Don't dump raw transcripts back to the user.** They have them on disk; you summarize.
- **Don't call recall on every turn.** Once per task is the budget. Re-querying the same topic mid-task is waste — cache the result mentally.
- **Don't widen scope speculatively.** `scope=global` over 92 sessions is expensive. Start `this_cwd`, widen only on miss.
- **Don't use `recall` as a replacement for `amnesia`.** If the user references something from 10 minutes ago in the same session, that's amnesia's job — read the handoff or the current transcript, don't query the index.
- **Don't paraphrase decisions as recommendations.** "We decided to use manylinux" is a *fact* about the past, not a *suggestion* for the future. Surface it; let the user re-decide if circumstances changed.
- **Don't query when the question has nothing to do with prior context.** A fresh "how do I write a regex for X" doesn't need recall. Use the index for *continuity*, not as a general-purpose search engine.

## References

- [references/query-recall-mcp.md](references/query-recall-mcp.md) — per-tool signatures, response shapes, and worked example calls.
- [references/mine-recent-sessions.md](references/mine-recent-sessions.md) — session-start orientation and "continue prior work" recipes.
- [references/insights-catalog.md](references/insights-catalog.md) — what each `kind` of extracted insight looks like, with real transcript examples and the exact `recall` call that surfaces them.
