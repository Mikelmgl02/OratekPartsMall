'use client';

import { RefObject, useCallback, useEffect, useRef, useState } from 'react';
import type { GridApi } from 'ag-grid-community';
import { patchRows } from './grid-flash';

export type EditStatus = { kind: 'saving' | 'saved' | 'error'; text: string };
type Pending<T> = { fields: Record<string, unknown>; restore: Partial<T> };
type Options<T> = {
  grid: RefObject<GridApi<T> | null>;
  /** A row field and the column showing it: what flashes when a fresh row comes in. */
  fieldColumns: Record<string, string>;
  /** The rows as the server has them now. */
  reread: (ids: string[]) => Promise<T[]>;
  /** One batch, all rows or none; resolves to the saved rows. */
  save: (rows: (Record<string, unknown> & { id: string })[]) => Promise<T[]>;
  /** The editable columns, refreshed when a save starts and ends so their editable cue follows. */
  cueColumns: string[];
  /** Re-read the rows on screen every POLL_MS. */
  poll: boolean;
  /** What a row is called in the status: { one: 'número', many: 'números' }. */
  noun: { one: string; many: string };
};

const BATCH_MS = 500;  // edits collect this long after the last one, then save together
const POLL_MS = 15000;  // the rows on screen are re-read this often, so changes made elsewhere show (and flash)
const errorText = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio. Inténtalo de nuevo.';

/**
 * EssaMobileApp's grid editing, shared by the Inventario and Biblioteca OEM grids. Edits and pastes collect for BATCH_MS and save as one
 * batch, all rows or none; the saved rows come back into the grid and only the cells that changed flash. A refused batch puts every one
 * of its cells back, then re-reads them (another person's change shows). The rows on screen are re-read every POLL_MS, but never while
 * a cell is being edited or saved: a poll must never move a cell under the cursor.
 */
export function useGridEdits<T extends { id: string }>(options: Options<T>) {
  const settings = useRef(options);
  settings.current = options;
  const { grid, poll } = options;
  const [status, setStatus] = useState<EditStatus | null>(null);
  const saving = useRef(false);
  const pending = useRef(new Map<string, Pending<T>>());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const busy = useCallback(() => saving.current || pending.current.size > 0 || !!grid.current?.getEditingCells().length, [grid]);
  const isSaving = useCallback(() => saving.current, []);
  const refreshRows = useCallback(async (ids: string[], quiet = false) => {
    if (!ids.length || !grid.current) return 0;
    const rows = await settings.current.reread(ids.slice(0, 100));
    if (!grid.current || (quiet && busy())) return 0;
    return patchRows(grid.current, rows, settings.current.fieldColumns);
  }, [grid, busy]);
  const setSaving = useCallback((value: boolean) => {
    saving.current = value;
    grid.current?.refreshCells({ columns: settings.current.cueColumns, force: true });
  }, [grid]);
  const flush = useCallback(async () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    const batch = new Map(pending.current);
    pending.current.clear();
    if (!batch.size) return;
    const { noun } = settings.current;
    const name = (count: number) => `${count} ${count === 1 ? noun.one : noun.many}`;
    // Compared with each cell's value before the batch, so the edited cells flash once the server keeps them.
    const before = new Map([...batch].map(([id, entry]) => [id, { ...(grid.current?.getRowNode(id)?.data ?? {}), ...entry.restore }]));
    setSaving(true);
    setStatus({ kind: 'saving', text: `Guardando ${name(batch.size)}…` });
    try {
      const saved = await settings.current.save([...batch].map(([id, entry]) => ({ id, ...entry.fields })));
      if (grid.current) {
        for (const row of saved) { const node = grid.current.getRowNode(row.id); if (node?.data) node.setData({ ...node.data, ...before.get(row.id) } as T); }
        patchRows(grid.current, saved, settings.current.fieldColumns);
      }
      setStatus({ kind: 'saved', text: `${name(saved.length)} ${saved.length === 1 ? 'guardado' : 'guardados'}.` });
    } catch (error) {
      for (const [id, entry] of batch) { const node = grid.current?.getRowNode(id); if (node?.data) node.setData({ ...node.data, ...entry.restore }); }
      setStatus({ kind: 'error', text: errorText(error) });
      refreshRows([...batch.keys()], true).catch(() => 0);
    } finally { setSaving(false); }
  }, [grid, refreshRows, setSaving]);
  /** One cell edit: fields go to the server, restore holds each field's value before the batch (kept from the first edit). */
  const queue = useCallback((id: string, fields: Record<string, unknown>, restore: Partial<T>) => {
    const entry = pending.current.get(id) ?? { fields: {}, restore: {} };
    Object.assign(entry.fields, fields);
    for (const [key, value] of Object.entries(restore)) if (!(key in entry.restore)) (entry.restore as Record<string, unknown>)[key] = value;
    pending.current.set(id, entry);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void flush(), BATCH_MS);
  }, [flush]);

  useEffect(() => {
    if (!poll) return;
    const tick = () => {
      const api = grid.current;
      if (!api || document.visibilityState !== 'visible' || busy()) return;
      const ids = api.getRenderedNodes().map(node => node.data?.id).filter((id): id is string => !!id);
      refreshRows([...new Set(ids)], true).catch(() => 0);  // a failed poll waits for the next one
    };
    const interval = setInterval(tick, POLL_MS);
    const wake = () => { if (document.visibilityState === 'visible') tick(); };
    document.addEventListener('visibilitychange', wake);
    return () => { clearInterval(interval); document.removeEventListener('visibilitychange', wake); };
  }, [poll, grid, busy, refreshRows]);
  useEffect(() => () => { if (pending.current.size) void flush(); }, [flush]);  // leaving the page still saves what was typed

  return { status, setStatus, busy, isSaving, refreshRows, queue, flush };
}
