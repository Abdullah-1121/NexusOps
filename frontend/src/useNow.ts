import { useEffect, useState } from "react";

/** Live ticking clock for in-flight UI (progress stepper, stage wait).
 *  Client-side only — never a server claim, just "now" rendered on a timer. */
export function useNow(intervalMs = 250): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}