"""Signal #7 — Per-project goal stack.

What the operator is *trying to do* on this project, right now. The recall
layer surfaces the most-recently-progressed active goal at SessionStart so a
new session can lead with "Last in-flight: <goal>. Continue?" instead of
asking the operator to re-state context cold.

The extractor emits two kinds of candidates:

1. ``goal`` — a fresh goal declaration. Detected via:
   * **Explicit markers** in user strings (``goal:``, ``trying to``,
     ``objective:``, ``the point is``, ``what we want to do is``,
     ``let's ship/launch/build/figure``, ``need to ship/launch/build``).
   * **First-message-of-session heuristic** — the FIRST user-string record
     after the ``permission-mode`` boundary is treated as a goal statement
     (high signal: the first thing the operator says when opening a fresh
     session is almost always "what I'm here to do today").

2. ``goal_progress`` — a progress marker that *links to* an existing goal.
   The :mod:`extractors.progress` extractor already mines ``Done.`` /
   ``Shipped`` / ``Committed`` / ``Fixed`` from assistant text; we re-emit
   them as ``goal_progress`` so the goals index can bump
   ``last_progress_ts`` without depending on the progress extractor's
   internal scoring.

Both kinds carry the raw goal/progress text as ``content`` and the source
session id as ``session_source`` in ``context`` so the goals index can
fold them into the ``goal_stack`` table without re-parsing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator

from extractors.base import (
    DagLike,
    Extraction,
    Extractor,
    RecordLike,
    get_assistant_text_blocks,
    get_user_string,
)

log = logging.getLogger(__name__)


# Explicit goal markers in user strings. Case-insensitive. We use `\b` at the
# leading edge only; the trailing edge varies per alternative (colon, space,
# word boundary) so a uniform `\b` at the end would fail for "goal:" (the
# char after ":" is a space, which doesn't form a word boundary with ":").
_GOAL_MARKER_RE = re.compile(
    r"(?i)\b("
    r"goal:|trying to\b|objective:|the point is\b|what we want to do is\b|"
    r"let'?s (ship|launch|build|figure)\b|"
    r"need to (ship|launch|build)\b"
    r")"
)

# Progress markers (mirror of `progress.py`, but we link them to goals).
# Anchored at paragraph start so we don't pick up "Done." inside narrative.
_PROGRESS_RE = re.compile(
    r"^(Done\.|All shipped\b|Shipped\b|Committed\b|Fixed\b)"
)

# "Still broken" / "blocked on" near a goal hints at blocked status.
_BLOCKED_RE = re.compile(r"(?i)\b(still broken|blocked on|stuck on|can'?t get past)\b")

# Explicit done markers in the goal text itself.
_DONE_INLINE_RE = re.compile(r"(?i)\b(done|shipped|landed|merged)\b")


_BASE_GOAL_SCORE = 0.65
_EXPLICIT_MARKER_BONUS = 0.15  # goal: / objective: / trying to ...
_FIRST_MESSAGE_BONUS = 0.1     # first user-string after permission-mode
_LONG_GOAL_BONUS = 0.05        # >40 chars (more specific = more useful)
_LONG_GOAL_THRESHOLD = 40

_BASE_PROGRESS_SCORE = 0.55


def _trim_goal_text(text: str, max_len: int = 240) -> str:
    """Collapse to one line, strip, cap at max_len.

    Goals are surfaced at SessionStart inline; they need to fit on one
    terminal row without wrapping mid-sentence.
    """
    one_line = " ".join(text.split())
    if len(one_line) > max_len:
        return one_line[: max_len - 1].rstrip() + "…"
    return one_line


# Trivial session openers / acks that are NOT goals. The first-message heuristic
# would otherwise emit these as goals, flooding the corpus with low-signal rows
# that dilute retrieval (the `goal` kind was the weakest in recall testing).
_LOW_SIGNAL_OPENER_RE = re.compile(
    r"(?i)^\s*("
    r"hi|hey|hello|yo|hiya|sup|gm|gn|morning|good morning|"
    r"ok|okay|k|kk|yes|yep|yeah|ya|no|nope|nah|sure|"
    r"thanks|thank you|ty|thx|cool|nice|great|"
    r"continue|carry on|go on|go ahead|proceed|resume|cont|next|"
    r"done|stop|wait|hold on"
    r")\b[\s\W]*$"
)
_MIN_FIRST_GOAL_LEN = 12  # chars, after whitespace-collapse


def _is_substantive_first_goal(text: str) -> bool:
    """Whether a first-of-session user string is substantive enough to count as
    a goal. Filters trivial openers/acks so the first-message heuristic doesn't
    flood the goal corpus. Explicit-marker goals bypass this check entirely.
    """
    t = " ".join(text.split())
    if len(t) < _MIN_FIRST_GOAL_LEN:
        return False
    return not _LOW_SIGNAL_OPENER_RE.match(t)


class Goals(Extractor):
    """Emit ``goal`` and ``goal_progress`` extractions.

    State carried across records: ``_seen_first_user`` tracks per-session
    whether we've already credited the first-user-after-permission-mode
    heuristic for that session. The extractor is otherwise stateless.
    """

    name = "goals"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        # Per-session: have we credited the first-user-string yet?
        # We treat the FIRST user-string after the (first) permission-mode
        # record for that session as a goal statement. If no permission-mode
        # is present we fall back to the first user-string outright — that's
        # still the highest-signal turn in a session.
        seen_first_user: set[str] = set()

        for rec in records:
            sid = getattr(rec, "session_id", None) or ""

            if getattr(rec, "type", None) == "permission-mode":
                continue

            # User-string path: goal declarations.
            user_text = get_user_string(rec)
            if user_text is not None:
                is_first = sid not in seen_first_user
                if is_first:
                    seen_first_user.add(sid)
                # The first user-string of a session is a strong goal signal,
                # but only when it's *substantive*: trivial openers ("hi",
                # "ok", "continue") would otherwise flood the goal corpus with
                # low-signal rows that dilute retrieval. Explicit-marker goals
                # are kept anywhere in the session, regardless of this gate.
                first_goal = is_first and _is_substantive_first_goal(user_text)

                marker_hit = bool(_GOAL_MARKER_RE.search(user_text))
                if not (marker_hit or first_goal):
                    continue

                trimmed = _trim_goal_text(user_text)
                if not trimmed:
                    continue

                score = _BASE_GOAL_SCORE
                if marker_hit:
                    score += _EXPLICIT_MARKER_BONUS
                if first_goal:
                    score += _FIRST_MESSAGE_BONUS
                if len(trimmed) >= _LONG_GOAL_THRESHOLD:
                    score += _LONG_GOAL_BONUS
                score = min(score, 1.0)

                ctx: dict = {
                    "source": "user_string",
                    "first_message": first_goal,
                    "marker": marker_hit,
                }
                if _BLOCKED_RE.search(user_text):
                    ctx["status_hint"] = "blocked"
                elif _DONE_INLINE_RE.search(user_text):
                    ctx["status_hint"] = "done"

                log.debug(
                    "goals: declaration session=%s uuid=%s marker=%s first=%s score=%.2f",
                    sid, rec.uuid, marker_hit, first_goal, score,
                )
                yield Extraction(
                    kind="goal",
                    content=trimmed,
                    session_id=sid,
                    cwd=rec.cwd,
                    ts=rec.ts,
                    source_uuid=rec.uuid,
                    score=score,
                    context=ctx,
                )
                continue

            # Assistant path: progress markers that should bump
            # last_progress_ts on whatever goal is active for this project.
            for block in get_assistant_text_blocks(rec):
                paragraphs = [p.strip() for p in block.split("\n\n") if p.strip()]
                for para in paragraphs:
                    if not _PROGRESS_RE.match(para):
                        continue
                    marker = para.split("\n", 1)[0][:120]
                    log.debug(
                        "goals: progress session=%s uuid=%s marker=%r",
                        sid, rec.uuid, marker,
                    )
                    yield Extraction(
                        kind="goal_progress",
                        content=marker,
                        session_id=sid,
                        cwd=rec.cwd,
                        ts=rec.ts,
                        source_uuid=rec.uuid,
                        score=_BASE_PROGRESS_SCORE,
                        context={"marker": marker, "source": "assistant"},
                    )
