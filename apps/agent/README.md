# archmentor-agent

LiveKit Agent worker for ArchMentor.

## Stack

- LiveKit Agents (Python) — VAD + `session.say()` + barge-in
- Silero VAD via `livekit-plugins-silero`
- whisper.cpp (Metal) via `pywhispercpp` — added in M1
- Kokoro TTS via `streaming-tts` (MPS) — added in M1
- Anthropic SDK — Claude Opus 4.6 streaming with tool-use (M2)
- Redis — hot SessionState (no TTL on session keys)
- structlog — JSON logs

## Directory map

```
archmentor_agent/
├── main.py              # cli.run_app(WorkerOptions(...)) — M1
├── brain/               # Claude Opus + tool-use — M2
│   ├── prompts/
│   ├── tools.py         # interview_decision schema
│   └── client.py
├── state/
│   ├── session_state.py # SessionState + DesignDecision
│   └── redis_store.py
├── events/              # Serialized router + coalescer — M2
├── audio/               # Noise gate + whisper STT — M1
├── tts/                 # Kokoro — M1, streaming in M4
├── canvas/              # Excalidraw parser — M3
├── queue/               # Utterance queue + speech-check — M2
└── snapshots/           # brain_snapshots writer — M2
```

## Commands

```bash
# From repo root
uv sync                                                     # install workspace deps
uv run --package archmentor-agent python -m archmentor_agent.main dev

uv run ruff check apps/agent
uv run ty check apps/agent
uv run pytest apps/agent
```

## System prerequisites (M1)

whisper.cpp and Kokoro need native components:

- macOS: Xcode CLT + Metal SDK (default on Apple Silicon)
- `pywhispercpp` may require a one-time model download; we pin `large-v3`
- Kokoro's `streaming-tts` uses PyTorch with MPS backend

These are installed on demand in M1. The scaffold in this package does
not import them.
