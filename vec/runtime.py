"""Product-owned ollama runtime for dense embeds (+ optional chat refine).

Design: total-recall **always** owns the ollama binary under the plugin data
dir (``$CLAUDE_PLUGIN_DATA/total-recall/bin/ollama``), starts ``ollama serve``
on a **product-owned port** (default ``127.0.0.1:11435``), and pulls models.
System PATH / snap ollama and anything on ``:11434`` are never used unless the
operator pins ``TOTAL_RECALL_LLM_BASE_URL`` / ``RECALL_OLLAMA_ALLOW_SYSTEM=1``.
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

# Product-owned — never share system ollama's classic :11434.
DEFAULT_BASE_URL = "http://127.0.0.1:11435"
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
    """Product daemon URL. Env pin → stamp file → default :11435."""
    env = (os.environ.get("TOTAL_RECALL_LLM_BASE_URL") or "").strip().rstrip("/")
    if env:
        return env
    stamp = data_root() / "bin" / ".ollama-base-url"
    if stamp.is_file():
        try:
            raw = stamp.read_text(encoding="utf-8").strip().rstrip("/")
            if raw:
                return raw
        except OSError:
            pass
    return DEFAULT_BASE_URL


def host_port(url: str | None = None) -> str:
    """host:port for OLLAMA_HOST from a base URL."""
    u = (url or base_url()).rstrip("/")
    u = u.removeprefix("https://").removeprefix("http://")
    return u.split("/", 1)[0]


def _stamp_base_url(url: str) -> None:
    try:
        p = data_root() / "bin" / ".ollama-base-url"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(url.rstrip("/") + "\n", encoding="utf-8")
    except OSError:
        pass
    # Keep this process and children on the product daemon.
    os.environ["TOTAL_RECALL_LLM_BASE_URL"] = url.rstrip("/")
    os.environ["OLLAMA_HOST"] = host_port(url)


def daemon_is_product(bin_path: Path | None = None, url: str | None = None) -> bool:
    """True only when our product binary is serving the product URL.

    Reachable-but-foreign (system ollama, etc.) → False. No unknowns.
    Candidates: pidfile we wrote on start, then live ``ollama`` processes.
    """
    u = (url or base_url()).rstrip("/")
    if not daemon_reachable(u):
        return False
    want = bin_path or resolve_ollama_bin()
    if want is None:
        return False
    try:
        want_r = want.resolve()
    except OSError:
        want_r = want
    hp = host_port(u)
    port = hp.rsplit(":", 1)[-1] if ":" in hp else "11435"
    want_s = str(want_r).encode()
    pids: list[str] = []
    pidfile = data_root() / "bin" / "ollama.pid"
    if pidfile.is_file():
        try:
            raw = pidfile.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                pids.append(raw)
        except OSError:
            pass
    # Real ELF product serve shows as process name "ollama".
    try:
        out = subprocess.run(  # noqa: S603
            ["pgrep", "-x", "ollama"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout:
            pids.extend(p.strip() for p in out.stdout.splitlines() if p.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass

    for pid in pids:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if b"serve" not in cmdline:
            continue
        try:
            exe = Path(f"/proc/{pid}/exe").resolve()
        except OSError:
            exe = None
        if exe != want_r and want_s not in cmdline:
            continue
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            environ = []
        env_txt = [e.decode("utf-8", "replace") for e in environ if e]
        if any(
            e == f"OLLAMA_HOST={hp}"
            or (e.startswith("OLLAMA_HOST=") and e.endswith(f":{port}"))
            for e in env_txt
        ):
            return True
    return False


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
    """Resolve product-embedded binary only (never system PATH by default)."""
    env = (os.environ.get("RECALL_OLLAMA") or "").strip()
    if env:
        p = Path(env)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    bundled = data_root() / "bin" / "ollama"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled
    allow = (os.environ.get("RECALL_OLLAMA_ALLOW_SYSTEM") or "0").strip().lower()
    if allow in ("1", "true", "yes", "on"):
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


def _product_serve_env(url: str | None = None) -> dict[str, str]:
    """Env for product ``ollama serve``: GPU + MTP + product-owned bind."""
    env = os.environ.copy()
    u = (url or base_url()).rstrip("/")
    env["OLLAMA_HOST"] = host_port(u)
    env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
    env.setdefault("OLLAMA_KEEP_ALIVE", "-1")
    env.setdefault("OLLAMA_MAX_LOADED_MODELS", "4")
    env.setdefault("OLLAMA_NUM_PARALLEL", "4")
    env.setdefault("OLLAMA_MAX_QUEUE", "2048")
    # MTP draft depth (MLX). CUDA/llama-server auto-uses built-in mtp.* tensors
    # on models like qwen3.5:2b (no extra env required, but harmless).
    env.setdefault("OLLAMA_MLX_MTP_MAX_DRAFT_TOKENS", "4")
    env.setdefault("OLLAMA_MLX_MTP_INITIAL_DRAFT_TOKENS", "4")
    # Optional shared model store (host already has models elsewhere).
    recall_models = (os.environ.get("RECALL_OLLAMA_MODELS") or "").strip()
    if recall_models:
        env["OLLAMA_MODELS"] = recall_models
    return env


def start_daemon(bin_path: Path, url: str | None = None) -> bool:
    """Start product binary on product URL. Never treats a foreign daemon as up."""
    u = (url or base_url()).rstrip("/")
    if daemon_is_product(bin_path, u):
        _stamp_base_url(u)
        return True
    if daemon_reachable(u) and not daemon_is_product(bin_path, u):
        log.warning(
            "ollama at %s is reachable but not product binary %s — not using foreign daemon",
            u,
            bin_path,
        )
        return False
    log_dir = data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "llm-provision.log"
    env = _product_serve_env(u)
    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(
                f"\n[python-runtime] starting {bin_path} serve "
                f"OLLAMA_HOST={env.get('OLLAMA_HOST')} url={u}\n"
            )
            proc = subprocess.Popen(  # noqa: S603
                [str(bin_path), "serve"],
                stdout=lf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        log.info("started product ollama serve pid=%s host=%s", proc.pid, env.get("OLLAMA_HOST"))
        try:
            (data_root() / "bin" / "ollama.pid").write_text(str(proc.pid) + "\n", encoding="utf-8")
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to start product ollama serve: %s", exc)
        return False
    deadline = time.time() + _SERVE_WAIT_S
    while time.time() < deadline:
        if daemon_is_product(bin_path, u):
            _stamp_base_url(u)
            return True
        time.sleep(0.5)
    ok = daemon_is_product(bin_path, u)
    if ok:
        _stamp_base_url(u)
    return ok


def pull_model(bin_path: Path, tag: str, url: str | None = None) -> bool:
    u = (url or base_url()).rstrip("/")
    names = list_model_names(u)
    if names is not None and model_present(tag, names):
        return True
    env = os.environ.copy()
    env["OLLAMA_HOST"] = host_port(u)
    try:
        r = subprocess.run(  # noqa: S603
            [str(bin_path), "pull", tag],
            timeout=_PULL_TIMEOUT_S,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if r.returncode != 0:
            log.warning("ollama pull %s failed: %s", tag, (r.stderr or r.stdout or "")[:400])
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("ollama pull %s error: %s", tag, exc)
        return False
    names2 = list_model_names(u)
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
    bin_path = resolve_ollama_bin()
    if bin_path is not None:
        status["bin"] = str(bin_path)

    # Only a product-owned daemon counts. Foreign on any port → start ours.
    if not daemon_is_product(bin_path, url):
        if bin_path is None:
            if not _try_bash_provision():
                if need_embed:
                    raise RuntimeError(
                        "Product ollama is not available (no binary, daemon down). "
                        "total-recall auto-provisions under the plugin data dir on "
                        "first session; or run: bash scripts/llm-setup.sh"
                    )
                return status
            bin_path = resolve_ollama_bin()
            if bin_path is not None:
                status["bin"] = str(bin_path)
        if bin_path is not None and not start_daemon(bin_path, url):
            if not _try_bash_provision() and need_embed:
                raise RuntimeError(
                    f"Could not start product ollama at {url} using {bin_path}. "
                    "Check logs under the plugin data dir (logs/llm-provision.log)."
                )
            bin_path = resolve_ollama_bin() or bin_path

    owned = daemon_is_product(bin_path, url)
    status["daemon"] = owned
    status["base_url"] = base_url()
    status["product_owned"] = owned
    if owned:
        _stamp_base_url(base_url())
    if not owned:
        if need_embed:
            raise RuntimeError(
                f"product ollama daemon not serving at {url} "
                f"(foreign-or-down; never using system :11434 by default)"
            )
        return status

    if bin_path is None:
        bin_path = resolve_ollama_bin()
    if bin_path is not None:
        status["bin"] = str(bin_path)

    if pull and need_embed:
        tag = embed_model_tag()
        if bin_path is not None:
            status["embed_ready"] = pull_model(bin_path, tag, url)
        else:
            status["embed_ready"] = model_present(tag)
        if not status["embed_ready"]:
            # try bash provision once more for pull
            _try_bash_provision()
            status["embed_ready"] = model_present(tag)
        if not status["embed_ready"]:
            raise RuntimeError(
                f"Embed model {tag!r} is not available on product ollama. "
                f"Pull: OLLAMA_HOST={host_port(url)} "
                f"{(status.get('bin') or 'ollama')} pull {tag}"
            )

    if pull and need_chat:
        tag = chat_model_tag()
        if bin_path is not None:
            status["chat_ready"] = pull_model(bin_path, tag, url)
        else:
            status["chat_ready"] = model_present(tag)

    return status
