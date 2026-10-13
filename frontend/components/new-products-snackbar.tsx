'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { PackagePlus, X } from 'lucide-react';
import { request } from '@/lib/types';

// Administrators see on the storefront how many Productos nuevos wait for review (GET management/catalog/proposals/summary/), with a
// link to the list. The count is read when the page opens and again when the tab comes back. Closing it hides it for the rest of the
// visit in this tab, until a pending proposal is created, reopened or changed after that (pending_changed_at moves past the one it
// was closed at); approving, linking or rejecting never brings it back, editing one that stays pending does.
const NEW_PRODUCTS = '/administracion?seccion=inventario&vista=nuevos';
type Summary = { pending: number; pending_changed_at: string | null };
const dismissedKey = (userId: number) => `motionpartes:new-products-dismissed:${userId}`;
const time = (value: string | null | undefined) => { const parsed = value ? Date.parse(value) : NaN; return Number.isFinite(parsed) ? parsed : 0; };

function readDismissed(userId: number) {
  try { return time(sessionStorage.getItem(dismissedKey(userId))); } catch { return 0; }
}

export default function NewProductsSnackbar({ userId }: { userId: number }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [loaded, setLoaded] = useState(false);  // the first answer came, a count or an error
  const [dismissed, setDismissed] = useState(() => readDismissed(userId));
  const latest = useRef(0);
  useEffect(() => {
    let live = true;
    const load = () => {
      const call = ++latest.current;  // a slow answer never replaces a newer one
      request<Summary>('/api/management/catalog/proposals/summary')
        .then(data => { if (live && call === latest.current) setSummary({ pending: Math.max(0, Number(data.pending) || 0), pending_changed_at: data.pending_changed_at ?? null }); })
        .catch(() => { /* Only a reminder: the list itself shows any error. */ })
        .finally(() => { if (live && call === latest.current) setLoaded(true); });
    };
    const refresh = () => { if (document.visibilityState === 'visible') load(); };
    load();
    document.addEventListener('visibilitychange', refresh);
    return () => { live = false; document.removeEventListener('visibilitychange', refresh); };
  }, [userId]);
  const pending = summary?.pending ?? 0;
  const visible = pending > 0 && (!dismissed || time(summary?.pending_changed_at) > dismissed);
  function close() {
    const mark = summary?.pending_changed_at || new Date().toISOString();
    setDismissed(time(mark));
    try { sessionStorage.setItem(dismissedKey(userId), mark); } catch { /* Hidden until the page reloads. */ }
  }
  // The live region stays in the page so screen readers announce the reminder when it appears.
  return <div className="admin-snackbar-region" role="status" aria-live="polite" data-loaded={loaded ? '' : undefined}>
    {visible && <div className="admin-snackbar">
      <PackagePlus size={18} aria-hidden="true"/>
      <span>{pending === 1 ? 'Hay 1 producto nuevo por revisar.' : `Hay ${pending.toLocaleString('es-PA')} productos nuevos por revisar.`}</span>
      <Link href={NEW_PRODUCTS}>Revisar</Link>
      <button type="button" aria-label="Cerrar aviso de productos nuevos" onClick={close}><X size={16}/></button>
    </div>}
  </div>;
}
