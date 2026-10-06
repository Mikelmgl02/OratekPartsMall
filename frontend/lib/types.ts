export type Page<T> = { count: number; next: string | null; previous: string | null; results: T[] };
export type Account = { id: string; name: string; roles: string[]; capabilities: string[] };
export type SessionUser = { id: number; username: string; first_name: string; last_name: string; is_superuser: boolean };
export type AdminSection = 'inventory' | 'alternates' | 'accounts' | 'users' | 'invitations' | 'analytics' | 'templates' | 'applications';
export type Member = { id: number; user: number; account: string; permission: 'owner' | 'manager' | 'staff'; username: string; email: string; account_name: string; user_active: boolean; permission_label: string };
export type ManagedAccount = { id: string; name: string; active: boolean; roles: string[]; member_count: number; members: Member[] };
export type ManagedUser = SessionUser & { email: string; is_active: boolean; is_staff: boolean; date_joined: string; memberships: Member[] };
export type ManagedRole = { code: string; name: string; capability: string };
export type ManagedInvitation = { id: number; email: string; token: string; expires_at: string; accepted_at: string | null; status: 'pending' | 'accepted' | 'expired' };
export type CatalogAvailability = { status: 'unknown' | 'sold_out' | 'low' | 'medium' | 'high'; supplier_count: number; updated_at: string | null };
export type CatalogImage = { id: string; url: string; thumbnail_url: string; alt_text: string; position: number; width: number; height: number; size_bytes: number; created_at: string };
export type PartReference = { brand: string; code: string; kind?: 'alias' | 'oem' | 'manufacturer'; ref_type?: 'unknown' | 'oem' | 'company'; reference_source?: string };
export type Part = { images?: CatalogImage[]; id: string; sku: string; is_OEM?: boolean; name: string; description: string; category?: string; subcategory?: string; part_type?: string | null; codes: PartReference[]; availability?: CatalogAvailability };
export type WishlistEntry = { part: Part; saved_at: string; available: boolean };
export type WishlistState = { part_ids: string[]; count: number };
export type CatalogFacet = { value: string; label: string; count: number };
export type CatalogFacets = Record<'category' | 'subcategory' | 'availability' | 'supplier', CatalogFacet[]>;
export type CatalogFilters = { category: string[]; subcategory: string[]; availability: string; supplier: string[] };
export type CatalogPage = Page<Part> & { facets?: CatalogFacets };
export type OfferItem = { id: string; codigo: string; brand: string; description: string };
export type Offer = { supplier_id: string; supplier_name: string; in_stock: boolean; items?: OfferItem[]; selected_item?: OfferItem };
export type StockItem = { references?: { brand: string; code: string }[]; id: string; supplier_invent_id: string; part: string | null; codigo: string; brand: string; description: string; matching_status: 'pending' | 'matched' | 'review'; source: 'upload' | 'apiag'; reported_quantity: number; reserved_quantity: number; available_quantity: number; updated_at: string };
export type ManagedPart = Pick<Part, 'id' | 'sku' | 'is_OEM' | 'name' | 'description' | 'category' | 'subcategory' | 'part_type' | 'codes' | 'images'> & { active: boolean; stock_record_count: number; identity?: { ref_type: 'unknown' | 'oem' | 'company'; brand: string; status: 'oem' | 'choose_oem' | 'oem_available' | 'needs_oem' } };
export type ManagedStockItem = StockItem & { supplier: string; supplier_name: string; part_name: string | null; part_sku: string | null };
export type ManagedAlternate = PartReference & { id: number; part: string; part_name: string; part_sku: string; code: string; brand: string };
export type CatalogGroupingPart = Pick<Part, 'id' | 'sku' | 'name' | 'description' | 'category' | 'subcategory' | 'codes'> & { proposed_parent?: boolean };
export type CatalogGroupingCandidate = { target_sku: string; target: CatalogGroupingPart; sources: CatalogGroupingPart[]; source_skus: string[]; source_count: number; reason: string; warnings?: string[]; needs_review: boolean };
export type CatalogGroupingSuggestion = { source_sku: string; target_sku: string; category: string; subcategory: string; reason: string; confidence: number; source_reference: string; target_reference: string };
export type CatalogGroupingClassification = { target_sku: string; source_skus: string[]; category: string; subcategory: string; reason: string; confidence: number; suggestions: CatalogGroupingSuggestion[] };
export type CatalogImportSummary = { rows: number; created_skus: number; updated_skus: number; unchanged_skus: number; added_codes: number; skipped_rows?: number; rejected_rows?: number };
export type CatalogImportRecoveryRow = {
  row: number; column: 'SKU' | 'CODIGO_ALTERNO'; description: string; excel_value: string;
  suggested_code: string; candidates: string[]; reason: string;
  automatic: boolean; resolved: boolean; skipped: boolean; value: string;
};
export type CatalogImportResult = {
  valid: boolean; imported: boolean; summary: CatalogImportSummary;
  errors: { row: number; column: string; message: string }[]; error_count: number;
  warnings?: { row: number; column: string; message: string }[]; warning_count?: number;
  numeric_warning_count?: number; date_recovery_count?: number;
  recovery_rows?: CatalogImportRecoveryRow[]; recovery_count?: number;
  source_rows?: number; skipped_rows?: number[]; skipped_row_count?: number;
  rejected_row_count?: number; pending_error_count?: number;
  preview: { sku: string; name: string; description: string; category?: string; subcategory?: string; active: boolean; codes: { code: string; brand: string }[]; action: 'create' | 'update' | 'unchanged'; added_codes: number }[];
  preview_count: number; preview_token?: string; job_id?: string; filename?: string;
  status?: 'ready' | 'importing' | 'completed' | 'review'; applied_summary?: CatalogImportSummary;
  progress?: { total_batches: number; completed_batches: number; total_skus: number; processed_skus: number; batch_size: number };
};
export type CatalogImportIssueValues = {
  sku: string; name: string; description: string; code: string; brand: string;
  active: string; category: string; subcategory: string;
};
export type CatalogImportIssue = {
  id: string; job_id: string; filename: string; row: number;
  job_status?: CatalogImportResult['status'];
  can_repair?: boolean;
  values: CatalogImportIssueValues; original: Record<string, unknown>;
  errors: CatalogImportResult['errors']; recovery: CatalogImportRecoveryRow[];
  status: 'pending' | 'resolved'; part_id?: string | null;
};
export type CatalogImportIssuePage = Page<CatalogImportIssue> & { pending_count: number };
export type CatalogImportIssueResult = {
  valid: boolean; issue: CatalogImportIssue; errors?: CatalogImportResult['errors'];
  summary?: CatalogImportSummary; part_id?: string;
};
export type SupplierInventoryImportSummary = {
  source_rows: number; valid_rows: number; rejected_rows: number;
  created_items: number; updated_items: number; unchanged_items: number;
  credit_units: number; debit_units: number;
};
export type SupplierInventoryImportRow = {
  references?: { brand: string; code: string }[];
  row: number; supplier_invent_id: string; codigo: string; brand: string; description: string;
  current_quantity: number; uploaded_quantity: number; direction: 'credit' | 'debit' | 'none';
  movement_quantity: number; new_quantity: number; matching_status: StockItem['matching_status'];
  part_id: string | null; part_sku: string | null; action: 'create' | 'update' | 'unchanged';
};
export type SupplierInventoryImportResult = {
  job_id: string; supplier_id: string; expires_at: string; filename: string;
  status: 'ready' | 'importing' | 'completed' | 'review'; valid: boolean; imported: boolean;
  summary: SupplierInventoryImportSummary; applied_summary: SupplierInventoryImportSummary;
  preview: SupplierInventoryImportRow[]; preview_count: number;
  errors: { row: number; column: string; message: string }[]; error_count: number;
  warnings: { row: number; column: string; message: string }[]; warning_count: number;
  progress: { batch_size: number; total_batches: number; completed_batches: number; total_rows: number; processed_rows: number; next_batch: number | null };
};
export type LedgerEntry = { id: number; kind: string; quantity: number; direction: 'credit' | 'debit'; balance_type: 'stock' | 'reservation'; reserved_delta: number; reserved_direction: 'credit' | 'debit' | ''; reported_after: number; reserved_after: number; reference: string; created_at: string };
export type BasketLine = { key: string; part: Part; offer: Offer; quantity: number };
export const roleLabel: Record<string, string> = { supplier_retail: 'Proveedor minorista', supplier_wholesale: 'Proveedor mayorista', client_business: 'Cliente empresarial', client_walkin: 'Cliente particular' };
const fieldLabel: Record<string, string> = {
  username: 'Usuario', password: 'Contraseña', email: 'Correo electrónico', token: 'Código de invitación',
  update_id: 'ID de actualización', supplier_invent_id: 'ID de inventario del proveedor', codigo: 'Código del repuesto',
  brand: 'Marca', description: 'Descripción', source: 'Origen', quantity: 'Cantidad', part: 'Repuesto',
  name: 'Nombre', first_name: 'Nombre', last_name: 'Apellido', roles: 'Tipos de cuenta', permission: 'Permiso',
  user: 'Usuario', account: 'Cuenta', days: 'Vigencia', active: 'Estado de la cuenta', is_active: 'Estado del usuario',
  is_OEM: 'El SKU principal es OEM', sku: 'SKU interno', codes: 'Alternos', code: 'Código alterno', matching_status: 'Estado de coincidencia',
  file: 'Archivo Excel', mode: 'Acción', preview_token: 'Vista previa',
  category: 'Categoría', subcategory: 'Subcategoría',
};
function validationMessage(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(validationMessage).filter(Boolean).join(' ');
  if (value && typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key === 'non_field_errors' ? '' : `${fieldLabel[key] || 'Datos'}: `}${validationMessage(item)}`).join(' ');
  return '';
}
export class RequestError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (!(options?.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(url, { ...options, headers });
  if (response.ok && response.status === 204) return undefined as T;
  let data;
  try { data = await response.json(); } catch { throw new Error('No se pudo cargar esta página. Inténtalo de nuevo.'); }
  if (!response.ok) {
    const message = typeof data.detail === 'string' ? data.detail : validationMessage(data);
    throw new RequestError(message || 'Ocurrió un error. Inténtalo de nuevo.', response.status);
  }
  return data;
}
