import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CanvasScenePublisher, _testing } from "./canvas-scene-publisher";
import type { Room } from "livekit-client";

const { stripFiles, stableStringify, fnv1aHex } = _testing;

// Minimal Room mock — only localParticipant.sendText is exercised.
function makeRoom(): { room: Room; sendText: ReturnType<typeof vi.fn> } {
  const sendText = vi.fn().mockResolvedValue(undefined);
  const room = {
    localParticipant: { sendText },
  } as unknown as Room;
  return { room, sendText };
}

function makeElements(id: string): unknown[] {
  return [{ id, type: "rectangle", x: 0, y: 0, width: 10, height: 10 }];
}

describe("stripFiles", () => {
  it("removes fileId and extra fields from image elements", () => {
    const input = [
      { id: "a", type: "image", x: 10, y: 20, width: 100, height: 50, fileId: "blob:abc", status: "loaded" },
    ];
    const result = stripFiles(input);
    expect(result).toEqual([
      { id: "a", type: "image", x: 10, y: 20, width: 100, height: 50 },
    ]);
  });

  it("passes non-image elements through unchanged", () => {
    const input = [{ id: "b", type: "rectangle", x: 0, y: 0, width: 5, height: 5 }];
    expect(stripFiles(input)).toEqual(input);
  });
});

describe("stableStringify", () => {
  it("produces the same output regardless of object key insertion order", () => {
    const a = stableStringify([{ z: 1, a: 2 }]);
    const b = stableStringify([{ a: 2, z: 1 }]);
    expect(a).toBe(b);
  });
});

describe("fnv1aHex", () => {
  it("returns an 8-character hex string", () => {
    expect(fnv1aHex("hello")).toMatch(/^[0-9a-f]{8}$/);
  });

  it("is stable for the same input", () => {
    expect(fnv1aHex("test")).toBe(fnv1aHex("test"));
  });

  it("differs for different inputs", () => {
    expect(fnv1aHex("scene-a")).not.toBe(fnv1aHex("scene-b"));
  });
});

describe("CanvasScenePublisher", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("deduplicates identical scenes (lastFingerprint stable)", async () => {
    const { room, sendText } = makeRoom();
    const publisher = new CanvasScenePublisher(room);
    const elements = [{ id: "x", type: "rectangle", x: 0, y: 0, width: 10, height: 10 }];

    // First call fires the leading-edge publish immediately.
    publisher.onSceneChange(elements);
    // Drain the async publish (sendText mock resolves as microtask).
    await Promise.resolve();
    // Now lastFingerprint is set. Advance past the trailing edge timer.
    vi.advanceTimersByTime(1100);
    await Promise.resolve();

    // Second call with identical elements — fingerprint matches, bail out early.
    publisher.onSceneChange(elements);
    vi.advanceTimersByTime(1100);
    await Promise.resolve();

    // Only the initial leading-edge publish fired; the second call was deduped.
    expect(sendText).toHaveBeenCalledTimes(1);
    publisher.dispose();
  });

  it("coalesces rapid changes within throttle window into one publish", () => {
    const { room, sendText } = makeRoom();
    const publisher = new CanvasScenePublisher(room);

    // First call fires the leading edge immediately.
    publisher.onSceneChange(makeElements("a"));
    // Rapid subsequent changes inside the 1s window — only last should stick.
    publisher.onSceneChange(makeElements("b"));
    publisher.onSceneChange(makeElements("c"));

    // Advance past the trailing edge.
    vi.advanceTimersByTime(1100);

    // Leading-edge fired once, trailing-edge fires once with "c".
    expect(sendText).toHaveBeenCalledTimes(2);
    publisher.dispose();
  });

  it("dispose flushes a pending scene (#57 invariant)", async () => {
    const { room, sendText } = makeRoom();
    const publisher = new CanvasScenePublisher(room);

    // First call triggers leading-edge publish (sendText call 1) and schedules trailing.
    publisher.onSceneChange([{ id: "a", type: "rect", x: 0, y: 0, width: 1, height: 1 }]);
    // Immediately push a new distinct scene — sits in pendingScene, timer running.
    publisher.onSceneChange([{ id: "b", type: "rect", x: 1, y: 1, width: 1, height: 1 }]);

    // dispose() before the timer fires — should flush the pending "b" scene.
    publisher.dispose();
    // Allow microtasks (flushNow is async) to settle.
    await Promise.resolve();
    await Promise.resolve();

    expect(sendText).toHaveBeenCalledTimes(2);
  });
});
