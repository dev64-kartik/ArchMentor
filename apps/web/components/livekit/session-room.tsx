"use client";
// M1 diagnostic: the console.log calls below surface LiveKit room
// events + <audio> element lifecycle in the browser devtools, which
// is currently the only way to tell whether the agent's streamed
// audio actually plays. Replace with structured client-side telemetry
// once the voice loop is validated.
/* oxlint-disable no-console */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ConnectionState,
  type RemoteAudioTrack,
  type RemoteTrack,
  Room,
  RoomEvent,
  Track,
} from "livekit-client";

import { fetchLiveKitToken } from "@/lib/livekit/token";

type Props = {
  room: string;
};

type AiSpeakingState = "idle" | "listening" | "speaking" | "thinking";

// Minimum seconds between "Join" and "End session" becoming enabled.
// Guards against an accidental double-click that ends the session
// before the opening utterance has even finished playing.
const MIN_SESSION_SEC = 45;

// Allocate the decoder once — per-event `new TextDecoder()` is free
// at single-digit ms scale but allocates a native object per data
// message and the data channel fires on every agent phase transition.
const AI_STATE_DECODER = new TextDecoder();

function isAiSpeakingState(value: unknown): value is AiSpeakingState {
  return (
    value === "idle" ||
    value === "listening" ||
    value === "speaking" ||
    value === "thinking"
  );
}

function parseAiState(payload: Uint8Array): AiSpeakingState | null {
  try {
    const text = AI_STATE_DECODER.decode(payload);
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed === "object" && parsed !== null && "ai_state" in parsed) {
      const state = (parsed as { ai_state: unknown }).ai_state;
      return isAiSpeakingState(state) ? state : null;
    }
    return null;
  } catch (err) {
    console.warn("[session] ai_state parse failed", err);
    return null;
  }
}

/**
 * Joins a LiveKit room, publishes the candidate's mic, and renders
 * connection + AI state. Chrome's autoplay policy blocks AudioContext
 * creation until a user gesture occurs on the page, so the room join
 * is gated behind a Join button. Everything downstream — mic publish,
 * remote track attach, `<audio>` playback — happens inside the click
 * handler so the gesture cascade is preserved.
 */
export function SessionRoom({ room: roomName }: Props) {
  const roomRef = useRef<Room | null>(null);
  const audioElRef = useRef<HTMLAudioElement | null>(null);
  // Synchronous guard against double-click and StrictMode double-invoke.
  // React state updates are batched across renders; reading `joining` /
  // `joined` in the click handler can miss an in-flight connect. A ref
  // is written synchronously so the second click returns immediately.
  const joiningRef = useRef(false);
  // Track the audio track that arrived before the <audio> element
  // mounted. The RoomEvent.TrackSubscribed handler stashes it here;
  // the audio-element mount effect picks it up and calls attach().
  const pendingTrackRef = useRef<RemoteAudioTrack | null>(null);
  // Set to false on unmount / disconnect so in-flight `join()` can
  // short-circuit instead of calling setState on a dead component.
  const mountedRef = useRef(true);
  const [connectionState, setConnectionState] = useState<ConnectionState>(
    ConnectionState.Disconnected,
  );
  const [aiState, setAiState] = useState<AiSpeakingState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [needsAudioUnlock, setNeedsAudioUnlock] = useState(false);
  const [joined, setJoined] = useState(false);
  const [joining, setJoining] = useState(false);
  const [elapsedSec, setElapsedSec] = useState(0);

  const attachAudioTrack = useCallback((track: RemoteAudioTrack) => {
    const el = audioElRef.current;
    if (!el) {
      pendingTrackRef.current = track;
      return;
    }
    track.attach(el);
    el.play().catch((err) => {
      console.warn("[session] audio play() rejected", err);
      setNeedsAudioUnlock(true);
    });
  }, []);

  const resetJoinedState = useCallback(() => {
    setJoined(false);
    setAiState("idle");
    setElapsedSec(0);
    joiningRef.current = false;
  }, []);

  const join = useCallback(async () => {
    // Synchronous short-circuit — must precede any await or setState.
    if (joiningRef.current) return;
    joiningRef.current = true;
    setJoining(true);
    setError(null);

    // Lock Chrome's user-gesture activation to an AudioContext *before*
    // the first await. `room.startAudio()` is only safe to call until
    // the user gesture expires; awaiting two network round-trips first
    // can consume the gesture on some Chrome builds.
    const AudioCtor =
      typeof window !== "undefined"
        ? (window.AudioContext ??
          (window as Window & { webkitAudioContext?: typeof AudioContext })
            .webkitAudioContext)
        : undefined;
    const audioContext = AudioCtor ? new AudioCtor() : null;

    const room = new Room({ adaptiveStream: true, dynacast: true });
    roomRef.current = room;

    room.on(RoomEvent.ConnectionStateChanged, (state) => {
      setConnectionState(state);
    });
    room.on(
      RoomEvent.TrackSubscribed,
      (track: RemoteTrack, _pub, participant) => {
        console.log("[session] TrackSubscribed", {
          kind: track.kind,
          sid: track.sid,
          participant: participant.identity,
        });
        if (track.kind !== Track.Kind.Audio) return;
        attachAudioTrack(track as RemoteAudioTrack);
        setAiState("speaking");
      },
    );
    room.on(RoomEvent.TrackUnsubscribed, (track) => {
      if (track.kind !== Track.Kind.Audio) return;
      (track as RemoteAudioTrack).detach();
      if (pendingTrackRef.current === track) pendingTrackRef.current = null;
      setAiState("listening");
    });
    room.on(RoomEvent.DataReceived, (payload, participant, _kind, topic) => {
      // The agent publishes `{ ai_state: "speaking" | "listening" |
      // "thinking" }` on the `ai_state` topic at every phase boundary
      // so the UI can prompt the candidate to speak and tell them
      // when their input is being processed.
      if (topic !== "ai_state") return;
      // Origin filter: accept `ai_state` only from remote participants,
      // and reject messages from the local participant (we never send
      // them to ourselves in M1, but LiveKit loopback is easy to trip
      // into with a misconfigured second tab). Rejecting local also
      // blocks a malicious extension that publishes on the same topic.
      if (!participant || participant.isLocal) {
        console.warn("[session] ai_state rejected: no remote origin", {
          participant: participant?.identity,
        });
        return;
      }
      const state = parseAiState(payload);
      if (state) {
        console.log("[session] ai_state", state, "from", participant.identity);
        setAiState(state);
      }
    });
    room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
      console.log("[session] AudioPlaybackStatusChanged", {
        canPlaybackAudio: room.canPlaybackAudio,
      });
      setNeedsAudioUnlock(!room.canPlaybackAudio);
    });
    room.on(RoomEvent.Disconnected, (reason) => {
      console.log("[session] Disconnected", { reason });
      // Unexpected disconnect (network drop, token expiry, SFU kick).
      // Without this reset the join guard stays locked, the timer keeps
      // ticking, and the end-session button stays disabled forever.
      if (mountedRef.current && roomRef.current === room) {
        roomRef.current = null;
        pendingTrackRef.current = null;
        resetJoinedState();
      }
    });

    try {
      const creds = await fetchLiveKitToken(roomName);
      if (!mountedRef.current) {
        room.disconnect();
        return;
      }
      await room.connect(creds.url, creds.token);
      if (!mountedRef.current) {
        room.disconnect();
        return;
      }
      // `startAudio()` MUST be called inside the click handler's
      // microtask chain so Chrome associates the AudioContext with
      // the user gesture. Eagerly-constructed `audioContext` above
      // hedges the gesture in case `connect()` exceeds the activation
      // budget on a slow connection.
      await room.startAudio();
      await room.localParticipant.setMicrophoneEnabled(true);
      setAiState("listening");
      setJoined(true);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setAiState("idle");
      roomRef.current?.disconnect();
      roomRef.current = null;
      // Release the AudioContext we eagerly created — otherwise Chrome
      // accumulates contexts on repeated failed joins.
      audioContext?.close().catch(() => undefined);
    } finally {
      setJoining(false);
      // Successful joins release the guard when the room disconnects
      // (via the RoomEvent.Disconnected handler). Failed joins release
      // it here.
      if (!roomRef.current) {
        joiningRef.current = false;
      }
    }
  }, [attachAudioTrack, resetJoinedState, roomName]);

  useEffect(() => {
    const el = audioElRef.current;
    if (!el) return;
    // Attach a track that arrived before we mounted.
    if (pendingTrackRef.current) {
      pendingTrackRef.current.attach(el);
      el.play().catch((err) => {
        console.warn("[session] audio play() rejected", err);
        setNeedsAudioUnlock(true);
      });
      pendingTrackRef.current = null;
    }
    const logState = (type: string) => () =>
      console.log(`[session] audio.${type}`, {
        paused: el.paused,
        currentTime: el.currentTime,
        readyState: el.readyState,
      });
    const onError = () =>
      console.warn("[session] audio.error", {
        error: el.error,
        code: el.error?.code,
        message: el.error?.message,
        readyState: el.readyState,
      });
    const handlers: Array<[keyof HTMLMediaElementEventMap, () => void]> = [
      ["play", logState("play")],
      ["playing", logState("playing")],
      ["pause", logState("pause")],
      ["ended", logState("ended")],
      ["error", onError],
      ["stalled", logState("stalled")],
      ["waiting", logState("waiting")],
      ["loadedmetadata", logState("loadedmetadata")],
    ];
    for (const [evt, handler] of handlers) el.addEventListener(evt, handler);
    return () => {
      for (const [evt, handler] of handlers)
        el.removeEventListener(evt, handler);
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const mountedAt = Date.now();
    console.log("[session] SessionRoom mounted", { mountedAt });
    return () => {
      const aliveMs = Date.now() - mountedAt;
      const hasRoom = !!roomRef.current;
      console.log("[session] SessionRoom unmount", { aliveMs, hasRoom });
      mountedRef.current = false;
      roomRef.current?.disconnect();
      roomRef.current = null;
      pendingTrackRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!joined) {
      setElapsedSec(0);
      return;
    }
    const started = Date.now();
    const interval = setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    return () => clearInterval(interval);
  }, [joined]);

  const leave = useCallback(() => {
    roomRef.current?.disconnect();
    roomRef.current = null;
    pendingTrackRef.current = null;
    setConnectionState(ConnectionState.Disconnected);
    resetJoinedState();
  }, [resetJoinedState]);

  const handleAudioUnlock = useCallback(async () => {
    try {
      await roomRef.current?.startAudio();
      await audioElRef.current?.play();
      setNeedsAudioUnlock(false);
    } catch (err) {
      console.warn("[session] audio unlock failed", err);
      setError(
        err instanceof Error ? err.message : "Could not unlock audio playback.",
      );
    }
  }, []);

  const remainingSec = Math.max(0, MIN_SESSION_SEC - elapsedSec);
  const joinButtonClass = [
    "rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white",
    "hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-60",
  ].join(" ");
  const leaveButtonClass = [
    "rounded-md bg-neutral-700 px-4 py-2 text-sm font-medium text-white",
    "hover:bg-neutral-800 disabled:cursor-not-allowed",
    "disabled:bg-neutral-400 disabled:opacity-60",
  ].join(" ");
  const unlockButtonClass = [
    "rounded-md bg-amber-500 px-3 py-2 text-sm font-medium text-white",
    "hover:bg-amber-600",
  ].join(" ");
  const leaveTitle =
    remainingSec > 0
      ? `Wait until the intro plays and you've spoken at least once (${remainingSec}s remaining)`
      : "Leave the session";

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <StatusDot state={connectionState} />
        <span className="text-sm text-neutral-700 dark:text-neutral-300">
          Room <code className="text-xs">{roomName}</code>
        </span>
      </div>
      <AiStateIndicator state={aiState} />

      {!joined ? (
        <button
          type="button"
          onClick={join}
          disabled={joining}
          className={joinButtonClass}
        >
          {joining ? "Joining…" : "Join session"}
        </button>
      ) : (
        <div className="flex items-center gap-3">
          <span className="font-mono text-xs text-neutral-500 dark:text-neutral-400">
            {formatElapsed(elapsedSec)}
          </span>
          <button
            type="button"
            onClick={leave}
            disabled={remainingSec > 0}
            title={leaveTitle}
            className={leaveButtonClass}
          >
            {remainingSec > 0
              ? `End session (${remainingSec}s)`
              : "End session"}
          </button>
        </div>
      )}

      {needsAudioUnlock ? (
        <button
          type="button"
          onClick={handleAudioUnlock}
          className={unlockButtonClass}
        >
          Click to enable AI audio
        </button>
      ) : null}

      {error ? (
        <p className="text-xs text-red-600 dark:text-red-400">{error}</p>
      ) : null}

      <audio ref={audioElRef} autoPlay playsInline className="w-full" />
    </div>
  );
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function StatusDot({ state }: { state: ConnectionState }) {
  const color =
    state === ConnectionState.Connected
      ? "bg-emerald-500"
      : state === ConnectionState.Connecting || state === ConnectionState.Reconnecting
        ? "bg-amber-500"
        : "bg-neutral-400";
  return (
    <span className="inline-flex items-center gap-2 text-xs text-neutral-600 dark:text-neutral-400">
      <span className={`inline-block h-2 w-2 rounded-full ${color}`} />
      {state}
    </span>
  );
}

function AiStateIndicator({ state }: { state: AiSpeakingState }) {
  const config: Record<
    AiSpeakingState,
    { label: string; bg: string; pulse: boolean; urgent: boolean }
  > = {
    speaking: {
      label: "🔊 AI speaking — listen",
      bg: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-100",
      pulse: false,
      urgent: false,
    },
    listening: {
      label: "🎙️ Your turn — speak now",
      bg: "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-100",
      pulse: true,
      urgent: true,
    },
    thinking: {
      label: "🧠 AI processing your speech…",
      bg: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-100",
      pulse: false,
      urgent: false,
    },
    idle: {
      label: "Waiting for room…",
      bg: "bg-neutral-100 text-neutral-600 dark:bg-neutral-900/40 dark:text-neutral-400",
      pulse: false,
      urgent: false,
    },
  };
  const { label, bg, pulse, urgent } = config[state];
  return (
    <div
      role="status"
      aria-live={urgent ? "assertive" : "polite"}
      aria-atomic="true"
      className={`rounded-md border border-transparent px-4 py-3 text-base font-medium ${bg} ${
        pulse ? "animate-pulse" : ""
      }`}
    >
      {label}
    </div>
  );
}
