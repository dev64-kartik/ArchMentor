"use client";

import { useCallback, useState } from "react";
import type { Room } from "livekit-client";

import { ExcalidrawCanvas } from "@/components/canvas/excalidraw-canvas";
import { SessionRoom } from "@/components/livekit/session-room";

type Props = {
  sessionId: string;
  roomName: string;
};

/**
 * Wraps the SessionRoom + ExcalidrawCanvas so they can share a Room
 * reference. SessionRoom owns the LiveKit lifecycle and lifts its
 * joined room up via onRoomChange; the canvas wrapper publishes scenes
 * over the same connection.
 */
export function SessionShell({ sessionId, roomName }: Props) {
  const [liveRoom, setLiveRoom] = useState<Room | null>(null);
  const onRoomChange = useCallback((room: Room | null) => setLiveRoom(room), []);

  return (
    <main className="grid h-screen grid-cols-[70%_30%]">
      <section className="border-r border-neutral-200 dark:border-neutral-800">
        <ExcalidrawCanvas room={liveRoom} />
      </section>
      <aside className="flex flex-col gap-4 p-4">
        <SessionRoom
          room={roomName}
          sessionId={sessionId}
          onRoomChange={onRoomChange}
        />
      </aside>
    </main>
  );
}
