"""``total-recall llm-model`` — print the auto-selected (or env-overridden) LLM model tag.

This is a single source of truth for bash provisioners: call
``total-recall llm-model`` to know which model to ``ollama pull`` before
running a rebuild with LLM refinement enabled.

Output is a single bare line, e.g.::

    qwen3.5:9b
"""

from __future__ import annotations

import os

import click


@click.command("llm-model", help="Print the auto-selected local LLM model tag (single line).")
def llm_model_cmd() -> None:
    """Emit the model tag that get_default_client() will use.

    Respects TOTAL_RECALL_LLM_MODEL env override; otherwise calls
    autoselect_model() to probe free RAM and disk and pick the best tier.
    """
    # Honour explicit override first — same logic as get_default_client().
    tag = os.environ.get("TOTAL_RECALL_LLM_MODEL")
    if not tag:
        from extractors.llm.client import autoselect_model
        tag = autoselect_model()
    click.echo(tag)
