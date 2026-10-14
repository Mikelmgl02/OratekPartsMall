'use client';

import { useEffect, useState } from 'react';
import { Library, Tags } from 'lucide-react';
import { OEMLibraryPanel } from './admin-oem-library';
import { CatalogSuffixesPanel } from './admin-catalog-suffixes';

type Tab = 'library' | 'suffixes';
const tabs: { key: Tab; slug: string; label: string; Icon: typeof Library }[] = [
  { key: 'library', slug: 'biblioteca', label: 'Biblioteca OEM', Icon: Library },
  { key: 'suffixes', slug: 'sufijos', label: 'Sufijos', Icon: Tags },
];

// Every OEM tool in one admin section. ?pestana= deep-links a tab (any other value, such as the retired revision or automatica, opens
// the library); switching tabs rewrites it without reloading the page.
export default function AdminOEMSection() {
  const [tab, setTab] = useState<Tab>('library');
  useEffect(() => {
    const match = tabs.find(item => item.slug === new URL(window.location.href).searchParams.get('pestana'));
    if (match) setTab(match.key);
  }, []);
  function open(next: Tab) {
    setTab(next);
    const url = new URL(window.location.href);
    url.searchParams.set('pestana', tabs.find(item => item.key === next)!.slug);
    window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
  }
  return <section className="inventory-panel admin-panel suffix-modal oem-section" aria-label="OEM">
    <div className="admin-toolbar"><div><h2>OEM</h2><p>Biblioteca de números OEM y sufijos de códigos.</p></div></div>
    <div className="admin-inventory-tabs oem-section-tabs" role="group" aria-label="Herramientas OEM">{tabs.map(({ key, label, Icon }) =>
      <button key={key} className={tab === key ? 'selected' : ''} aria-pressed={tab === key} onClick={() => open(key)}><Icon size={14} aria-hidden="true"/>{label}</button>)}</div>
    {tab === 'library' ? <OEMLibraryPanel/> : <CatalogSuffixesPanel/>}
  </section>;
}
