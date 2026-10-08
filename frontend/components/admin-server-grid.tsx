'use client';
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { AgGridReact } from 'ag-grid-react';
import type { ColDef, GetRowIdParams, GridState, IServerSideDatasource, StateUpdatedEvent } from 'ag-grid-community';
import { gridLocale, motionGridTheme } from '@/lib/ag-grid';
import { Page, request } from '@/lib/types';

// The management lists page by 50 (DRF PAGE_SIZE): grid block N is API page N + 1.
export const GRID_BLOCK = 50;
const HEADER = 42;
const MAX_HEIGHT = 680;
const KEPT: (keyof GridState)[] = ['columnOrder', 'columnSizing', 'columnVisibility', 'sort'];

function savedState(key: string): GridState | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(key) || 'null');
    return value && typeof value === 'object' ? value : undefined;
  } catch { return undefined; }
}

type Props<T> = {
  /** Column order, widths, visibility and sort are remembered per grid in this browser. */
  storageKey: string;
  label: string;
  path: string;
  /** Query parameters for the API (search, filters); blank values are left out. Changing them reloads from the first row. */
  params: Record<string, string>;
  columns: ColDef<T>[];
  rowId: (row: T) => string;
  /** colId -> the API ordering fields it sorts by ('category,subcategory'); other columns do not sort. */
  ordering: Record<string, string>;
  /** Bumps after an edit: the loaded rows are read again and the scroll position stays. */
  revision: number;
  rowHeight: number;
  context?: unknown;
  empty: ReactNode;
  onCount?: (count: number | null) => void;
};

// AG Grid Enterprise server-side row model over a paginated management list: rows load in blocks while scrolling, sorting runs
// on the server, and the whole list stays one grid however long it is.
export default function AdminServerGrid<T>({ storageKey, label, path, params, columns, rowId, ordering, revision, rowHeight, context, empty, onCount }: Props<T>) {
  const grid = useRef<AgGridReact<T>>(null);
  const [count, setCount] = useState<number | null>(null);
  const [error, setError] = useState('');
  // The datasource reads the current filters from refs: AG Grid keeps one datasource and asks it again after a refresh.
  const current = useRef({ params, ordering, generation: 0 });
  current.current.params = params; current.current.ordering = ordering;
  const key = `motionpartes.grid.${storageKey}.v2`;
  const initialState = useMemo(() => typeof window === 'undefined' ? undefined : savedState(key), [key]);
  const datasource = useMemo<IServerSideDatasource<T>>(() => ({
    getRows(rows) {
      const generation = current.current.generation;
      const query = new URLSearchParams({ page: String(Math.floor((rows.request.startRow ?? 0) / GRID_BLOCK) + 1) });
      for (const [name, value] of Object.entries(current.current.params)) if (value) query.set(name, value);
      const sort = rows.request.sortModel.flatMap(item => (current.current.ordering[item.colId] || '').split(',').filter(Boolean)
        .map(field => item.sort === 'desc' ? `-${field}` : field));
      if (sort.length) query.set('ordering', sort.join(','));
      request<Page<T>>(`${path}?${query}`).then(page => {
        if (generation === current.current.generation) { setCount(page.count); setError(''); }
        rows.success({ rowData: page.results, rowCount: page.count });
      }).catch(caught => {
        if (generation === current.current.generation) setError(caught instanceof Error ? caught.message : 'No se pudo cargar la información.');
        rows.fail();
      });
    },
  }), [path]);
  const filters = JSON.stringify(params);
  const loadedFilters = useRef(filters);
  useEffect(() => {
    if (loadedFilters.current === filters) return;
    loadedFilters.current = filters;
    current.current.generation += 1;
    setCount(null);
    grid.current?.api?.refreshServerSide({ purge: true });
  }, [filters]);
  const loadedRevision = useRef(revision);
  useEffect(() => {
    if (loadedRevision.current === revision) return;
    loadedRevision.current = revision;
    grid.current?.api?.refreshServerSide({ purge: false });
  }, [revision]);
  useEffect(() => { onCount?.(count); }, [count, onCount]);
  function remember(event: StateUpdatedEvent<T>) {
    // Only what someone changed is kept: the grid also reports its own start, which would freeze today's default layout.
    if (event.sources.includes('gridInitializing')) return;
    const state = Object.fromEntries(KEPT.filter(name => event.state[name] !== undefined).map(name => [name, event.state[name]]));
    try { localStorage.setItem(key, JSON.stringify(state)); } catch { /* storage blocked: the layout is simply not remembered */ }
  }
  const height = count === null ? MAX_HEIGHT : Math.min(MAX_HEIGHT, HEADER + Math.max(count, 2) * rowHeight + 2);
  const defaultColumn = useMemo<ColDef<T>>(() => ({ sortable: false, resizable: true, suppressHeaderMenuButton: false }), []);
  const columnDefs = useMemo(() => columns.map(column => ({ ...column, sortable: !!column.colId && column.colId in ordering })), [columns, ordering]);
  return <div className="admin-grid-shell">
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => { setError(''); grid.current?.api?.refreshServerSide({ purge: true }); }}><RefreshCw size={14}/>Intentar de nuevo</button></div>}
    <div className="quotation-grid admin-grid" role="region" aria-label={label} style={{ height, ['--admin-row-height' as string]: `${rowHeight - 2}px` }}>
      <AgGridReact<T> ref={grid} theme={motionGridTheme} localeText={gridLocale} rowModelType="serverSide" serverSideDatasource={datasource}
        cacheBlockSize={GRID_BLOCK} maxBlocksInCache={40} blockLoadDebounceMillis={80} getRowId={({ data }: GetRowIdParams<T>) => rowId(data)}
        columnDefs={columnDefs} defaultColDef={defaultColumn} rowHeight={rowHeight} context={context} initialState={initialState} onStateUpdated={remember}
        cellSelection enableBrowserTooltips ensureDomOrder suppressColumnVirtualisation loadThemeGoogleFonts={false}/>
      {count === 0 && <div className="admin-grid-empty">{empty}</div>}
    </div>
  </div>;
}
