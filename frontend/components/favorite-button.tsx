import { Heart, LoaderCircle } from 'lucide-react';

export default function FavoriteButton({ sku, saved, busy, onToggle, compact = false }: {
  sku: string; saved: boolean; busy: boolean; onToggle: () => void; compact?: boolean;
}) {
  return <button className={`favorite-button ${compact ? 'compact' : ''} ${saved ? 'saved' : ''}`} aria-label={`${saved ? 'Quitar' : 'Guardar'} ${sku} ${saved ? 'de' : 'en'} favoritos`} aria-pressed={saved} disabled={busy} onClick={onToggle} title={saved ? 'Quitar de favoritos' : 'Guardar en favoritos'}>
    {busy ? <LoaderCircle size={18} className="spin" aria-hidden="true"/> : <Heart size={18} fill={saved ? 'currentColor' : 'none'} aria-hidden="true"/>}
    {!compact && <span>{saved ? 'En favoritos' : 'Guardar en favoritos'}</span>}
  </button>;
}
