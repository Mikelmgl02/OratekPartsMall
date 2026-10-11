'use client';

import { AllCommunityModule, ModuleRegistry, themeQuartz, type ColDef, type DefaultMenuItem, type GetMainMenuItemsParams } from 'ag-grid-community';
import { CellSelectionModule, ClipboardModule, ColumnMenuModule, ContextMenuModule, ExcelExportModule, LicenseManager, RichSelectModule, ServerSideRowModelApiModule,
  ServerSideRowModelModule } from 'ag-grid-enterprise';
import { AG_GRID_LOCALE_ES } from '@ag-grid-community/locale';

const license = process.env.NEXT_PUBLIC_AG_GRID_LICENSE_KEY;
if (license) LicenseManager.setLicenseKey(license);
ModuleRegistry.registerModules([AllCommunityModule, CellSelectionModule, ClipboardModule, ColumnMenuModule, ContextMenuModule, ExcelExportModule,
  RichSelectModule, ServerSideRowModelModule, ServerSideRowModelApiModule]);

export const gridLocale = AG_GRID_LOCALE_ES;
export const motionGridTheme = themeQuartz.withParams({
  accentColor: '#6e954b', backgroundColor: '#ffffff', foregroundColor: '#344331',
  borderColor: '#dfe5d8', headerBackgroundColor: '#f1f5eb', headerTextColor: '#647858',
  fontFamily: 'DM Sans, sans-serif', fontSize: 12, headerFontSize: 11,
  spacing: 6, rowHeight: 56, headerHeight: 42, wrapperBorderRadius: 10,
  valueChangeValueHighlightBackgroundColor: '#dcf39e',  // flashCells: the brand lime, fading out
});

// Every grid always shows all its columns: none can be hidden, neither from the column menu (no "Elegir columnas") nor by dragging it out
// of the grid (each grid also sets suppressDragLeaveHidesColumns). Sorting, resizing, moving and "Restablecer columnas" stay.
function withoutChooser(params: GetMainMenuItemsParams): DefaultMenuItem[] {
  const items = params.defaultItems.filter(item => item !== 'columnChooser');
  return items.filter((item, index) => item !== 'separator' || (index > 0 && index < items.length - 1 && items[index - 1] !== 'separator'));
}
export const fixedColumns: Pick<ColDef, 'lockVisible' | 'mainMenuItems'> = { lockVisible: true, mainMenuItems: withoutChooser };

// Touch screens: a sideways swipe across a grid's header moves its columns, like a swipe across its rows (the grid never scrolls the
// header itself, it follows the rows). Passive listeners: the page's own scrolling is untouched. Returns the cleanup.
export function swipeableHeader(root: HTMLElement) {
  let last: number | null = null;
  const start = (event: TouchEvent) => {
    last = event.touches.length === 1 && (event.target as Element | null)?.closest('.ag-header') ? event.touches[0].clientX : null;
  };
  const move = (event: TouchEvent) => {
    if (last === null || event.touches.length !== 1) return;
    const x = event.touches[0].clientX, body = root.querySelector<HTMLElement>('.ag-center-cols-viewport');
    if (body) body.scrollLeft += last - x;
    last = x;
  };
  const end = () => { last = null; };
  root.addEventListener('touchstart', start, { passive: true });
  root.addEventListener('touchmove', move, { passive: true });
  root.addEventListener('touchend', end);
  root.addEventListener('touchcancel', end);
  return () => {
    root.removeEventListener('touchstart', start); root.removeEventListener('touchmove', move);
    root.removeEventListener('touchend', end); root.removeEventListener('touchcancel', end);
  };
}
