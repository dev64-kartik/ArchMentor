"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Room } from "livekit-client";

import { CanvasScenePublisher } from "./canvas-scene-publisher";

import "@excalidraw/excalidraw/index.css";

/**
 * Dynamic-imported Excalidraw mount that publishes scene changes over
 * LiveKit (`canvas-scene` topic) to the mentor agent.
 *
 * - SSR is disabled because Excalidraw touches `window` at module scope
 *   (Next 15 + React 19 hydration check would otherwise fail).
 * - The `CanvasScenePublisher` owns throttling, fingerprint dedup, and
 *   `files` stripping — this component just wires onChange to it.
 * - R19 disclosure: when the candidate pastes one or more image
 *   elements, render a non-blocking banner explaining that the mentor
 *   does not yet see images. Per-element overlays were considered and
 *   deferred — a single banner is robust to canvas pan/zoom and
 *   accessible without DOM bounds polling.
 */
const Excalidraw = dynamic(
  () => import("@excalidraw/excalidraw").then((m) => m.Excalidraw),
  { ssr: false, loading: () => <CanvasLoadingFallback /> },
);

type Props = {
  /** LiveKit room — `null` until the candidate joins. Publishing is a
   * no-op while disconnected. */
  room: Room | null;
};

export function ExcalidrawCanvas({ room }: Props) {
  // Publisher binds to a Room. When `room` flips from null → Room, we
  // construct one; we dispose on unmount or room change to flush any
  // pending throttle timer.
  const publisherRef = useRef<CanvasScenePublisher | null>(null);
  const [hasImageElement, setHasImageElement] = useState(false);

  useEffect(() => {
    if (!room) return;
    const publisher = new CanvasScenePublisher(room);
    publisherRef.current = publisher;
    return () => {
      publisher.dispose();
      if (publisherRef.current === publisher) {
        publisherRef.current = null;
      }
    };
  }, [room]);

  // Page Visibility API: force-flush a pending scene when the tab is
  // about to become hidden so the mentor doesn't lose the candidate's
  // last-second sketch. The publisher's flushNow() is idempotent.
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        void publisherRef.current?.flushNow();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  const onChange = useMemo(
    () => (elements: readonly unknown[]) => {
      // Track image-element presence for the R19 banner. Cheap pass —
      // the throttled publish is downstream.
      let sawImage = false;
      for (const element of elements) {
        if (
          typeof element === "object" &&
          element !== null &&
          (element as { type?: unknown }).type === "image"
        ) {
          sawImage = true;
          break;
        }
      }
      setHasImageElement((prev) => (prev === sawImage ? prev : sawImage));
      void publisherRef.current?.onSceneChange(elements);
    },
    [],
  );

  return (
    <div className="relative h-full w-full">
      {hasImageElement ? <ImageDisclosureBanner /> : null}
      <Excalidraw onChange={onChange} />
    </div>
  );
}

function ImageDisclosureBanner() {
  // Anchored to the bottom of the canvas so it can never collide with
  // Excalidraw's top toolbar or its colour-picker popup.
  return (
    <div
      role="status"
      aria-live="polite"
      className={[
        "pointer-events-none absolute inset-x-0 bottom-2 z-10 mx-auto",
        "max-w-md rounded-md border border-amber-300 bg-amber-50/95 px-3 py-2",
        "text-xs text-amber-900 shadow-sm",
        "dark:border-amber-700 dark:bg-amber-950/90 dark:text-amber-100",
      ].join(" ")}
    >
      Mentor doesn&apos;t see images yet — describe them in text.
    </div>
  );
}

function CanvasLoadingFallback() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-neutral-500">
      Loading canvas…
    </div>
  );
}
