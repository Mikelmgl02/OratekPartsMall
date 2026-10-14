'use client';
import { type ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { AgGridReact, type AgGridReactProps } from 'ag-grid-react';
import type { ColDef, GetRowIdParams } from 'ag-grid-community';
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
  /** Like the admin and supplier grids: fill the window from where the grid starts, shrinking to its rows when they are few (maxRows is ignored). */
  fill?: boolean | 'stacked';
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
  if (!rows.length && empty) return <>{empty}</>;
  const content = HEADER + Math.max(1, fill ? rows.length : Math.min(rows.length, maxRows)) * rowHeight + 2;
  const height = fill ? Math.min(available ?? content, content) : content;
  return <div ref={frame} className={`quotation-grid admin-grid ${className}`} role="region" aria-label={label} style={{ height, ['--admin-row-height' as string]: `${rowHeight - 2}px` }}>
    <AgGridReact<T> theme={motionGridTheme} localeText={gridLocale} rowData={rows} columnDefs={columns} defaultColDef={defaultColumn} getRowId={({ data }: GetRowIdParams<T>) => rowId(data)}
      rowHeight={rowHeight} context={context} ensureDomOrder suppressColumnVirtualisation enableBrowserTooltips loadThemeGoogleFonts={false} popupParent={popupParent} suppressDragLeaveHidesColumns {...gridProps}/>
  </div>;
}
