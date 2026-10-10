'use client';
import { useSyncExternalStore } from 'react';

// Whether the server now runs a newer build than this page (a deploy, or a local rebuild): checked every 5 minutes while the page is shown,
// and when it comes back into view, at most every 30 seconds. One check for the whole page; every notice reads it.
const CURRENT = process.env.NEXT_PUBLIC_DEPLOYMENT_ID || '';
const EVERY = 5 * 60_000, AT_MOST = 30_000;
let state = { newer: false, dismissed: false };
const listeners = new Set<() => void>();
const set = (changes: Partial<typeof state>) => { state = { ...state, ...changes }; listeners.forEach(listener => listener()); };
let started = false, last = 0;

async function check() {
  if (document.visibilityState !== 'visible' || Date.now() - last < AT_MOST) return;
  last = Date.now();
  try {
    const data = await (await fetch('/build-version', { cache: 'no-store' })).json();
    if (data?.deployment && data.deployment !== CURRENT) set({ newer: true });
  } catch { /* the next check */ }
}

export function startUpdateCheck() {
  if (started || !CURRENT) return;  // a dev server has no builds to compare
  started = true;
  setInterval(() => void check(), EVERY);
  document.addEventListener('visibilitychange', () => void check());
  window.addEventListener('focus', () => void check());
  void check();
}

export function useUpdate() {
  const current = useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => state, () => state);
  return { shown: current.newer && !current.dismissed, dismiss: () => set({ dismissed: true }) };
}
