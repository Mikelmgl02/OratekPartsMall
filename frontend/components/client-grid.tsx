'use client';
import { type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { AgGridReact, type AgGridReactProps } from 'ag-grid-react';
import type { ColDef, GetRowIdParams, ModelUpdatedEvent } from 'ag-grid-community';
import { fixedColumns, gridLocale, motionGridTheme, swipeableHeader } from '@/lib/ag-grid';
import { useWindowFill } from './grid-fill';

const HEADER = 42;

type Props<T> = {
  label: string;
  rows: T[];
  columns: ColDef<T>[];
  rowId: (row: T) => string;
  rowHeight?: number;
  /** The grid grows with its rows up to this many, then scrolls. */
  maxRows?: number;
  className?: string;
  context?: unknown;
  /** Like the admin and supplier grids: fill the window from where the grid starts, shrinking to its rows when they are few (maxRows is ignored).
   *  'parent': the layout around it sets the height (a full-height workspace); --rows-height says how tall its rows are, for a phone's stacked layout. */
  fill?: boolean | 'stacked' | 'parent';
  /** Shown instead of the grid when there are no rows. */
  empty?: ReactNode;
  gridProps?: Partial<AgGridReactProps<T>>;
};

// Short lists that arrive whole (price lists, a client's rules, pricing clients): sorted in the browser, sized to their rows.
export default function ClientGrid<T>({ label, rows, columns, rowId, rowHeight = 52, maxRows = 12, className = '', context, empty, gridProps, fill = false }: Props<T>) {
  const defaultColumn = useMemo<ColDef<T>>(() => ({ ...fixedColumns, sortable: true, resizable: true }), []);
  // Popups (column menus, tooltips) open on the page rather than inside the grid frame, so a grid of a row or two never clips them; in a
  // dialog they open in the dialog, whose top layer would otherwise cover them (as in ServerGrid).
  const frame = useRef<HTMLDivElement>(null);
  const [popupParent, setPopupParent] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => { if (frame.current) setPopupParent(frame.current.closest('dialog') ?? document.body); }, [rows.length > 0]);
  useEffect(() => frame.current ? swipeableHeader(frame.current) : undefined, [rows.length > 0]);
  const available = useWindowFill(frame, [rows.length > 0], fill === 'stacked');
  // 'parent': how tall the rows really are (autoHeight cells grow some), so a phone's stacked layout shows them all.
  const [body, setBody] = useState<number | null>(null);
  const measureRows = useCallback((event: ModelUpdatedEvent<T>) => {
    let total = 0; event.api.forEachNodeAfterFilterAndSort(node => { total += node.rowHeight ?? rowHeight; });
    setBody(previous => previous === total ? previous : total);
  }, [rowHeight]);
  const measure = fill === 'parent' ? measureRows : undefined;
  if (!rows.length && empty) return <>{empty}</>;
  const content = HEADER + Math.max(1, fill ? rows.length : Math.min(rows.length, maxRows)) * rowHeight + 2;
  const height = fill === 'parent' ? undefined : fill ? Math.min(available ?? content, content) : content;
  return <div ref={frame} className={`quotation-grid admin-grid ${fill === 'parent' ? 'fill-parent' : ''} ${className}`} role="region" aria-label={label}
    style={{ height, ['--admin-row-height' as string]: `${rowHeight - 2}px`, ...(fill === 'parent' ? { ['--rows-height' as string]: `${HEADER + Math.max(rowHeight, body ?? rows.length * rowHeight) + 2 + 18}px` } : {}) }}>
    <AgGridReact<T> theme={motionGridTheme} localeText={gridLocale} rowData={rows} columnDefs={columns} defaultColDef={defaultColumn} getRowId={({ data }: GetRowIdParams<T>) => rowId(data)}
      rowHeight={rowHeight} context={context} ensureDomOrder suppressColumnVirtualisation enableBrowserTooltips loadThemeGoogleFonts={false} popupParent={popupParent} suppressDragLeaveHidesColumns onModelUpdated={measure} {...gridProps}/>
  </div>;
}
