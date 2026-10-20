/**
 * Pi extension: ingest the live session before compaction, then inject a
 * real recall on the next agent run.
 *
 * session_before_compact runs before Pi summarizes. This handler returns
 * undefined. A cancel result would cancel compaction. A custom summary
 * would replace Pi's own summarizer.
 *
 * session_compact fires only after compaction succeeds. The following
 * before_agent_start is the post-compact inject. before_agent_start has no
 * other channel; a returned custom message is the documented way to add
 * context to that run.
 *
 * Load: `pi --extension /path/to/total-recall/adapters/pi/total-recall-compact.ts`
 * or copy this file to `~/.pi/agent/extensions/`.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function projectRoot(): string {
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

function dbArgs(): string[] {
  if (process.env.TOTAL_RECALL_DB) return ["--db", process.env.TOTAL_RECALL_DB];
  if (process.env.TOTAL_RECALL_DB_DIR) {
    return ["--db", join(process.env.TOTAL_RECALL_DB_DIR, "index.db")];
  }
  return [];
}

function launch(args: string[]): { cmd: string; argv: string[] } {
  const tail = ["-m", "total_recall", ...dbArgs(), ...args];
  if (process.env.TOTAL_RECALL_PY) {
    return { cmd: process.env.TOTAL_RECALL_PY, argv: tail };
  }
  const root = projectRoot();
  if (root) {
    return {
      cmd: "uv",
      argv: ["run", "--project", root, "--python", ">=3.10", "python", ...tail],
    };
  }
  return { cmd: "python3", argv: tail };
}

function run(args: string[]): string {
  const spec = launch(args);
  const result = spawnSync(spec.cmd, spec.argv, {
    encoding: "utf8",
    env: process.env,
  });
  if (result.error || result.status) return "";
  return (result.stdout || "").trim();
}

export default function totalRecallCompact(pi: ExtensionAPI) {
  let recallPending = false;

  pi.on("session_before_compact", async (_event, ctx) => {
    const file = ctx.sessionManager.getSessionFile();
    if (!file) return;
    const sessionId = ctx.sessionManager.getSessionId();
    const args = ["index", "--file", file, "--source", "pi"];
    if (sessionId) args.push("--session", sessionId);
    run(args);
  });

  pi.on("session_compact", async () => {
    recallPending = true;
  });

  pi.on("session_compact_failed", async () => {
    recallPending = false;
  });

  pi.on("before_agent_start", async (_event, ctx) => {
    if (!recallPending) return;
    recallPending = false;
    const args = ["compact-recall", "--cwd", ctx.cwd, "--max-chars", "2500"];
    const sessionId = ctx.sessionManager.getSessionId();
    if (sessionId) args.push("--session", sessionId);
    const text = run(args);
    if (!text) return;
    return {
      message: {
        customType: "total-recall",
        content: text,
        display: true,
      },
    };
  });
}
