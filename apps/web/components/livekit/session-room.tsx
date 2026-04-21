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
  Room,
  RoomEvent,
  Track,
} from "livekit-client";

import { fetchLiveKitToken } from "@/lib/livekit/token";

type Props = {
  room: string;
};

type AiSpeakingState = "idle" | "listening" | "speaking" | "thinking";

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
  const [connectionState, setConnectionState] = useState<ConnectionState>(
    ConnectionState.Disconnected,
  );
  const [aiState, setAiState] = useState<AiSpeakingState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [needsAudioUnlock, setNeedsAudioUnlock] = useState(false);
  const [joined, setJoined] = useState(false);
  const [joining, setJoining] = useState(false);
  const [elapsedSec, setElapsedSec] = useState(0);
  const MIN_SESSION_SEC = 45;

  const join = useCallback(async () => {
    if (joining || joined) return;
    setJoining(true);
    setError(null);

    const room = new Room({ adaptiveStream: true, dynacast: true });
    roomRef.current = room;

    room.on(RoomEvent.ConnectionStateChanged, (state) => {
      setConnectionState(state);
    });
    room.on(RoomEvent.TrackSubscribed, (track, _pub, participant) => {
      console.log("[session] TrackSubscribed", {
        kind: track.kind,
        sid: track.sid,
        participant: participant.identity,
      });
      if (track.kind !== Track.Kind.Audio) return;
      if (audioElRef.current) {
        (track as RemoteAudioTrack).attach(audioElRef.current);
        audioElRef.current.play().catch((err) => {
          console.warn("[session] audio play() rejected", err);
          setNeedsAudioUnlock(true);
        });
      }
      setAiState("speaking");
    });
    room.on(RoomEvent.TrackUnsubscribed, (track) => {
      if (track.kind !== Track.Kind.Audio) return;
      (track as RemoteAudioTrack).detach();
      setAiState("listening");
    });
    room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
      // The agent publishes `{ ai_state: "speaking" | "listening" |
      // "thinking" }` on the `ai_state` topic at every phase boundary
      // so the UI can prompt the candidate to speak and tell them
      // when their input is being processed.
      if (topic !== "ai_state") return;
      try {
        const text = new TextDecoder().decode(payload);
        const parsed = JSON.parse(text) as { ai_state?: AiSpeakingState };
        if (
          parsed.ai_state === "speaking" ||
          parsed.ai_state === "listening" ||
          parsed.ai_state === "thinking"
        ) {
          console.log("[session] ai_state", parsed.ai_state);
          setAiState(parsed.ai_state);
        }
      } catch (err) {
        console.warn("[session] ai_state parse failed", err);
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
    });

    try {
      const creds = await fetchLiveKitToken(roomName);
      await room.connect(creds.url, creds.token);
      // `startAudio()` MUST be called inside the click handler's
      // microtask chain so Chrome associates the AudioContext with
      // the user gesture. Doing it later (e.g. after `connect()`
      // resolves into a queued microtask) sometimes still trips the
      // autoplay policy on strict Chrome builds.
      await room.startAudio();
      await room.localParticipant.setMicrophoneEnabled(true);
      setAiState("listening");
      setJoined(true);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      roomRef.current?.disconnect();
      roomRef.current = null;
    } finally {
      setJoining(false);
    }
  }, [joined, joining, roomName]);

  useEffect(() => {
    const el = audioElRef.current;
    if (!el) return;
    const log = (type: string) => () =>
      console.log(`[session] audio.${type}`, {
        paused: el.paused,
        currentTime: el.currentTime,
        readyState: el.readyState,
      });
    const handlers: Array<[keyof HTMLMediaElementEventMap, () => void]> = [
      ["play", log("play")],
      ["playing", log("playing")],
      ["pause", log("pause")],
      ["ended", log("ended")],
      ["error", log("error")],
      ["stalled", log("stalled")],
      ["waiting", log("waiting")],
      ["loadedmetadata", log("loadedmetadata")],
    ];
    for (const [evt, handler] of handlers) el.addEventListener(evt, handler);
    return () => {
      for (const [evt, handler] of handlers) el.removeEventListener(evt, handler);
    };
  }, []);

  useEffect(() => {
    const mountedAt = Date.now();
    console.log("[session] SessionRoom mounted", { mountedAt });
    return () => {
      const aliveMs = Date.now() - mountedAt;
      const hasRoom = !!roomRef.current;
      console.log("[session] SessionRoom unmount", { aliveMs, hasRoom });
      roomRef.current?.disconnect();
      roomRef.current = null;
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
    setJoined(false);
    setAiState("idle");
  }, []);

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
          className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-60"
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
            disabled={elapsedSec < MIN_SESSION_SEC}
            title={
              elapsedSec < MIN_SESSION_SEC
                ? `Wait until the intro plays and you've spoken at least once (${MIN_SESSION_SEC - elapsedSec}s remaining)`
                : "Leave the session"
            }
            className="rounded-md bg-neutral-700 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-800 disabled:cursor-not-allowed disabled:bg-neutral-400 disabled:opacity-60"
          >
            {elapsedSec < MIN_SESSION_SEC
              ? `End session (${MIN_SESSION_SEC - elapsedSec}s)`
              : "End session"}
          </button>
        </div>
      )}

      {needsAudioUnlock ? (
        <button
          type="button"
          onClick={() => {
            roomRef.current?.startAudio().catch(() => undefined);
            audioElRef.current?.play().catch(() => undefined);
            setNeedsAudioUnlock(false);
          }}
          className="rounded-md bg-amber-500 px-3 py-2 text-sm font-medium text-white hover:bg-amber-600"
        >
          Click to enable AI audio
        </button>
      ) : null}

      {error ? <p className="text-xs text-red-600 dark:text-red-400">{error}</p> : null}

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
  const config: Record<AiSpeakingState, { label: string; bg: string; pulse: boolean }> = {
    speaking: {
      label: "🔊 AI speaking — listen",
      bg: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-100",
      pulse: false,
    },
    listening: {
      label: "🎙️ Your turn — speak now",
      bg: "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-100",
      pulse: true,
    },
    thinking: {
      label: "🧠 AI processing your speech…",
      bg: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-100",
      pulse: false,
    },
    idle: {
      label: "Waiting for room…",
      bg: "bg-neutral-100 text-neutral-600 dark:bg-neutral-900/40 dark:text-neutral-400",
      pulse: false,
    },
  };
  const { label, bg, pulse } = config[state];
  return (
    <div
      className={`rounded-md border border-transparent px-4 py-3 text-base font-medium ${bg} ${
        pulse ? "animate-pulse" : ""
      }`}
    >
      {label}
    </div>
  );
}
