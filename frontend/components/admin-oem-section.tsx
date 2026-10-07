'use client';

import { useEffect, useState } from 'react';
import { Check, Library, ListChecks, Tags, Wand2, X } from 'lucide-react';
import { OEMLibraryPanel } from './admin-oem-library';
import { OEMRunsPanel } from './admin-oem-runs';
import { CatalogSuffixesPanel } from './admin-catalog-suffixes';
import OEMReview from './admin-oem-review';
import CatalogGrouping from './admin-catalog-grouping';
import type { CatalogGroupingCandidate } from '@/lib/types';

type Tab = 'library' | 'review' | 'auto' | 'suffixes';
const tabs: { key: Tab; slug: string; label: string; Icon: typeof Library }[] = [
  { key: 'library', slug: 'biblioteca', label: 'Biblioteca OEM', Icon: Library },
  { key: 'review', slug: 'revision', label: 'Revisión OEM', Icon: ListChecks },
  { key: 'auto', slug: 'automatica', label: 'Aplicación automática', Icon: Wand2 },
  { key: 'suffixes', slug: 'sufijos', label: 'Sufijos', Icon: Tags },
];

// Every OEM tool in one admin section. ?pestana= deep-links a tab; switching tabs rewrites it without reloading the page.
export default function AdminOEMSection() {
  const [tab, setTab] = useState<Tab>('library');
  const [suffixSearch, setSuffixSearch] = useState('');
  const [grouping, setGrouping] = useState<CatalogGroupingCandidate | null>(null);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    const match = tabs.find(item => item.slug === new URL(window.location.href).searchParams.get('pestana'));
    if (match) setTab(match.key);
  }, []);
  function open(next: Tab, search = '') {
    setTab(next); setSuffixSearch(search); setNotice('');
    const url = new URL(window.location.href);
    url.searchParams.set('pestana', tabs.find(item => item.key === next)!.slug);
    window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
  }
  return <section className="inventory-panel admin-panel suffix-modal oem-section" aria-label="OEM">
    <div className="admin-toolbar"><div><h2>OEM</h2><p>Biblioteca de números OEM, revisión de propuestas, aplicación automática y sufijos del buscador OEM.</p></div></div>
    <div className="admin-inventory-tabs oem-section-tabs" role="group" aria-label="Herramientas OEM">{tabs.map(({ key, label, Icon }) =>
      <button key={key} className={tab === key ? 'selected' : ''} aria-pressed={tab === key} onClick={() => open(key)}><Icon size={14} aria-hidden="true"/>{label}</button>)}</div>
    {notice && <div className="notice success" role="status"><Check size={16}/>{notice}<button className="icon-button" aria-label="Cerrar notificación" onClick={() => setNotice('')}><X size={15}/></button></div>}
    {tab === 'library' ? <OEMLibraryPanel/>
      : tab === 'review' ? <OEMReview onChanged={() => undefined} onOpenSuffixes={token => open('suffixes', token)} onOpenGrouping={setGrouping}/>
      : tab === 'auto' ? <OEMRunsPanel onChanged={() => undefined}/>
      : <CatalogSuffixesPanel key={suffixSearch} initialSearch={suffixSearch}/>}
    {grouping && <CatalogGrouping initialFamily={grouping} onClose={() => setGrouping(null)}
      onSaved={(targetSku, count) => { setGrouping(null); setNotice(`${count} ${count === 1 ? 'SKU agrupado' : 'SKU agrupados'} bajo ${targetSku}.`); }}/>}
  </section>;
}
