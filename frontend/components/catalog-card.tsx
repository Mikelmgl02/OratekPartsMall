import { ArrowRight, Clock3, Store } from 'lucide-react';
import type { Part } from '@/lib/types';
import { CatalogPhoto } from './part-image';
import CatalogSkeleton from './catalog-skeleton';
import StockStatus from './stock-status';
import FavoriteButton from './favorite-button';

export default function CatalogCard({ part, preview, busy, onOpen, saved = false, saving = false, onFavorite, unavailable = false }: { part: Part; preview: boolean; busy: boolean; onOpen: () => void; saved?: boolean; saving?: boolean; onFavorite?: () => void; unavailable?: boolean }) {
  const codes = [...new Set(part.codes.map(code => code.code))].filter(code => code !== part.sku);
  const suppliers = part.availability?.supplier_count || 0;
  const updated = part.availability?.updated_at ? new Date(part.availability.updated_at) : null;
  return <article className={`part-card ${busy ? 'is-loading' : ''} ${onFavorite ? 'has-favorite' : ''}`} aria-label={`Repuesto ${part.sku}`} inert={busy || undefined} aria-hidden={busy || undefined}>
    {busy && <CatalogSkeleton/>}
    <button className="part-image" tabIndex={-1} aria-hidden="true" onClick={onOpen} disabled={unavailable}>
      <span className="part-image-label">{preview ? 'EJEMPLO' : 'CATÁLOGO'}</span><CatalogPhoto image={part.images?.[0]} alt={`${part.name} ${part.description} ${part.category}`} thumbnail/>
    </button>
    {onFavorite && <FavoriteButton sku={part.sku} saved={saved} busy={saving} onToggle={onFavorite} compact/>}
    <div className="part-card-body">
      <h3><button onClick={onOpen} disabled={unavailable}>{part.name && part.name !== part.sku ? part.name : part.sku}</button></h3>
      {part.name && part.name !== part.sku && <span className="part-sku">{part.sku}</span>}
      <p className={`part-description ${part.description ? '' : 'missing'}`} title={part.description || undefined}>{part.description || 'DESCRIPCIÓN PENDIENTE'}</p>
      <div className="part-stock-row">{unavailable ? <span className="catalog-stock-status unknown">FUERA DEL CATÁLOGO</span> : <StockStatus availability={part.availability} preview={preview}/>}</div>
      <div className="part-card-bottom"><span><Store size={14}/>{unavailable ? 'GUARDADO EN TU LISTA' : preview ? 'PROVEEDORES DE EJEMPLO' : suppliers ? `${suppliers} ${suppliers === 1 ? 'PROVEEDOR CON STOCK' : 'PROVEEDORES CON STOCK'}` : 'SIN PROVEEDORES CON STOCK'}</span></div>
      <dl className="part-classification"><div><dt>GRUPO</dt><dd title={part.category}>{part.category || 'SIN CLASIFICAR'}</dd></div><div><dt>SUBGRUPO</dt><dd title={part.subcategory}>{part.subcategory || 'SIN CLASIFICAR'}</dd></div></dl>
      <div className="part-alternates" aria-label={`Alternos de ${part.sku}`}><span className="part-alternates-label">{codes.length ? `ALTERNOS (${codes.length})` : 'SIN ALTERNOS REGISTRADOS'}</span>{codes.length > 0 && <div>{codes.slice(0, 2).map(code => <span className="part-code" title={code} key={code}>{code}</span>)}{codes.length > 2 && <button className="part-code more-codes" disabled={unavailable} aria-label={`Ver los ${codes.length} alternos de ${part.sku}`} onClick={onOpen}>+{codes.length - 2}</button>}</div>}</div>
      {updated && Number.isFinite(updated.getTime()) && <div className="part-stock-updated" title={updated.toLocaleString('es-PA')}><Clock3 size={12} aria-hidden="true"/>STOCK ACTUALIZADO · {updated.toLocaleDateString('es-PA', {day:'numeric', month:'short'})}</div>}
      <button className="part-card-cta" disabled={unavailable} aria-label={`Ver ${part.name || part.sku}`} onClick={onOpen}>Ver opciones<ArrowRight size={14} aria-hidden="true"/></button>
    </div>
  </article>;
}
