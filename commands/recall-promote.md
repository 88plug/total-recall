---
description: Promote a recall finding (correction/preference/domain fact) into project auto-memory so it survives without needing recall.
argument-hint: <topic>
---

Steps:
1. Treat $ARGUMENTS as a topic and call `recall(topic="$ARGUMENTS", limit=3)` via the MCP tool. Pick the highest-scoring hit.
2. Show candidate: kind / cwd / verbatim content.
3. Ask: promote to project `MEMORY.md` (cwd-scoped) or global `~/.claude/CLAUDE.md` (if scope=global)?
4. On confirmation: write `~/.claude/projects/<cwd-slug>/memory/<kind>_<short-slug>.md` with frontmatter `name`, `description`, `metadata.type` (feedback|project|reference), body `**Fact:** ... **Why:** ... **How to apply:** ...`. Add one-line entry to `MEMORY.md` index.

The existing auto-memory system at `~/.claude/projects/<slug>/memory/` is hand-curated — match conventions exactly. Reference: `~/.claude/projects/-home-operator-my-project/memory/MEMORY.md`.
