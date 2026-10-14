'use client';
import { ClipboardList, History, ShoppingBag } from 'lucide-react';

// Cesta, Cotizaciones (the orders sent) and Actividad (the cesta's and the orders' ledgers).
export default function ClientRequestNavigation({ selected, count, canViewSent, onDraft, onSent, onActivity }: {
  selected: 'draft' | 'sent' | 'activity'; count: number; canViewSent: boolean; onDraft: () => void; onSent: () => void; onActivity: () => void;
}) {
  return <nav className="client-request-navigation" aria-label="Tus solicitudes">
    <button type="button" aria-current={selected === 'draft' ? 'page' : undefined} onClick={onDraft}><ShoppingBag size={17}/>Cesta<span>{count}</span></button>
    <button type="button" aria-current={selected === 'sent' ? 'page' : undefined} disabled={!canViewSent} onClick={onSent}><ClipboardList size={17}/>Cotizaciones</button>
    <button type="button" aria-current={selected === 'activity' ? 'page' : undefined} disabled={!canViewSent} onClick={onActivity}><History size={17}/>Actividad</button>
  </nav>;
}
