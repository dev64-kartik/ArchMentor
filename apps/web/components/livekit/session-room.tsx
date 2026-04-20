"use client";

import { useEffect, useRef, useState } from "react";
import { ConnectionState, Room, RoomEvent, Track } from "livekit-client";

import { fetchLiveKitToken } from "@/lib/livekit/token";

type Props = {
  room: string;
};

type AiSpeakingState = "idle" | "listening" | "speaking" | "thinking";

/**
 * Joins a LiveKit room, publishes the candidate's mic, and renders
 * connection + AI state. Until M4 wires brain state signaling, the AI
 * indicator is a visual placeholder: it flips to "speaking" when a
 * remote audio track is audible and back to "listening" otherwise.
 */
export function SessionRoom({ room: roomName }: Props) {
  const roomRef = useRef<Room | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>(
    ConnectionState.Disconnected,
  );
  const [aiState, setAiState] = useState<AiSpeakingState>("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const room = new Room({ adaptiveStream: true, dynacast: true });
    roomRef.current = room;

    room.on(RoomEvent.ConnectionStateChanged, (state) => {
      if (!cancelled) setConnectionState(state);
    });
    room.on(RoomEvent.TrackSubscribed, (track) => {
      if (track.kind === Track.Kind.Audio) setAiState("speaking");
    });
    room.on(RoomEvent.TrackUnsubscribed, (track) => {
      if (track.kind === Track.Kind.Audio) setAiState("listening");
    });

    (async () => {
      try {
        const creds = await fetchLiveKitToken(roomName);
        if (cancelled) return;

        await room.connect(creds.url, creds.token);
        await room.localParticipant.setMicrophoneEnabled(true);
        if (!cancelled) setAiState("listening");
      } catch (exc) {
        if (!cancelled) {
          setError(exc instanceof Error ? exc.message : String(exc));
        }
      }
    })();

    return () => {
      cancelled = true;
      roomRef.current?.disconnect();
    };
  }, [roomName]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <StatusDot state={connectionState} />
        <span className="text-sm text-neutral-700 dark:text-neutral-300">
          Room <code className="text-xs">{roomName}</code>
        </span>
      </div>
      <AiStateIndicator state={aiState} />
      {error ? (
        <p className="text-xs text-red-600 dark:text-red-400">{error}</p>
      ) : null}
    </div>
  );
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
  const label =
    state === "speaking"
      ? "AI speaking"
      : state === "listening"
        ? "AI listening"
        : state === "thinking"
          ? "AI thinking"
          : "Waiting for room";
  return (
    <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-700 dark:border-neutral-800 dark:bg-neutral-900/40 dark:text-neutral-300">
      {label}
    </div>
  );
}
