'use client';

import { useEffect, useState } from 'react';
import { request } from './types';
import type { Grupo } from './line-types';

// The catalog's grupos and subgrupos (management/grupos), read once per page and shared by the screens that pick a subgrupo (Editar
// celdas, the line editor); the Grupos admin drops the copy after a write so the next reader asks again.
let cached: Promise<Grupo[]> | null = null;
export function loadGrupos(fresh = false) {
  if (fresh || !cached) cached = request<Grupo[]>('/api/management/grupos').catch(error => { cached = null; throw error; });
  return cached;
}
export function forgetGrupos() { cached = null; }

export type SubgrupoOption = { id: string; name: string; grupo: { id: string; nombre: string }; label: string };
// Every subgrupo as GRUPO / SUBGRUPO, the label the grid's cells and the pickers show, in the grupos' order.
export function subgrupoOptions(grupos: Grupo[]): SubgrupoOption[] {
  return grupos.flatMap(grupo => grupo.subgrupos.map(sub => ({ id: sub.id, name: sub.name, grupo: { id: grupo.id, nombre: grupo.nombre }, label: `${grupo.nombre} / ${sub.name}` })));
}

export function useGrupos(enabled = true) {
  const [grupos, setGrupos] = useState<Grupo[] | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let live = true; setError('');
    loadGrupos(retry > 0).then(rows => { if (live) setGrupos(rows); }).catch(caught => { if (live) setError(caught instanceof Error ? caught.message : 'No se pudieron cargar los grupos.'); });
    return () => { live = false; };
  }, [enabled, retry]);
  return { grupos, error, reload: () => setRetry(value => value + 1) };
}
