'use client';
import { type ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { AgGridReact } from 'ag-grid-react';
import type { AgGridReactProps } from 'ag-grid-react';
import type { ColDef, GetRowIdParams, GridApi, GridState, IServerSideDatasource, StateUpdatedEvent } from 'ag-grid-community';
import { gridLocale, motionGridTheme } from '@/lib/ag-grid';
import { Page, request } from '@/lib/types';
import { usePageViewport } from './app-shell';

// Lists page by 50 (DRF PAGE_SIZE) unless the endpoint says otherwise (prices: 100): grid block N is API page N + 1.
export const GRID_BLOCK = 50;
const HEADER = 42;
const MIN_HEIGHT = 360;
const BOTTOM_GAP = 26; // the panel's bottom padding stays visible under the grid
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
  /** Every page the current filters load, for lists that send more than rows (the OEM library's counts). */
  onPage?: (page: Page<T>) => void;
  /** Extra classes for the grid frame. */
  className?: string;
  /** The endpoint's page size; each grid block is one API page. */
  pageSize?: number;
  /** Extra AG Grid options (editing, clipboard, tooltips...). */
  gridProps?: Partial<AgGridReactProps<T>>;
  /** The grid API once ready, for callers that edit rows in place. */
  onReady?: (api: GridApi<T>) => void;
};

// Rows load in blocks while scrolling, sorting runs on the server, and the whole list stays one grid however long it is.
// AG Grid Enterprise server-side row model over a paginated list (admin and supplier panels).
export default function ServerGrid<T>({ storageKey, label, path, params, columns, rowId, ordering, revision, rowHeight, context, empty, onCount, onPage, className = '',
  pageSize = GRID_BLOCK, gridProps, onReady }: Props<T>) {
  const grid = useRef<AgGridReact<T>>(null);
  const frame = useRef<HTMLDivElement>(null);
  const viewport = usePageViewport();
  // The grid fills the window from where it starts down to the bottom edge (never under MIN_HEIGHT), and shrinks to its rows
  // when a search leaves only a few.
  const [fill, setFill] = useState<number | null>(null);
  const [count, setCount] = useState<number | null>(null);
  const [error, setError] = useState('');
  // The datasource reads the current filters from refs: AG Grid keeps one datasource and asks it again after a refresh.
  const current = useRef({ params, ordering, generation: 0 });
  current.current.params = params; current.current.ordering = ordering;
  const pageListener = useRef(onPage);
  pageListener.current = onPage;
  const key = `motionpartes.grid.${storageKey}.v2`;
  const initialState = useMemo(() => typeof window === 'undefined' ? undefined : savedState(key), [key]);
  const datasource = useMemo<IServerSideDatasource<T>>(() => ({
    getRows(rows) {
      const generation = current.current.generation;
      const query = new URLSearchParams({ page: String(Math.floor((rows.request.startRow ?? 0) / pageSize) + 1) });
      for (const [name, value] of Object.entries(current.current.params)) if (value) query.set(name, value);
      const sort = rows.request.sortModel.flatMap(item => (current.current.ordering[item.colId] || '').split(',').filter(Boolean)
        .map(field => item.sort === 'desc' ? `-${field}` : field));
      if (sort.length) query.set('ordering', sort.join(','));
      request<Page<T>>(`${path}?${query}`).then(page => {
        if (generation === current.current.generation) { setCount(page.count); setError(''); pageListener.current?.(page); }
        rows.success({ rowData: page.results, rowCount: page.count });
      }).catch(caught => {
        if (generation === current.current.generation) setError(caught instanceof Error ? caught.message : 'No se pudo cargar la información.');
        rows.fail();
      });
    },
  }), [path, pageSize]);
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
  useLayoutEffect(() => {
    // Found from the grid's own place in the DOM: on first load this effect runs before the shell attaches its viewport ref.
    const element = frame.current, scroller = element?.closest<HTMLElement>('.app-viewport') ?? viewport.current;
    if (!scroller || !element) return;
    const measure = () => {
      // In a dialog the page behind does not frame the grid: keep a fixed share of the window instead.
      if (element.closest('dialog')) { setFill(Math.max(MIN_HEIGHT, Math.min(560, Math.floor(window.innerHeight * 0.6)))); return; }
      const top = element.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
      setFill(Math.max(MIN_HEIGHT, Math.floor(scroller.clientHeight - top - BOTTOM_GAP)));
    };
    measure();
    // Anything above the grid can still move it (web fonts arriving, a notice, the tools wrapping): re-measure whenever the window
    // or the page content changes size. The grid's own new height settles on the same value, so this stops after one round.
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    if (scroller.firstElementChild) observer.observe(scroller.firstElementChild);
    let live = true;
    document.fonts?.ready.then(() => { if (live) measure(); });
    return () => { live = false; observer.disconnect(); };
  }, [viewport, count === 0, error]);
  const available = fill ?? 600;
  const height = count === null ? available : Math.min(available, HEADER + Math.max(count, count === 0 ? 4 : 2) * rowHeight + 2);
  const defaultColumn = useMemo<ColDef<T>>(() => ({ sortable: false, resizable: true, suppressHeaderMenuButton: false }), []);
  const columnDefs = useMemo(() => columns.map(column => ({ ...column, sortable: !!column.colId && column.colId in ordering })), [columns, ordering]);
  return <div className="admin-grid-shell">
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => { setError(''); grid.current?.api?.refreshServerSide({ purge: true }); }}><RefreshCw size={14}/>Intentar de nuevo</button></div>}
    <div ref={frame} className={`quotation-grid admin-grid ${className}`} role="region" aria-label={label} style={{ height, ['--admin-row-height' as string]: `${rowHeight - 2}px` }}>
      <AgGridReact<T> ref={grid} theme={motionGridTheme} localeText={gridLocale} rowModelType="serverSide" serverSideDatasource={datasource}
        cacheBlockSize={pageSize} maxBlocksInCache={40} blockLoadDebounceMillis={80} getRowId={({ data }: GetRowIdParams<T>) => rowId(data)}
        columnDefs={columnDefs} defaultColDef={defaultColumn} rowHeight={rowHeight} context={context} initialState={initialState} onStateUpdated={remember}
        cellSelection enableBrowserTooltips ensureDomOrder suppressColumnVirtualisation loadThemeGoogleFonts={false} onGridReady={event => onReady?.(event.api)} {...gridProps}/>
      {count === 0 && <div className="admin-grid-empty">{empty}</div>}
    </div>
  </div>;
}
