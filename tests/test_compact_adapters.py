"""The harness adapters only use channels those harnesses actually document."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _code(text: str) -> str:
    """Drop comment lines so docs can name the channels the code must not use."""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//", "/*", "*", "*/")):
            continue
        kept.append(line)
    return "\n".join(kept)


def test_pi_extension_does_not_cancel_or_replace_summary() -> None:
    text = _code((ROOT / "adapters/pi/total-recall-compact.ts").read_text(encoding="utf-8"))
    assert "session_before_compact" in text
    assert "session_compact" in text
    assert "before_agent_start" in text
    assert "cancel: true" not in text
    assert "cancel:true" not in text
    assert "compaction:" not in text
    assert 'customType: "total-recall"' in text


def test_opencode_plugin_appends_context_and_does_not_replace_prompt() -> None:
    text = _code((ROOT / "adapters/opencode/total-recall-compact.js").read_text(encoding="utf-8"))
    assert "experimental.session.compacting" in text
    assert "output.context.push" in text
    assert "output.prompt" not in text
    assert "session.prompt" not in text
    assert '--source", "opencode"' in text or "--source opencode" in text


def test_goose_hooks_have_no_compaction_event() -> None:
    raw = json.loads((ROOT / "adapters/goose/hooks/hooks.json").read_text(encoding="utf-8"))
    events = set(raw["hooks"])
    assert events == {"Stop", "SessionEnd"}
    assert "PreCompact" not in events
    script = _code((ROOT / "adapters/goose/ingest-on-stop.sh").read_text(encoding="utf-8"))
    assert "exit 0" in script
    assert "decision" not in script
    assert "--source goose" in script


def test_codex_plugin_points_at_shared_hooks() -> None:
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["hooks"] == "./hooks/hooks.json"
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    assert any(group.get("matcher") == "compact" for group in hooks["hooks"]["SessionStart"])


def test_precompact_ingests_before_the_packet_and_never_blocks() -> None:
    text = _code((ROOT / "hooks/pre-compact-seed.sh").read_text(encoding="utf-8"))
    assert text.index("recall::ingest_transcript") < text.index("build_packet.py")
    assert "exit 2" not in text
    assert "continue:false" not in text
    assert "codex_precompact" in text
    restore = (ROOT / "hooks/session-start-compact-restore.sh").read_text(encoding="utf-8")
    assert "compact-recall" in restore
    assert "POST-COMPACTION CONTINUATION" in restore
