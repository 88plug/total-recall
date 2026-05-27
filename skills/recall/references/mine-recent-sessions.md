# Mining recent sessions

Recipes for session-start orientation and resuming prior work via total-recall. Complements amnesia (which owns the acute, intra-session handoff).

## When this doc applies

- The `SessionStart` signpost mentioned "N prior memories in this cwd."
- The user opened a session with vague intent ("ok let's keep going on the thing").
- The user references "that session last week" without naming it.
- You're in a fresh cwd with no amnesia handoff but the cwd-slug has older sessions on disk.

Skip this doc when amnesia already injected a handoff — that handoff is fresher than anything `recall` can give you for the same session.

## Recipe 1 — Session-start orientation

Goal: in one call, get identity + active goal + bans + voice + recent corrections + machines. Then (if the user is vague about which thread) pick a session.

**Step 1 — the cheap one-call:**

```
get_operator_context(cwd="<current cwd>")
```

This is the SessionStart bundle. Use the returned active goal as your default working assumption for what the user wants to do. Apply the bans silently. Calibrate tone from the voice cheat sheet.

**Step 2 — only if thread-picking is needed:**

If the user is vague ("ok let's keep going on the thing") or referencing a specific past thread, fall back to:

```
prior_sessions_for_cwd(limit=5)
```

Then produce a 3-line summary. Format:

```
You have 3 recent sessions in this cwd:
  1. [2025-05-20]  wireguard relay handshake debugging  — last: "ok push it and let's see if relay picks up"
  2. [2025-05-18]  webhook signature drift              — last: "fine leave it, we'll revisit"
  3. [2025-05-15]  CI scheduling                       — last: "scheduled it for 03:00 UTC daily"
Which one are we continuing — or new thread?
```

Rules:

- One line per session. No paragraph.
- Truncate `last_prompt` at ~60 chars.
- If `turn_count < 5`, drop the session — it was probably a misfire.
- Don't surface sessions with `ai_title` starting "untitled" unless they're the only ones.
- Don't call `prior_sessions_for_cwd` if the active goal from `get_operator_context` already answers the question.

## Recipe 2 — Continue prior work

User says "yeah, keep going on the wireguard one."

```
get_session_digest(session_id="<id from recipe 1>")
```

Then in order:

1. **Read `away_summary` first.** It's the narrative recap and is verbatim trustworthy.
2. **Skim `progress` extractions** for state markers like "deployed to sfo1, not yet to lax1", "wrote test but hasn't passed yet."
3. **Use `last_prompt` as resume seed.** That's literally where the user stopped typing.
4. **Read `decisions` only if a current question revisits a settled one.** Don't dump them prophylactically.

Acknowledge resumption in one sentence — *"Picking up where we left off: sfo1 was deployed, lax1 was next, you'd written the test but hadn't run it."* — then act.

## Recipe 3 — Cross-session "have I done this before"

User says: *"I want to set up a Cloudflare tunnel for the new VPS."*

```
recall(topic="cloudflare tunnel setup", scope="global", limit=5)
```

If there's a prior session that solved this, get its digest:

```
get_session_digest(session_id="<top hit>")
```

Surface the *recipe* the user landed on, not the trial-and-error path that preceded it. The user already paid the cost of finding the working approach; deliver only that.

## Recipe 4 — Avoid repeating a corrected mistake

Before you suggest *anything* defaulty (cloud provider, package manager, formatting tool, branching strategy), run the bans check first — it's the cheapest answer.

**Step 1 — direct ban check:**

```
check_banned("<thing you're about to suggest>")
```

If `banned: true`, stop. Pick the un-banned alternative silently and move on.

**Step 2 — broader preference if step 1 returned `false`:**

```
find_user_preferences(domain="<area>")
```

If `domain` is ambiguous, omit it — the tool will return all preference rows. Apply the preference silently.

**Step 3 — past pushback lookup when you suspect a topic has history:**

```
recall_corrections_about(topic="<topic>")
```

Returns verbatim quotes from past corrections. Quote them back to the user only if you need to explain why you're going un-default; otherwise act silently.

Don't say "I checked your preferences and saw…" — just suggest the right thing the first time.

## Compaction boundaries — division of labor

```
                  amnesia                       total-recall
   ┌────────────────────────────┐       ┌────────────────────────────┐
   │ current session, current   │       │ everything older than the  │
   │ context window, acute      │       │ current session OR in a    │
   │ handoff across the most    │       │ different cwd. Historical  │
   │ recent compact_boundary.   │       │ breadth, not acute depth.  │
   └────────────────────────────┘       └────────────────────────────┘
       │                                    │
       └─── present ───── compact ──── past ┘
```

- Before the most recent `compact_boundary`: use amnesia's handoff or the JSONL walker for the *current* session; use `recall` for *other* sessions.
- For "yesterday in the same cwd": amnesia's handoff only covers the current session — yesterday's session is `recall` territory.
- For "10 minutes ago in this session": amnesia. Don't touch `recall`.

## Worked example — cluster static-IP discovery

**Context in the corpus:** in a past session for a cluster cwd, Claude tried to discover cluster nodes by scanning the LAN. The user interrupted with: *"no you fucking crazy — they are static ips"*. The actual topology was: fixed IPs documented in `~/.ssh/config`, no scanning needed.

**A future session in the same cwd should, before scanning anything:**

```
find_user_preferences(domain="topology")
recall(topic="cluster node discovery", kind="correction", scope="this_cwd", limit=3)
```

The correction surfaces; the assistant reads `~/.ssh/config` instead of nmap-ing the LAN; the user does not have to re-correct the same mistake.

**What NOT to do:** call `recall(topic="cluster")` with no `kind` and `scope="global"`. That returns dozens of hits unrelated to the topology question, costs tokens, and dilutes the actual signal.

## Anti-patterns

- **Calling `prior_sessions_for_cwd` every turn.** Once per session-start, not per turn.
- **Following up with `get_session_digest` on more than one session.** Pick the most likely candidate. If wrong, ask the user; don't fan out.
- **Surfacing the trial-and-error path.** The user wants the answer, not the journey. The journey is in the JSONL; spare them.
- **Treating an old decision as binding.** Decisions decay. If a 6-month-old decision conflicts with current code, flag the conflict — don't enforce the stale rule.
