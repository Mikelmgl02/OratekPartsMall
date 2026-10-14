'use client';
import { type DependencyList, type RefObject, useLayoutEffect, useState } from 'react';
import { usePageViewport } from './app-shell';

export const MIN_GRID_HEIGHT = 360;
const BOTTOM_GAP = 26; // the panel's bottom padding stays visible under the grid

// The height that lets a grid fill the window from where it starts down to the bottom edge (never under MIN_GRID_HEIGHT), as the admin
// and supplier grids do; callers shrink it to their rows when a list is short. In a dialog it keeps a fixed share of the window instead.
// stacked: one of several grids down a page (the cesta's suppliers): each may take the window's whole height, wherever it starts.
// below: how much of what follows the grid should stay on screen under it (a note at the foot of its panel).
export function useWindowFill(frame: RefObject<HTMLElement | null>, deps: DependencyList = [], stacked = false, below?: () => number) {
  const viewport = usePageViewport();
  const [fill, setFill] = useState<number | null>(null);
  useLayoutEffect(() => {
    // Found from the grid's own place in the DOM: on first load this effect runs before the shell attaches its viewport ref.
    const element = frame.current, scroller = element?.closest<HTMLElement>('.app-viewport') ?? viewport.current;
    if (!scroller || !element) return;
    const measure = () => {
      if (element.closest('dialog')) { setFill(Math.max(MIN_GRID_HEIGHT, Math.min(560, Math.floor(window.innerHeight * 0.6)))); return; }
      const top = stacked ? 0 : element.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
      setFill(Math.max(MIN_GRID_HEIGHT, Math.floor(scroller.clientHeight - top - BOTTOM_GAP - (below?.() ?? 0))));
    };
    measure();
    // Anything above the grid can still move it (web fonts arriving, a notice, the tools wrapping): re-measure whenever the window
    // or the page content changes size. The grid's own new height settles on the same value, so this stops after one round.
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    if (scroller.firstElementChild) observer.observe(scroller.firstElementChild);
    let live = true;
    document.fonts?.ready.then(() => { if (live) measure(); });
    return () => { live = false; observer.disconnect(); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewport, stacked, below, ...deps]);
  return fill;
}
