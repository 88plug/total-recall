"""MCP tool surface for the escalation detector.

Importing this module registers :func:`assess_escalation_risk` on the
shared :data:`mcp_server.server.mcp` instance. It is a thin adapter: the
real logic lives in :func:`detector.escalation.assess_escalation`. We
keep the wire-level concerns (docstring shown to the model, dict return
type) here so the detector module stays free of MCP imports.
"""

from __future__ import annotations

from detector.escalation import assess_escalation
from mcp_server.server import mcp
from mcp.types import ToolAnnotations


@mcp.tool(title="Assess Escalation Risk", annotations=ToolAnnotations(readOnlyHint=True))
def assess_escalation_risk(
    last_user: str,
    previous_user: str | None = None,
    draft_response: str | None = None,
) -> dict:
    """Numerically assess operator frustration level from their last turn.

    Use BEFORE finalizing a response, especially after any operator
    pushback. Returns {risk, state, triggers, recommended_action,
    banned_phrases_in_draft}. Recommended actions: 'ship_as_is',
    'trim_to_5_lines', 'run_command_paste_output', 'silence_then_act'.
    Risk >= 4 means stop talking and run the verifying command instead
    of explaining.
    """
    return assess_escalation(
        last_user=last_user,
        previous_user=previous_user,
        draft_response=draft_response,
    ).to_dict()
