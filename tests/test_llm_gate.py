"""Tests for `_text_refine_enabled` — the TOTAL_RECALL_LLM_REFINE_TEXT gate.

Semantics: default-on / opt-out.
  Unset / "1" / "true" / "yes" / "on"  → enabled
  "0" / "false" / "no" / "off"          → disabled
"""
from __future__ import annotations

import pytest

from total_recall.cmd_rebuild import _text_refine_enabled


@pytest.mark.parametrize("env_value", [None, "1", "true", "True", "TRUE", "yes", "on"])
def test_enabled_by_default_and_truthy_values(env_value: str | None) -> None:
    assert _text_refine_enabled(env_value) is True


@pytest.mark.parametrize("env_value", ["0", "false", "False", "FALSE", "no", "off"])
def test_disabled_by_explicit_falsy_value(env_value: str) -> None:
    assert _text_refine_enabled(env_value) is False
