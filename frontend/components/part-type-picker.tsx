'use client';

import { useEffect, useState } from 'react';
import { PartType } from '@/lib/technical-types';
import { Page, request } from '@/lib/types';
import { UppercaseInput } from './uppercase-field';

export default function PartTypePicker({ onSelect }: { onSelect: (type: PartType) => void }) {
  const [search, setSearch] = useState(''); const [data, setData] = useState<Page<PartType> | null>(null); const [error, setError] = useState(''); const [retry, setRetry] = useState(0);
  useEffect(() => { let live = true; setData(null); setError(''); const timer = setTimeout(() => request<Page<PartType>>(`/api/management/part-types?search=${encodeURIComponent(search)}`).then(d => { if (live) setData(d); }).catch(e => { if (live) setError(e instanceof Error ? e.message : 'No se pudieron cargar los subgrupos.'); }), 250); return () => { live = false; clearTimeout(timer); }; }, [search, retry]);
  return <details className="part-type-picker"><summary>Elegir un subgrupo existente</summary><label>Buscar tipo de repuesto<UppercaseInput value={search} onChange={e => setSearch(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') e.preventDefault(); }} placeholder="GRUPO O SUBGRUPO"/></label><label>Subgrupo<select value="" onChange={e => { const type = data?.results.find(t => t.id === e.target.value); if (type) onSelect(type); }}><option value="">{data ? 'Selecciona un subgrupo' : 'Cargando…'}</option>{data?.results.map(t => <option key={t.id} value={t.id}>{t.category} / {t.name}</option>)}</select></label>{data?.next && <p>Afina la búsqueda para encontrar más subgrupos.</p>}{error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => setRetry(v => v + 1)}>Reintentar</button></div>}</details>;
}
