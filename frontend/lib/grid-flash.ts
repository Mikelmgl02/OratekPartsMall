import type { GridApi, IRowNode } from 'ag-grid-community';

// Patching a server-side grid row in place and flashing only what moved (the approach of EssaMobileApp's ssrm-row-patch).

// Value-aware equality, not reference equality: the API builds fresh arrays and objects (codes, identity, images) on every read,
// so a reference check would flash every such column on every refresh, and a flash that fires on everything means nothing.
export function valuesEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (a == null || b == null || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    const other = b as unknown[];
    return a.length === other.length && a.every((value, index) => valuesEqual(value, other[index]));
  }
  const left = a as Record<string, unknown>, right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key => valuesEqual(left[key], right[key]));
}

/** The keys whose value differs between the row the grid holds and the fresh one. */
export function changedFields(previous: object | undefined, next: object): string[] {
  const before = (previous ?? {}) as Record<string, unknown>, after = next as Record<string, unknown>;
  return Object.keys(after).filter(key => key !== 'id' && !valuesEqual(before[key], after[key]));
}

/**
 * Put fresh rows into the grid and flash the cells that changed (700 ms, fading over 1.2 s). fieldColumns maps a row field to the
 * column showing it; a row whose change lands on no visible column flashes whole, so an update is never invisible, and a row that
 * did not change does not flash. Returns how many rows changed.
 */
export function patchRows<T extends { id: string }>(api: GridApi<T>, rows: T[], fieldColumns: Record<string, string>): number {
  const visible = new Set((api.getAllGridColumns() ?? []).map(column => column.getColId()));
  const wholeRows: IRowNode<T>[] = [];
  let changed = 0;
  for (const row of rows) {
    const node = api.getRowNode(String(row.id));
    if (!node) continue;
    const fields = changedFields(node.data, row);
    if (!fields.length) continue;
    changed += 1;
    node.setData(row);
    const columns = [...new Set(fields.map(field => fieldColumns[field]).filter(column => column && visible.has(column)))];
    if (columns.length) api.flashCells({ rowNodes: [node], columns, flashDuration: 700, fadeDuration: 1200 });
    else wholeRows.push(node);
  }
  if (wholeRows.length) api.flashCells({ rowNodes: wholeRows, flashDuration: 700, fadeDuration: 1200 });
  return changed;
}
