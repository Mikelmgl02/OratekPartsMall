// Browser push notifications for the deal inbox (mall/push.py, public/sw.js). The browser's subscription belongs to whoever is signed in:
// signing in sends it again (the server moves it to them), signing out tells the server to stop, and turning it off also unsubscribes
// the browser and remembers the choice here.
import { request } from './types';

// install: an iPhone or iPad browser tab, where push needs MotionPartes added to the home screen first.
export type PushState = 'unsupported' | 'unconfigured' | 'install' | 'blocked' | 'off' | 'on';
const OFF_KEY = 'motionpartes:push:off';
const PATH = '/api/market/inbox/push';

export function pushSupported() {
  return typeof window !== 'undefined' && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

function remembered(off: boolean) {
  try { if (off) localStorage.setItem(OFF_KEY, '1'); else localStorage.removeItem(OFF_KEY); } catch { /* the choice lasts this visit */ }
}
function turnedOff() {
  try { return localStorage.getItem(OFF_KEY) === '1'; } catch { return false; }
}

function keyBytes(key: string) {
  const base64 = (key + '='.repeat((4 - key.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(base64), char => char.charCodeAt(0));
}
function sameKey(subscription: PushSubscription, key: string) {
  const current = subscription.options.applicationServerKey;
  if (!current) return false;
  const a = new Uint8Array(current), b = keyBytes(key);
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function iosTab() {
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const installed = window.matchMedia('(display-mode: standalone)').matches || (navigator as Navigator & { standalone?: boolean }).standalone === true;
  return ios && !installed;
}

async function current() {
  return (await navigator.serviceWorker.getRegistration('/'))?.pushManager.getSubscription() ?? null;
}

export async function pushStatus(): Promise<{ state: PushState; key: string }> {
  if (!pushSupported() && !(typeof window !== 'undefined' && iosTab())) return { state: 'unsupported', key: '' };
  const config = await request<{ enabled: boolean; public_key: string }>(PATH);
  if (!config.enabled) return { state: 'unconfigured', key: '' };
  if (!pushSupported()) return { state: 'install', key: config.public_key };
  if (Notification.permission === 'denied') return { state: 'blocked', key: config.public_key };
  const subscription = Notification.permission === 'granted' ? await current() : null;
  return { state: subscription && sameKey(subscription, config.public_key) ? 'on' : 'off', key: config.public_key };
}

export async function enablePush(key: string): Promise<PushState> {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') return permission === 'denied' ? 'blocked' : 'off';
  await navigator.serviceWorker.register('/sw.js');
  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  if (subscription && !sameKey(subscription, key)) { await subscription.unsubscribe(); subscription = null; }
  subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
  await request(PATH, { method: 'POST', body: JSON.stringify(subscription.toJSON()) });
  remembered(false);
  return 'on';
}

export async function disablePush(): Promise<PushState> {
  const subscription = await current();
  if (subscription) {
    await request(`${PATH}/remove`, { method: 'POST', body: JSON.stringify({ endpoint: subscription.endpoint }) });
    await subscription.unsubscribe();
  }
  remembered(true);
  return 'off';
}

// Signing in: this browser's existing subscription now notifies the person who signed in (unless they turned it off here).
export async function syncPush() {
  if (!pushSupported() || Notification.permission !== 'granted' || turnedOff()) return;
  try {
    const subscription = await current();
    if (subscription) await request(PATH, { method: 'POST', body: JSON.stringify(subscription.toJSON()) });
  } catch { /* the next sign-in tries again */ }
}

// Signing out: the server stops notifying this browser; the subscription stays for whoever signs in next.
export async function forgetPush() {
  if (!pushSupported()) return;
  try {
    const subscription = await current();
    if (subscription) await request(`${PATH}/remove`, { method: 'POST', body: JSON.stringify({ endpoint: subscription.endpoint }) });
  } catch { /* signing out goes on */ }
}
