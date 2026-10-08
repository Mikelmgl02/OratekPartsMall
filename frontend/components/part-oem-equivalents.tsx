'use client';

import { useEffect, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { request } from '@/lib/types';
import { PartOEMEquivalents, partNumberViaLabels, referenceStatusLabels } from '@/lib/oem-reference-types';

// Read-only in the SKU editor: the SKU's OEM numbers and the aftermarket codes it reaches through them. The codes belong to the numbers
// (Biblioteca OEM), so saving the SKU never writes them.
export default function PartOEMEquivalentsSection({ partId }: { partId: string }) {
  const [data, setData] = useState<PartOEMEquivalents | null>(null); const [error, setError] = useState(''); const [retry, setRetry] = useState(0);
  useEffect(() => {
    let live = true; setError('');
    request<PartOEMEquivalents>(`/api/management/catalog/${partId}/oem-equivalents`)
      .then(result => { if (live) setData(result); })
      .catch(e => { if (live) setError(e instanceof Error ? e.message : 'No se pudieron cargar las equivalencias por número OEM.'); });
    return () => { live = false; };
  }, [partId, retry]);
  const names = new Map(data?.numbers.map(entry => [entry.reference.id, `${entry.reference.manufacturer} ${entry.reference.code}`]) ?? []);
  return <section className="catalog-codes part-oem-equivalents" aria-label="Equivalencias por número OEM">
    <h3>Equivalencias por número OEM<small>SOLO LECTURA</small></h3>
    <p>Códigos de marcas de repuesto equivalentes a los números OEM de este SKU. Pertenecen al número, no al SKU: se consultan en la Biblioteca OEM y no cambian al guardar.</p>
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => setRetry(value => value + 1)}>Intentar de nuevo</button></div>}
    {!data && !error && <div className="loading"><LoaderCircle size={16} className="spin"/>Cargando equivalencias…</div>}
    {data && (data.numbers.length ? <>
      <ul className="part-oem-numbers" aria-label="Números OEM del SKU">{data.numbers.map(entry => <li key={entry.reference.id}>
        <strong>{entry.reference.manufacturer} {entry.reference.code}</strong>
        <span>{entry.via.map(via => partNumberViaLabels[via]).join(' · ')} · {referenceStatusLabels[entry.reference.status]}</span>
      </li>)}</ul>
      {data.cross_references.length ? <ul className="part-oem-codes" aria-label="Códigos equivalentes">{data.cross_references.map(code => <li key={`${code.brand}:${code.code}`}
        className={code.on_sku ? 'on-sku' : ''} title={[...code.numbers.map(id => names.get(id)).filter(Boolean), ...code.citations].join('\n')}>
        {code.brand} {code.code}{code.on_sku && <small>TAMBIÉN ALTERNO</small>}</li>)}</ul>
        : <p className="form-footnote">Sus números OEM aún no tienen equivalencias de posventa.</p>}
    </> : <p className="form-footnote">Este SKU no tiene números OEM: ni alternos OEM, ni un código que nombre un número de la biblioteca, ni vínculos de un catálogo.</p>)}
  </section>;
}
