// Libro de movimientos del proveedor (mall.inventory_history): the stock ledger and the loads (Excel, apiag-cloud, manual) that changed items.
export type LedgerOrigin = 'quotation' | 'excel' | 'apiag' | 'manual';
export type LedgerItem = { id: string; supplier_invent_id: string; codigo: string; brand: string; description: string; source: string };
export type LedgerRow = {
  id: number; created_at: string; kind: string; kind_label: string; origin: LedgerOrigin; origin_label: string; item: LedgerItem;
  stock_delta: number; reserved_delta: number; available_delta: number; reported_after: number; reserved_after: number; available_after: number;
  reference: string; actor: string;
  order: { id: string; reference: string; client_name: string; status: string; status_label: string; revision: number } | null;
  run: { id: string; label: string; name: string; origin: string } | null;
};
export type LedgerRun = {
  id: string; origin: 'excel' | 'apiag' | 'manual'; origin_label: string; label: string; name: string; filename: string; mode: string; mode_label: string; started_at: string;
  finished_at: string | null; actor: string; rebuilt: boolean; import_job: string | null;
  counts: Partial<Record<'created' | 'updated' | 'stock' | 'withdrawn' | 'credit_units' | 'debit_units', number>>;
};
export type LedgerChange = {
  id: number; action: 'created' | 'updated' | 'stock' | 'withdrawn'; action_label: string; created_at: string; item: LedgerItem;
  fields: { field: string; label: string; before: unknown; after: unknown }[]; reason: string | null; reason_label: string;
  quantity_before: number | null; quantity_after: number | null; delta: number | null;
  branches: { sucursal: string; nombre: string; before: number; after: number }[];
};
export const originLabels: Record<LedgerOrigin, string> = { quotation: 'Cotización confirmada', excel: 'Carga Excel', apiag: 'apiag-cloud', manual: 'Actualización manual' };
export const kindLabels: Record<string, string> = { sync: 'Ajuste de existencias', reserve: 'Reserva', release: 'Liberación de reserva', settle: 'Pedido descontado por el proveedor',
  shortfall: 'Faltante físico', delivery: 'Entrega' };
export const actionLabels: Record<LedgerChange['action'], string> = { created: 'Creados', updated: 'Modificados', stock: 'Existencias', withdrawn: 'Retirados' };
