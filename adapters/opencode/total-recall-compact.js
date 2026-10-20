/**
 * OpenCode plugin: ingest the session, then append a real recall to the
 * compaction prompt.
 *
 * experimental.session.compacting fires before the summary. output.context
 * is appended to the default prompt. Setting output.prompt replaces that
 * prompt and drops output.context — this plugin never sets output.prompt.
 *
 * session.compacted fires after the summary. OpenCode does not inject
 * additionalContext on that event, and session.prompt would start a new
 * turn. The recall is therefore carried out through the summary prompt.
 *
 * Install: copy this file to ~/.config/opencode/plugins/ or .opencode/plugins/.
 * Runs `uv run --project` against the marketplace checkout when
 * `python3 -m total_recall` is not installed. TOTAL_RECALL_PY overrides.
 * TOTAL_RECALL_DB or TOTAL_RECALL_DB_DIR selects the index.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

function projectRoot() {
  for (const candidate of [process.env.TOTAL_RECALL_ROOT, process.env.CLAUDE_PLUGIN_ROOT]) {
    if (candidate && existsSync(join(candidate, "pyproject.toml"))) return candidate;
  }
  const base = join(homedir(), ".claude/plugins/cache/88plug/total-recall");
  if (!existsSync(base)) return "";
  let best = "";
  let bestMtime = 0;
  for (const name of readdirSync(base)) {
    const dir = join(base, name);
    if (!existsSync(join(dir, "pyproject.toml"))) continue;
    const mtime = statSync(dir).mtimeMs;
    if (mtime >= bestMtime) {
      best = dir;
      bestMtime = mtime;
    }
  }
  return best;
}

function dbArgs() {
  if (process.env.TOTAL_RECALL_DB) return ["--db", process.env.TOTAL_RECALL_DB];
  if (process.env.TOTAL_RECALL_DB_DIR) {
    return ["--db", join(process.env.TOTAL_RECALL_DB_DIR, "index.db")];
  }
  return [];
}

function run(args) {
  const tail = ["-m", "total_recall", ...dbArgs(), ...args];
  let cmd = "python3";
  let argv = tail;
  if (process.env.TOTAL_RECALL_PY) {
    cmd = process.env.TOTAL_RECALL_PY;
  } else {
    const root = projectRoot();
    if (root) {
      cmd = "uv";
      argv = ["run", "--project", root, "--python", ">=3.10", "python", ...tail];
    }
  }
  try {
    const result = spawnSync(cmd, argv, { encoding: "utf8", env: process.env });
    if (result.error || result.status) return "";
    return (result.stdout || "").trim();
  } catch {
    return "";
  }
}

export const TotalRecallCompact = async ({ directory, worktree }) => {
  const cwd = directory || worktree || process.cwd();

  async function ingest(sessionId) {
    if (!sessionId) return;
    run(["index", "--source", "opencode", "--session", sessionId]);
  }

  async function recallText(sessionId) {
    const args = ["compact-recall", "--cwd", cwd, "--max-chars", "2500"];
    if (sessionId) args.push("--session", sessionId);
    return run(args);
  }

  return {
    "experimental.session.compacting": async (input, output) => {
      const sessionId = (input && (input.sessionID || input.sessionId)) || "";
      await ingest(sessionId);
      const text = await recallText(sessionId);
      if (text && output && Array.isArray(output.context)) {
        output.context.push(text);
      }
    },
    event: async ({ event }) => {
      // Observation only. There is no post-compact inject channel.
      if (!event || event.type !== "session.compacted") return;
    },
  };
};
