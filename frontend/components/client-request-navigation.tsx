'use client';
import { ClipboardList, ShoppingBag } from 'lucide-react';

export default function ClientRequestNavigation({ selected, count, canViewSent, onDraft, onSent }: {
  selected: 'draft' | 'sent'; count: number; canViewSent: boolean; onDraft: () => void; onSent: () => void;
}) {
  return <nav className="client-request-navigation" aria-label="Tus solicitudes">
    <button type="button" aria-current={selected === 'draft' ? 'page' : undefined} onClick={onDraft}><ShoppingBag size={17}/>Borrador<span>{count}</span></button>
    <button type="button" aria-current={selected === 'sent' ? 'page' : undefined} disabled={!canViewSent} onClick={onSent}><ClipboardList size={17}/>Enviadas</button>
  </nav>;
}
