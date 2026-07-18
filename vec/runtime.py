"""Product-owned ollama runtime for dense embeds (+ optional chat refine).

Design: total-recall **owns** the ollama binary under the plugin data dir
(``$CLAUDE_PLUGIN_DATA/total-recall/bin/ollama``), starts ``ollama serve`` if
needed, and pulls models. System PATH ollama is a fallback, not the product
story. Python talks HTTP to that daemon — same process LLM refine uses.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"
RECOMMENDED_EMBED = "qwen3-embedding:0.6b"
RECOMMENDED_CHAT = "qwen3.5:2b"

_PROBE_TIMEOUT_S = 3.0
_SERVE_WAIT_S = 15.0
_PULL_TIMEOUT_S = 1800.0  # large models on slow links


def data_root() -> Path:
    """Plugin data root for total-recall (matches hooks/lib/common.sh)."""
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or str(
        Path.home() / ".claude" / "plugins" / "data"
    )
    override = os.environ.get("TOTAL_RECALL_DB_DIR") or os.environ.get(
        "TOTAL_RECALL_DATA_ROOT"
    )
    if override:
        # TOTAL_RECALL_DB_DIR is often .../total-recall — use as data root when
        # it already ends with total-recall, else parent for index-only paths.
        p = Path(override).expanduser()
        if p.name == "total-recall":
            return p
        return p
    return Path(base) / "total-recall"


def base_url() -> str:
    return (os.environ.get("TOTAL_RECALL_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def embed_model_tag() -> str:
    raw = (os.environ.get("TOTAL_RECALL_EMBED_MODEL") or RECOMMENDED_EMBED).strip()
    if not raw:
        return RECOMMENDED_EMBED
    if "/" in raw and not raw.startswith("http"):
        return RECOMMENDED_EMBED
    low = raw.lower().split(":")[0]
    if low in {
        "gte-modernbert-base",
        "gte-modernbert",
        "bge-small-en-v1.5",
        "bge-small-en",
    }:
        return RECOMMENDED_EMBED
    return raw


def chat_model_tag() -> str:
    return (os.environ.get("TOTAL_RECALL_LLM_MODEL") or RECOMMENDED_CHAT).strip()


def want_embed() -> bool:
    v = (os.environ.get("TOTAL_RECALL_VEC") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def want_chat() -> bool:
    return (os.environ.get("TOTAL_RECALL_LLM_PROVIDER") or "auto").strip().lower() != "none"


def daemon_reachable(url: str | None = None, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    u = (url or base_url()).rstrip("/")
    try:
        req = urllib.request.Request(f"{u}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(64)
        return True
    except Exception:  # noqa: BLE001
        return False


def list_model_names(url: str | None = None) -> list[str] | None:
    u = (url or base_url()).rstrip("/")
    try:
        req = urllib.request.Request(f"{u}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return []
    out: list[str] = []
    for m in models:
        if isinstance(m, dict) and m.get("name"):
            out.append(str(m["name"]))
    return out


def model_present(tag: str, names: list[str] | None = None) -> bool:
    if names is None:
        names = list_model_names() or []
    if tag in names:
        return True
    latest = tag if ":" in tag else f"{tag}:latest"
    if latest in names:
        return True
    # strip :latest for fuzzy match
    base = tag.split(":")[0]
    return any(n == base or n.startswith(base + ":") for n in names)


def resolve_ollama_bin() -> Path | None:
    """Resolve product-owned binary first, then PATH."""
    env = (os.environ.get("RECALL_OLLAMA") or "").strip()
    if env:
        p = Path(env)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    bundled = data_root() / "bin" / "ollama"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled
    which = shutil.which("ollama")
    if which:
        return Path(which)
    snap = Path("/snap/bin/ollama")
    if snap.is_file() and os.access(snap, os.X_OK):
        return snap
    return None


def _plugin_root() -> Path | None:
    env = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()
    if env and (Path(env) / "hooks" / "lib" / "common.sh").is_file():
        return Path(env)
    # repo layout: vec/runtime.py → repo root
    here = Path(__file__).resolve().parent.parent
    if (here / "hooks" / "lib" / "common.sh").is_file():
        return here
    return None


def _try_bash_provision() -> bool:
    """Invoke hooks/lib/common.sh recall::provision_llm (download+serve+pull)."""
    root = _plugin_root()
    if root is None:
        return False
    common = root / "hooks" / "lib" / "common.sh"
    env = os.environ.copy()
    # Ensure data root aligns with hooks
    env.setdefault(
        "CLAUDE_PLUGIN_DATA",
        str(data_root().parent) if data_root().name == "total-recall" else str(data_root()),
    )
    cmd = f"source {common.as_posix()!r} && recall::provision_llm"
    try:
        subprocess.run(
            ["bash", "-c", cmd],
            env=env,
            timeout=_PULL_TIMEOUT_S,
            check=False,
            capture_output=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("bash provision failed: %s", exc)
        return False
    return daemon_reachable()


def _product_serve_env() -> dict[str, str]:
    """Env for product ``ollama serve``: GPU + MTP defaults."""
    env = os.environ.copy()
    env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
    env.setdefault("OLLAMA_KEEP_ALIVE", "-1")
    env.setdefault("OLLAMA_MAX_LOADED_MODELS", "4")
    env.setdefault("OLLAMA_NUM_PARALLEL", "4")
    env.setdefault("OLLAMA_MAX_QUEUE", "2048")
    # MTP draft depth (MLX). CUDA/llama-server auto-uses built-in mtp.* tensors
    # on models like qwen3.5:2b (no extra env required, but harmless).
    env.setdefault("OLLAMA_MLX_MTP_MAX_DRAFT_TOKENS", "4")
    env.setdefault("OLLAMA_MLX_MTP_INITIAL_DRAFT_TOKENS", "4")
    return env


def start_daemon(bin_path: Path, url: str | None = None) -> bool:
    if daemon_reachable(url):
        return True
    log_dir = data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "llm-provision.log"
    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(f"\n[python-runtime] starting {bin_path} serve (GPU+MTP env)\n")
            proc = subprocess.Popen(  # noqa: S603
                [str(bin_path), "serve"],
                stdout=lf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=_product_serve_env(),
            )
        log.info("started ollama serve pid=%s", proc.pid)
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to start ollama serve: %s", exc)
        return False
    deadline = time.time() + _SERVE_WAIT_S
    while time.time() < deadline:
        if daemon_reachable(url):
            return True
        time.sleep(0.5)
    return daemon_reachable(url)


def pull_model(bin_path: Path, tag: str) -> bool:
    names = list_model_names()
    if names is not None and model_present(tag, names):
        return True
    try:
        r = subprocess.run(  # noqa: S603
            [str(bin_path), "pull", tag],
            timeout=_PULL_TIMEOUT_S,
            check=False,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            log.warning("ollama pull %s failed: %s", tag, (r.stderr or r.stdout or "")[:400])
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("ollama pull %s error: %s", tag, exc)
        return False
    names2 = list_model_names()
    return names2 is not None and model_present(tag, names2)


def ensure_product_ollama(
    *,
    embed: bool | None = None,
    chat: bool | None = None,
    pull: bool = True,
) -> dict[str, Any]:
    """Ensure product ollama is serving and required models are present.

    Returns a status dict. Raises RuntimeError only when *required* pieces
    cannot be brought up (embed requested but unavailable). Soft layers
    (chat) log and continue.
    """
    need_embed = want_embed() if embed is None else embed
    need_chat = want_chat() if chat is None else chat
    status: dict[str, Any] = {
        "base_url": base_url(),
        "embed": need_embed,
        "chat": need_chat,
        "daemon": False,
        "embed_model": embed_model_tag() if need_embed else None,
        "chat_model": chat_model_tag() if need_chat else None,
        "embed_ready": False,
        "chat_ready": False,
        "bin": None,
    }
    if not need_embed and not need_chat:
        return status

    url = base_url()
    if not daemon_reachable(url):
        bin_path = resolve_ollama_bin()
        if bin_path is None:
            if not _try_bash_provision():
                if need_embed:
                    raise RuntimeError(
                        "Product ollama is not available (no binary, daemon down). "
                        "total-recall auto-provisions under the plugin data dir on "
                        "first session; or run: bash scripts/llm-setup.sh"
                    )
                return status
        else:
            status["bin"] = str(bin_path)
            if not start_daemon(bin_path, url):
                # last resort: bash provision (may fetch tarball)
                if not _try_bash_provision() and need_embed:
                    raise RuntimeError(
                        f"Could not start product ollama at {url} using {bin_path}. "
                        "Check logs under the plugin data dir (logs/llm-provision.log)."
                    )
    status["daemon"] = daemon_reachable(url)
    if not status["daemon"]:
        if need_embed:
            raise RuntimeError(f"ollama daemon not reachable at {url}")
        return status

    bin_path = resolve_ollama_bin()
    if bin_path is not None:
        status["bin"] = str(bin_path)

    if pull and need_embed:
        tag = embed_model_tag()
        if bin_path is not None:
            status["embed_ready"] = pull_model(bin_path, tag)
        else:
            status["embed_ready"] = model_present(tag)
        if not status["embed_ready"]:
            # try bash provision once more for pull
            _try_bash_provision()
            status["embed_ready"] = model_present(tag)
        if not status["embed_ready"]:
            raise RuntimeError(
                f"Embed model {tag!r} is not available on product ollama. "
                f"Pull: {(status.get('bin') or 'ollama')} pull {tag}"
            )

    if pull and need_chat:
        tag = chat_model_tag()
        if bin_path is not None:
            status["chat_ready"] = pull_model(bin_path, tag)
        else:
            status["chat_ready"] = model_present(tag)

    return status
