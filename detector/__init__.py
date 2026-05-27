"""Heuristic detectors that score live conversational signals.

This package houses pure-Python, stdlib-only detectors that can be called
mid-turn (from MCP tools, hooks, or other in-process surfaces) to produce
numeric assessments of operator state. Detectors here must:

* depend only on the standard library (no DB, no network),
* be deterministic and side-effect-free,
* return small ``@dataclass`` results suitable for JSON serialization.

Currently exported:

* :func:`detector.escalation.assess_escalation` — rule-based scorer that
  estimates operator frustration from the last user turn and (optionally)
  the model's draft reply.
"""

from detector.escalation import EscalationAssessment, assess_escalation

__all__ = ["EscalationAssessment", "assess_escalation"]
