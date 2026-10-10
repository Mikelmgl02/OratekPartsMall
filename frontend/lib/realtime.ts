// The deal inbox's websocket (mall/realtime.py, /ws/inbox/): "deal X changed" nudges, so open pages fetch at once instead of waiting for
// their next poll, and poll far less while it is connected. It carries no deal content; the page reads it through the usual API.
import { inboxChanged } from './inbox';

export const DEAL_EVENT = 'motionpartes:deal';
export type DealNudge = { orderId: string; kind: 'message' | 'event' | 'read' };
let live = false;
export function realtimeLive() { return live; }

// Opens the socket and keeps it open (reconnecting with backoff up to 30 s) until the returned function closes it.
export function connectRealtime(onLive: (live: boolean) => void) {
  let socket: WebSocket | null = null, stopped = false, attempt = 0, timer: ReturnType<typeof setTimeout> | undefined;
  const state = (value: boolean) => { live = value; onLive(value); };
  const open = () => {
    socket = new WebSocket(`${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/inbox/`);
    socket.onopen = () => { attempt = 0; state(true); inboxChanged(); };  // catch up on whatever happened while disconnected
    socket.onmessage = event => {
      let data: { kind?: DealNudge['kind']; order_id?: string | null };
      try { data = JSON.parse(event.data); } catch { return; }
      inboxChanged();
      if (data.order_id && data.kind) window.dispatchEvent(new CustomEvent<DealNudge>(DEAL_EVENT, { detail: { orderId: data.order_id, kind: data.kind } }));
    };
    socket.onclose = () => {
      if (live) state(false);
      if (!stopped) timer = setTimeout(open, Math.min(30000, 1000 * 2 ** attempt++));
    };
  };
  open();
  return () => { stopped = true; clearTimeout(timer); socket?.close(); if (live) state(false); };
}

// Runs on every nudge about this deal (of these kinds).
export function onDealNudge(orderId: string, kinds: DealNudge['kind'][], run: () => void) {
  const listener = (event: Event) => {
    const detail = (event as CustomEvent<DealNudge>).detail;
    if (detail.orderId === orderId && kinds.includes(detail.kind)) run();
  };
  window.addEventListener(DEAL_EVENT, listener);
  return () => window.removeEventListener(DEAL_EVENT, listener);
}
