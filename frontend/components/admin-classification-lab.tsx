'use client';

import { useId, useRef, useState } from 'react';
import { Search, X } from 'lucide-react';
import Modal from './modal';
import ClassificationWorkbench from './classification-workbench';
import { UppercaseInput } from './uppercase-field';
import { request, type Page } from '@/lib/types';
import { type OEMLine } from '@/lib/line-types';

export default function ClassificationLab({ onClose, onChanged, initialLine }: { onClose: () => void; onChanged: () => void; initialLine?: OEMLine }) {
  const searchId = useId();
  const searchInput = useRef<HTMLInputElement>(null);
  const [applying, setApplying] = useState(false);
  const [notice, setNotice] = useState('');
  const [query, setQuery] = useState(initialLine?.sku ?? '');
  const [rows, setRows] = useState<OEMLine[]>(initialLine ? [initialLine] : []);
  const [selected, setSelected] = useState<OEMLine | null>(initialLine ?? null);
  const [busy, setBusy] = useState(false);
  const [searched, setSearched] = useState(false);
  const [count, setCount] = useState(0);
  const [error, setError] = useState('');
  async function find() {
    setNotice(''); setBusy(true); setError(''); setSelected(null); setRows([]); setSearched(false);
    try {
      const params = new URLSearchParams({ search: query.trim(), estado: 'publicada' });
      const data = await request<Page<OEMLine>>(`/api/management/lines?${params}`);
      setRows(data.results.slice(0, 20)); setCount(data.count); setSearched(true);
    } catch (error) { setError(error instanceof Error ? error.message : 'No se pudo consultar el catálogo.'); }
    finally { setBusy(false); }
  }
  return <Modal title="Clasificación" wide className="classification-modal" onClose={() => { if (!applying) onClose(); }}>
    <p className="modal-description">OEM a la izquierda; códigos aftermarket asociados a la derecha. Selecciona solo las equivalencias que verificaste.</p>
    <details className="classification-change"><summary>Cambiar ficha de inventario</summary>
    <form className="stack-form" onSubmit={event => { event.preventDefault(); void find(); }}>
      <label htmlFor={searchId}>Buscar código o descripción</label>
      <div className="classification-search-field">
        <UppercaseInput id={searchId} ref={searchInput} value={query} onChange={event => setQuery(event.target.value)} maxLength={120} disabled={busy || applying}/>
        {query && <button type="button" className="icon-button" aria-label="Limpiar código o descripción" disabled={busy || applying} onClick={() => { setQuery(''); searchInput.current?.focus(); }}><X size={16} aria-hidden="true"/></button>}
      </div>
      <button className="button soft" disabled={busy || applying || !query.trim()}><Search size={16}/>{busy ? 'Buscando…' : 'Buscar en inventario'}</button>
    </form>
    {error && <p className="notice error" role="alert">{error}</p>}
    {searched && <p role="status">{count ? `${count} resultados. Se muestran hasta 20; precisa la búsqueda si es necesario.` : 'No hay líneas publicadas para esta búsqueda. Prueba con otro código o descripción.'}</p>}
    <div className="stack-form">{rows.map(line => <button key={line.id} type="button" className="button soft" aria-pressed={selected?.id === line.id} disabled={applying} onClick={() => { setSelected(line); setNotice(''); }}>{line.sku} · {line.description}</button>)}</div>
    </details>
    {notice && <p role="status" className="notice success">{notice}</p>}
    {selected && <section className="stack-form" aria-label="Prueba del código seleccionado">
      <h3>{selected.sku}</h3><p>{selected.description}</p>
      <ClassificationWorkbench key={`${selected.id}:${selected.version}`} line={selected} onBusy={setApplying} onSaved={line => {
        setRows(current => [line, ...current.filter(row => row.id !== selected.id && row.id !== line.id)]);
        setSelected(line); setNotice(`Clasificación guardada. OEM principal: ${line.sku}. El código anterior se conserva como referencia aftermarket.`); onChanged();
      }}/>

    </section>}
    <p className="form-footnote">Desconocido significa aún no confirmado. Nada cambia hasta confirmar la aplicación.</p>
  </Modal>;
}
