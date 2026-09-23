import { useCallback, useEffect, useRef, useState } from "react";
import type { NexusEvent } from "./types";

/** Live event stream over the serve process's WebSocket (FR-6, D-11).
 *  Reconnects with backoff; the serve EventBus replays missed per-incident
 *  events on (re)connect, so a dropped socket never loses the story. */
export function useNexusSocket() {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<NexusEvent[]>([]);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let closed = false;
    let retry = 0;

    const connect = () => {
      if (closed) return;
      // Same origin in prod (FastAPI serves the build) and in dev (Vite proxy
      // forwards /ws with ws:true) — so a relative URL is always correct.
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      wsRef.current = ws;

      ws.onopen = () => {
        retry = 0;
        setConnected(true);
      };
      ws.onmessage = (msg) => {
        try {
          const ev = JSON.parse(msg.data) as NexusEvent;
          setEvents((prev) => [...prev, ev]);
        } catch {
          // a malformed frame is the server's bug; keep the socket alive
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, Math.min(1000 * 2 ** retry++, 8000));
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closed = true;
      wsRef.current?.close();
    };
  }, []);

  const send = useCallback(
    (payload: object) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
    },
    [],
  );

  /** The §5.4 human decision — same contract the dashboard socket accepts. */
  const decide = useCallback(
    (incident_id: string, decision: "approve" | "reject") => {
      send({ incident_id, decision, actor: "console-operator" });
    },
    [send],
  );

  return { connected, events, decide };
}