'use client';
import { RefreshCw, X } from 'lucide-react';
import { useUpdate } from '@/lib/update-check';

// "Hay una versión nueva": a page left open while a newer build went live offers to reload. Rendered by the app shell and inside every
// dialog too, since an open dialog's top layer would cover the page's.
export default function UpdateNotice() {
  const { shown, dismiss } = useUpdate();
  if (!shown) return null;
  return <div className="update-notice" role="status">
    <RefreshCw size={16}/><span>Hay una versión nueva de MotionPartes.</span>
    <button type="button" className="button primary small" onClick={() => window.location.reload()}>Recargar</button>
    <button type="button" className="icon-button" aria-label="Ahora no" title="Ahora no" onClick={dismiss}><X size={16}/></button>
  </div>;
}
