import { useEffect, useRef } from "react";

/** setInterval with a stable callback; pass null/0 as ms to disable. */
export function useInterval(fn, ms) {
  const ref = useRef(fn);
  useEffect(() => {
    ref.current = fn;
  });
  useEffect(() => {
    if (!ms) return undefined;
    const timer = setInterval(() => ref.current(), ms);
    return () => clearInterval(timer);
  }, [ms]);
}
