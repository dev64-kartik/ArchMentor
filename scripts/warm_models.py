"""Warm the Kokoro TTS + NLTK caches into the repo-local cache directory.

The livekit-agents worker used to do this on first job, but Kokoro's
cold-start downloads a ~300MB HF weights file + NLTK punkt data —
over the framework's 60s per-job initialization timeout, so the job
was killed mid-download.

This script is idempotent and safe to run repeatedly. It's called
from `scripts/dev.sh` once docker is up so the models are on disk
before the agent tries to load them. Afterwards you can set
`HF_HUB_OFFLINE=1` and the agent will never reach the network.

Cache paths are repo-local (`.model-cache/…`) so the sandboxed
harness can read/write them; the paths are picked up via `HF_HOME`
and `NLTK_DATA` env vars set in `.env`.
"""
# ruff: noqa: T201  # dev-only warm-up script — `print(..., flush=True)` is the
# right UX here so `scripts/dev.sh` shows live progress while weights download.

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HF_HOME = REPO_ROOT / ".model-cache" / "hf"
NLTK_DATA = REPO_ROOT / ".model-cache" / "nltk"

# Warm-up is the one step that needs network (to fetch voice weights
# on a fresh clone). Drop HF_HUB_OFFLINE for the duration of this
# script; runtime (.env) keeps it on so the agent never reaches the
# net after warm-up. Proxies stay intact — `socksio` is installed
# specifically so httpx can route HF calls through the sandbox's
# SOCKS proxy.
os.environ.pop("HF_HUB_OFFLINE", None)


def main() -> int:
    HF_HOME.mkdir(parents=True, exist_ok=True)
    NLTK_DATA.mkdir(parents=True, exist_ok=True)

    os.environ["HF_HOME"] = str(HF_HOME)
    os.environ["NLTK_DATA"] = str(NLTK_DATA)

    _ensure_nltk_punkt()
    _ensure_whisper()
    _ensure_kokoro()
    return 0


def _ensure_nltk_punkt() -> None:
    print("[warm] NLTK punkt_tab: checking...", flush=True)
    import nltk

    nltk.data.path.insert(0, str(NLTK_DATA))
    for resource in ("tokenizers/punkt_tab", "tokenizers/punkt"):
        try:
            nltk.data.find(resource)
            print(f"[warm] NLTK {resource}: cached", flush=True)
            return
        except LookupError:
            continue

    print("[warm] NLTK punkt_tab: downloading...", flush=True)
    t0 = time.time()
    nltk.download("punkt_tab", download_dir=str(NLTK_DATA), quiet=True)
    print(f"[warm] NLTK punkt_tab: downloaded in {time.time() - t0:.1f}s", flush=True)


def _ensure_whisper() -> None:
    """Pre-download the ggml whisper model into the repo-local cache.

    pywhispercpp defaults its cache to
    `~/Library/Application Support/pywhispercpp`, which the Claude
    sandbox can't write to. `ARCHMENTOR_WHISPER_DIR` points it here.
    """
    import importlib

    models_dir = REPO_ROOT / ".model-cache" / "whisper"
    models_dir.mkdir(parents=True, exist_ok=True)
    # Default must match `audio/stt.py::_load_model` (also `large-v3`).
    # A mismatch means this warm-up never prepares the model the agent
    # actually loads at runtime — the first live STT call then pays the
    # full download/init cost.
    model_name = os.environ.get("ARCHMENTOR_WHISPER_MODEL", "large-v3")

    candidates = [
        models_dir / f"ggml-{model_name}.bin",
        models_dir / f"{model_name}.bin",
    ]
    if any(p.exists() for p in candidates):
        print(f"[warm] whisper {model_name}: cached", flush=True)
        return

    print(f"[warm] whisper {model_name}: downloading...", flush=True)
    t0 = time.time()
    try:
        utils = importlib.import_module("pywhispercpp.utils")
    except ImportError:
        print("[warm] pywhispercpp not installed — skip.", file=sys.stderr, flush=True)
        return
    utils.download_model(model_name, download_dir=str(models_dir))
    print(f"[warm] whisper {model_name}: ready in {time.time() - t0:.1f}s", flush=True)


def _ensure_kokoro() -> None:
    """Ensure Kokoro model + selected voice are cached in HF_HOME."""
    _ensure_kokoro_base()
    _ensure_kokoro_voice(os.environ.get("ARCHMENTOR_TTS_VOICE", "af_bella"))

    print("[warm] Kokoro: loading engine...", flush=True)
    t0 = time.time()
    try:
        from streaming_tts import KokoroEngine
    except ImportError:
        print(
            "[warm] streaming_tts not installed — skip. Install via "
            "`uv sync --all-packages --extra audio` (macOS only).",
            file=sys.stderr,
            flush=True,
        )
        return

    # Force offline so the engine can only use what we placed in cache —
    # proves warm-up was sufficient and catches missing files early.
    os.environ["HF_HUB_OFFLINE"] = "1"
    engine = KokoroEngine(voice=os.environ.get("ARCHMENTOR_TTS_VOICE", "af_bella"))
    engine.synthesize("warmup")
    chunks = engine.queue.qsize()
    if chunks == 0:
        raise RuntimeError("[warm] Kokoro produced no audio — cache is incomplete")
    print(f"[warm] Kokoro: ready in {time.time() - t0:.1f}s (queue={chunks})", flush=True)


_KOKORO_REPO = "hexgrad/Kokoro-82M"
_KOKORO_COMMIT = "f3ff3571791e39611d31c381e3a41a3af07b4987"


def _kokoro_snapshot_dir() -> Path:
    safe_repo = _KOKORO_REPO.replace("/", "--")
    return HF_HOME / "hub" / f"models--{safe_repo}" / "snapshots" / _KOKORO_COMMIT


def _ensure_kokoro_base() -> None:
    snapshot = _kokoro_snapshot_dir()
    for filename in ("config.json", "kokoro-v1_0.pth"):
        if (snapshot / filename).exists():
            continue
        print(f"[warm] Kokoro {filename}: missing — fetching...", flush=True)
        _fetch_hf_file_into_cache(_KOKORO_REPO, filename)


def _ensure_kokoro_voice(voice: str) -> None:
    snapshot = _kokoro_snapshot_dir()
    dest = snapshot / "voices" / f"{voice}.pt"
    if dest.exists():
        print(f"[warm] Kokoro voice {voice}: cached", flush=True)
        return
    print(f"[warm] Kokoro voice {voice}: fetching...", flush=True)
    _fetch_hf_file_into_cache(_KOKORO_REPO, f"voices/{voice}.pt")


def _fetch_hf_file_into_cache(repo: str, relpath: str) -> None:
    """Download an HF file via plain HTTP and insert it into the HF cache layout.

    We can't use `hf_hub_download` under the Claude harness because its
    httpx client hangs when `ALL_PROXY` points at the sandbox's SOCKS
    endpoint. Curl tunnels cleanly through `HTTP_PROXY`, so we shell
    out for the fetch and assemble the cache entry ourselves.
    """
    import hashlib
    import shutil
    import subprocess

    curl = shutil.which("curl")
    if curl is None:
        raise RuntimeError("curl not found on PATH — required for HF warm-up fetch")

    url = f"https://huggingface.co/{repo}/resolve/main/{relpath}"
    relpath_hash = hashlib.md5(relpath.encode()).hexdigest()  # noqa: S324
    tmp = Path(tempfile.gettempdir()) / f"hf-{os.getpid()}-{relpath_hash}.bin"
    # curl is a trusted absolute path from shutil.which; args are literals +
    # a repo-pinned URL. Targeted S603 suppression — no shell interpolation.
    subprocess.run(  # noqa: S603
        [curl, "-sSL", "--fail", "-o", str(tmp), url], check=True, timeout=120
    )
    sha256 = hashlib.sha256(tmp.read_bytes()).hexdigest()

    cache_root = HF_HOME / "hub" / f"models--{repo.replace('/', '--')}"
    blob = cache_root / "blobs" / sha256
    blob.parent.mkdir(parents=True, exist_ok=True)
    tmp.replace(blob)

    snapshot_dir = cache_root / "snapshots" / _KOKORO_COMMIT
    target = snapshot_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    # Build a relative symlink that matches HF's canonical cache layout.
    rel_blob = os.path.relpath(blob, target.parent)
    target.symlink_to(rel_blob)

    ref_main = cache_root / "refs" / "main"
    ref_main.parent.mkdir(parents=True, exist_ok=True)
    ref_main.write_text(_KOKORO_COMMIT)


if __name__ == "__main__":
    sys.exit(main())
