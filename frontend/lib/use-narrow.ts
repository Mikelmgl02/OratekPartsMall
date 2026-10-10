'use client';
import { type RefObject, useEffect, useState } from 'react';

// Whether an element is narrower than `width` (a phone, the conversations panel's side at its usual width), following its resizes. Grids
// use it to trade columns for stacked cells.
export function useNarrow(element: RefObject<HTMLElement | null>, width: number) {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const target = element.current;
    if (!target) return;
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width) setNarrow(entry.contentRect.width < width); });
    observer.observe(target);
    return () => observer.disconnect();
  }, [element, width]);
  return narrow;
}
