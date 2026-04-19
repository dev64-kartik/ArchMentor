import { Room, type RoomOptions } from "livekit-client";

/**
 * Minimal LiveKit room factory. The session page wires audio tracks,
 * the canvas-diff data channel, and the thinking/speaking indicator
 * on top of this in M1 and M2.
 */
export function createLiveKitRoom(options: RoomOptions = {}): Room {
  return new Room({
    adaptiveStream: true,
    dynacast: true,
    ...options,
  });
}

export const LIVEKIT_URL = process.env["NEXT_PUBLIC_LIVEKIT_URL"] ?? "ws://localhost:7880";
