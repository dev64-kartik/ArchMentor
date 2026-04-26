"use client";

import type { Room } from "livekit-client";

const CANVAS_SCENE_TOPIC = "canvas-scene";
const THROTTLE_MS = 1000;

/**
 * Excalidraw scene → LiveKit text-stream publisher.
 *
 * R6/R7: 1 s leading+trailing throttle, fingerprint dedup so identical
 * scenes don't republish, server-stripped `files` field (R17). Publishes
 * over LiveKit text streams (not `publishData`) because non-trivial
 * scenes can exceed the SCTP per-frame limit; text streams chunk
 * transparently.
 *
 * Per refinements R4: full-scene-only — no diff transport.
 */
export class CanvasScenePublisher {
  private readonly room: Room;
  private lastFingerprint: string | null = null;
  private pendingScene: ScenePayload | null = null;
  private throttleTimer: ReturnType<typeof setTimeout> | null = null;
  private lastPublishMs = 0;
  private startMs = Date.now();

  constructor(room: Room) {
    this.room = room;
  }

  /**
   * Called from Excalidraw's `onChange`. Strips images, computes a
   * synchronous FNV-1a fingerprint over a stable serialization of
   * `elements`, and dispatches the throttled publish.
   *
   * All work is synchronous so there are no async-overlap races.
   */
  onSceneChange(elements: readonly unknown[]): void {
    const sanitizedElements = stripFiles(elements);
    const elementsKey = stableStringify(sanitizedElements);
    const fingerprint = fnv1aHex(elementsKey);
    if (fingerprint === this.lastFingerprint) {
      return;
    }
    const t_ms = Date.now() - this.startMs;
    this.pendingScene = {
      scene_fingerprint: fingerprint,
      t_ms,
      scene_json: { elements: sanitizedElements as unknown[], appState: {} },
    };
    this.scheduleFlush();
  }

  /**
   * Force-flush the pending scene, e.g. on `visibilitychange` to hidden
   * or before disconnect. Idempotent when nothing is pending.
   */
  async flushNow(): Promise<void> {
    if (this.throttleTimer !== null) {
      clearTimeout(this.throttleTimer);
      this.throttleTimer = null;
    }
    await this.publishPending();
  }

  dispose(): void {
    if (this.throttleTimer !== null) {
      clearTimeout(this.throttleTimer);
      this.throttleTimer = null;
    }
    this.flushNow(); // best-effort flush of any pending scene
    this.pendingScene = null;
  }

  private scheduleFlush(): void {
    const now = Date.now();
    const sinceLast = now - this.lastPublishMs;
    if (sinceLast >= THROTTLE_MS) {
      // Leading edge: publish immediately, schedule trailing edge so a
      // single late edit inside the window still surfaces.
      void this.publishPending();
      this.lastPublishMs = now;
      return;
    }
    if (this.throttleTimer !== null) return;
    const wait = THROTTLE_MS - sinceLast;
    this.throttleTimer = setTimeout(() => {
      this.throttleTimer = null;
      this.lastPublishMs = Date.now();
      void this.publishPending();
    }, wait);
  }

  private async publishPending(): Promise<void> {
    const scene = this.pendingScene;
    if (scene === null) return;
    this.pendingScene = null;
    try {
      const text = JSON.stringify(scene);
      // livekit-client@2.x: `sendText(text, options)` returns a stream
      // info promise; we don't await it on the hot path because the
      // browser-throttle bound is the latency contract.
      await this.room.localParticipant.sendText(text, {
        topic: CANVAS_SCENE_TOPIC,
      });
      // Commit the dedup cursor only after a successful send. If sendText
      // throws (room disconnected), lastFingerprint remains at its prior
      // value so the next onChange (or a flush retry) will re-attempt
      // this scene rather than silently suppressing it.
      this.lastFingerprint = scene.scene_fingerprint;
    } catch (err) {
      // Room may be disconnected (tab close, network drop). Leave
      // lastFingerprint unchanged so the scene is retried on the next
      // onChange. The server-side replay path also reads from
      // `canvas_snapshots`, not from text-stream history.
      // oxlint-disable-next-line no-console -- single diagnostic surface for canvas drops
      console.warn("[canvas] publish failed", err);
    }
  }
}

type ScenePayload = {
  scene_fingerprint: string;
  t_ms: number;
  scene_json: {
    elements: readonly unknown[];
    appState: Record<string, unknown>;
  };
};

/**
 * Replace any `image` element's data with bounding-box + placeholder.
 * R17 enforcement at the source — the agent's handler also strips
 * server-side as defense-in-depth.
 */
function stripFiles(elements: readonly unknown[]): unknown[] {
  return elements.map((element) => {
    if (typeof element !== "object" || element === null) return element;
    const e = element as Record<string, unknown>;
    if (e["type"] === "image") {
      // Preserve geometry so the scene parser still places the
      // placeholder correctly; drop fileId / status / scale.
      return {
        id: e["id"],
        type: "image",
        x: e["x"],
        y: e["y"],
        width: e["width"],
        height: e["height"],
      };
    }
    return element;
  });
}

/**
 * JSON.stringify with sorted keys — fingerprint must be deterministic
 * across element-property order changes Excalidraw makes during a
 * session (e.g., re-ordering `seed` and `versionNonce`).
 */
function stableStringify(value: unknown): string {
  return JSON.stringify(value, (_key, val) => {
    if (val !== null && typeof val === "object" && !Array.isArray(val)) {
      const sortedEntries = Object.entries(
        val as Record<string, unknown>,
      ).toSorted(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
      return Object.fromEntries(sortedEntries);
    }
    return val;
  });
}

/**
 * FNV-1a 32-bit hash over a string. Returns a lowercase hex string.
 * Cryptographic strength isn't needed for dedup fingerprinting — this
 * runs in microseconds with no async overhead.
 */
function fnv1aHex(input: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = (hash * 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

export const _testing = { stripFiles, stableStringify, fnv1aHex };
