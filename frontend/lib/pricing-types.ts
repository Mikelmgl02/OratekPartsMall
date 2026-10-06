// Supplier-only pricing and draft payloads. Never rendered on client screens.
import type { MembershipPermission } from './types';

export type PriceSource = 'engine' | 'previous' | 'manual' | 'none';
export type QuantitySource = 'requested' | 'available' | 'previous' | 'manual' | 'assistant';
export type ExceptionSeverity = 'block' | 'confirm' | 'info';
// Confirm-level alerts count as reviewed only while the context they were confirmed with stays the same.
export type DraftException = { code: string; severity: ExceptionSeverity; message: string; context: string; acknowledged: boolean };
// The 409 of a refused publish lists only what stops it; order-level alerts carry a null line.
export type PublishException = DraftException & { order_line_id: string | null };
export type DraftAcknowledgement = { code: string; context: string };
export type DraftStock = { reported_quantity: number; reserved_quantity: number; available_quantity: number; shortfall: number; updated_at: string };
export type Currency = 'USD' | 'PAB';
export type PriceListRef = { id: string; code: string; name: string; currency: Currency };
// How the engine reached a suggestion (supplier-only): the list price, the single rule applied (client agreement first, then the most
// specific target), parity, the rules it discarded and the next volume breaks.
export type PriceStep = { kind: 'parity'; from: Currency; to: Currency; rate: string }
  | { kind: 'rule'; rule_id: string; rule_revision: number; name: string; scope: string; target: string; target_value: string; min_quantity: number;
      action: 'discount' | 'net_price'; value: string; before: string | null; after: string };
export type PriceExplanation = {
  engine: string; evaluated_on: string; quantity_basis: number; currency: Currency; unit_price: string | null;
  status: 'priced' | 'missing' | 'no_price_list' | 'identity_changed' | 'currency_mismatch' | 'out_of_range';
  profile: { id: string; version: number; discount_percent: string; archived_list: string | null } | null; price_list: PriceListRef | null;
  base: { price_list_id: string; price_list_code: string; fallback: boolean; entry_revision: number; unit_price: string; currency: Currency; updated_at: string | null } | null;
  steps: PriceStep[]; discarded: { rule_id: string; name: string; reason: string }[]; next_breaks: { min_quantity: number; unit_price: string; rule_name: string }[];
  floor_price: string | null; codes: string[]; rounding: string; fingerprint: string;
};
export type DraftSuggestion = { unit_price: string | null; fingerprint: string; explanation: PriceExplanation };
// The client's private commercial profile as the draft sees it (supplier-only).
export type DraftProfile = { exists: boolean; version: number; discount_percent: string; customer_code: string; internal_notes: string };
export type DraftPricing = { engine: string; configured: boolean; price_list: PriceListRef | null; profile?: DraftProfile; stale: number; fillable: number };
export type RepriceScope = 'blank' | 'engine' | 'lines';
export type RepricedLine = { order_line_id: string; previous_unit_price: string | null; unit_price: string | null; reason: 'quantity' | RepriceScope };
export type QuoteDraftLine = {
  order_line_id: string; codigo: string; brand: string; description: string; requested: number; stock: DraftStock | null;
  quantity: number | null; quantity_source: QuantitySource; unit_price: string | null; price_source: PriceSource;
  // Null when the supplier has no price list yet.
  suggestion: DraftSuggestion | null; note: string; exceptions: DraftException[];
};
export type QuoteDraft = {
  persisted: boolean; draft_version: number; status: 'editing' | 'review_requested'; base_quotation_id: string | null;
  currency: Currency; terms: string; terms_origin: 'saved' | 'previous' | 'profile' | 'none'; updated_at: string | null; updated_by: { name: string } | null;
  // Approval request (status review_requested): who asked and when. Editing quantities, prices, currency or terms withdraws it.
  review_requested_by?: { name: string } | null; review_requested_at?: string | null;
  // can_publish false: the supplier requires a higher permission to send quotations, so the member requests approval instead.
  permissions: { can_publish: boolean; publish_requires: MembershipPermission }; pricing: DraftPricing; lines: QuoteDraftLine[]; order_exceptions: DraftException[];
  summary: { blocking: number; to_confirm: number; info: number; total: string; line_count: number };
  // Prices the save that returned this draft recalculated from the supplier's list; empty on reads.
  repriced: RepricedLine[];
};
export type QuoteDraftReprice = { save_id: string; expected_draft_version: number; scope: RepriceScope; order_line_ids?: string[] };
export type QuoteDraftEnvelope = { editable: boolean; draft: QuoteDraft | null };
export type QuoteDraftLineChange = { order_line_id: string; quantity?: number | null; unit_price?: string | null; note?: string;
  acknowledge?: DraftAcknowledgement[]; revoke?: string[] };
export type QuoteDraftSave = { save_id: string; expected_draft_version: number; currency?: 'USD' | 'PAB'; terms?: string; lines?: QuoteDraftLineChange[];
  request_review?: boolean };
export type AuditPriceSource = PriceSource | 'unspecified';
export type TraceException = { code: string; severity: ExceptionSeverity; context: string; acknowledged_by: { name: string } | null; acknowledged_at: string | null };
export type AcceptCheck = { result: 'ok' | 'blocked' | 'accepted_with_shortfall'; at: string; lines: {
  order_line_id: string; quantity: number; available_at_quote: number | null; available_at_accept: number | null;
  identity_ok_at_quote: boolean; identity_ok_at_accept: boolean; shortfall: boolean }[] };
export type QuotationTrace = { available: false } | {
  available: true; quotation_id: string; revision: number; draft_version: number | null; publisher: { name: string };
  publisher_permission: MembershipPermission; published_at: string; settings_snapshot: Record<string, unknown>;
  order_exceptions: TraceException[]; accept_check: AcceptCheck | null;
  lines: { order_line_id: string; codigo: string; brand: string; description: string; quantity: number; unit_price: string;
    available_at_quote: number | null; identity_ok_at_quote: boolean; available_at_accept: number | null; suggested_price: string | null;
    price_source: AuditPriceSource; quantity_source: QuantitySource; engine_fingerprint: string; explanation: Record<string, unknown>;
    exceptions: TraceException[] }[];
};

// "Precios › Configuración": the supplier's private settings. Permission fields and the assistant change only by the owner; writes send only changed fields.
export type SupplierPolicy = 'confirm' | 'block';
export type PricingSettingsValues = { default_currency: Currency; usd_pab_parity: boolean; config_min_permission: MembershipPermission; publish_min_permission: MembershipPermission;
  over_request_policy: SupplierPolicy; over_stock_policy: SupplierPolicy; accept_shortfall_policy: 'block' | 'allow'; prefill_quantity: 'requested' | 'available'; assistant_enabled: boolean };
export type PricingSettings = PricingSettingsValues & { version: number; updated_at: string | null; updated_by: { name: string } | null; permission: MembershipPermission;
  can_configure: boolean; can_manage_permissions: boolean };
export type PricingSettingsUpdate = Partial<PricingSettingsValues> & { expected_version: number };

// Supplier price lists and the private price grid ("Precios › Listas de precios").
export type PriceList = { id: string; code: string; name: string; currency: Currency; is_default: boolean; active: boolean; version: number;
  priced_count: number; missing_count: number; created_at: string; updated_at: string };
export type PriceListsEnvelope = { can_configure: boolean; item_count: number; results: PriceList[] };
export type PriceCell = { unit_price: string; revision: number; updated_at: string };
export type PriceRow = { supplier_item_id: string; supplier_invent_id: string; codigo: string; brand: string; description: string;
  matching_status: 'pending' | 'matched' | 'review'; discount_group: string; floor_price: string | null; item_pricing_revision: number | null;
  prices: Record<string, PriceCell>; updated_at: string | null };
export type PriceChangeInput = { list_id: string; supplier_item_id: string; unit_price: string | null; expected_revision: number | null };
export type ItemPricingInput = { supplier_item_id: string; discount_group?: string; floor_price?: string | null; expected_revision: number | null };
export type PriceWriteSummary = { created: number; updated: number; removed: number; unchanged: number; floor_updates: number; line_updates: number };
export type PriceWriteResult = { summary: PriceWriteSummary; rows: PriceRow[] };
export type PriceWriteConflict = { detail: string; rows: PriceRow[]; conflicts: { supplier_item_id: string; list_id: string | null; current: Record<string, unknown> }[] };
export type PriceHistoryEntry = { id: number; kind: 'list_price' | 'floor_price' | 'discount_group'; price_list: { id: string; code: string } | null;
  old_value: string | null; new_value: string | null; old_text: string; new_text: string; source: 'manual' | 'import' | 'api'; reference: string;
  actor: { name: string }; created_at: string };
export type PricingAuditEntry = { id: number; kind: string; actor: { name: string }; client: { id: string; name: string } | null;
  order: { id: string; reference: string } | null; object_id: string; payload: Record<string, unknown>; created_at: string };

// Price import from Excel (supplier-only): the preview and progress of an owner-bound job.
export type PriceImportCounts = { new_prices: number; increases: number; decreases: number; unchanged: number; removed: number };
export type PriceImportIssue = { row: number; column: string; message: string };
export type PriceImportChange = { row: number; supplier_invent_id: string; codigo: string; description: string; kind: PriceHistoryEntry['kind'];
  list: string | null; current: string | null; new: string | null; change_percent: string | null; action: 'create' | 'update' | 'delete'; big_change: boolean };
export type PriceImportResult = {
  job_id: string; supplier_id: string; filename: string; status: 'ready' | 'review' | 'importing' | 'completed'; expires_at: string; valid: boolean; imported: boolean;
  summary: PriceImportCounts & { source_rows: number; valid_rows: number; rejected_rows: number; big_changes: number; line_updates: number; floor_updates: number;
    lists: Record<string, PriceImportCounts> };
  applied_summary: PriceWriteSummary; id_column: string; code_column: string; columns: string[];
  lists: { header: string; code: string; new: boolean; archived: boolean; skipped: boolean }[];
  lists_to_create: { code: string; name: string; currency: Currency; is_default: boolean; header: string }[]; ignored_columns: string[];
  preview: PriceImportChange[]; preview_count: number; errors: PriceImportIssue[]; error_count: number; warnings: PriceImportIssue[]; warning_count: number;
  requires: { acknowledge_big_changes: boolean; confirm_new_lists: boolean };
  progress: { batch_size: number; total_batches: number; completed_batches: number; total_rows: number; processed_rows: number; next_batch: number | null };
};

// Pair profiles, commercial rules and the simulator (supplier-only; clients never see any of it).
export type PriceListSummary = PriceListRef & { active: boolean };
export type AccountRef = { id: string; name: string };
export type PricingClient = { id: string; name: string; order_count: number; last_order_at: string; profile: { exists: boolean; version: number; customer_code: string;
  price_list: PriceListSummary | null; discount_percent: string; preferred_currency: '' | Currency; rule_count: number } };
export type PricingClientPage = { count: number; next: string | null; previous: string | null; results: PricingClient[]; default_list: PriceListSummary | null };
export type ClientProfile = { client: AccountRef; exists: boolean; id: string | null; version: number; price_list_id: string | null; price_list: PriceListSummary | null;
  fallback_to_default: boolean; discount_percent: string; preferred_currency: '' | Currency; default_terms: string; internal_notes: string; customer_code: string;
  effective_list: PriceListSummary | null; effective_currency: Currency; order_count: number; last_order_at: string; updated_at: string | null;
  updated_by: { name: string } | null; can_configure: boolean };
export type ClientProfileUpdate = { operation_id: string; expected_version: number; price_list_id?: string | null; fallback_to_default?: boolean; discount_percent?: string;
  preferred_currency?: '' | Currency; default_terms?: string; internal_notes?: string; customer_code?: string };
export type RuleScope = 'all' | 'client';
export type RuleTarget = 'all' | 'line' | 'brand' | 'item';
export type RuleKind = 'discount' | 'net_price';
export type RuleItem = { id: string; supplier_invent_id: string; codigo: string; brand: string; description: string };
export type PricingRule = { id: string; name: string; scope: RuleScope; client: AccountRef | null; target: RuleTarget; target_value: string; item: RuleItem | null;
  kind: RuleKind; value: string; currency: '' | Currency; min_quantity: number; valid_from: string | null; valid_until: string | null; active: boolean;
  validity: 'active' | 'scheduled' | 'expired' | 'archived'; note: string; revision: number; created_by: { name: string }; updated_by: { name: string };
  created_at: string; updated_at: string };
export type PricingRulePage = { count: number; next: string | null; previous: string | null; results: PricingRule[]; can_configure: boolean };
export type PricingRuleInput = { name: string; scope: RuleScope; client_id: string | null; target: RuleTarget; target_value: string; item_id: string | null; kind: RuleKind;
  value: string; currency: '' | Currency; min_quantity: number; valid_from: string | null; valid_until: string | null; note: string };
export type SimulatedLine = { supplier_item_id: string; supplier_invent_id: string; codigo: string; brand: string; description: string; discount_group: string;
  quantity: number; unit_price: string | null; line_total: string | null; status: PriceExplanation['status']; explanation: PriceExplanation };
export type SimulationResult = { client: AccountRef | null; profile: { exists: boolean; version: number; discount_percent: string } | null;
  price_list: PriceListSummary | null; currency: Currency; on_date: string; engine: string; lines: SimulatedLine[] };
