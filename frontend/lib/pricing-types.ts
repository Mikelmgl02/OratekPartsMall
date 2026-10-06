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
// How the engine reached a suggestion (supplier-only). Rule steps, discarded rules and volume breaks arrive with client rules.
export type PriceStep = { kind: 'parity'; from: Currency; to: Currency; rate: string }
  | { kind: 'rule'; rule_id: string; rule_revision: number; name: string; scope: string; target: string; target_value: string; min_quantity: number;
      action: 'discount' | 'net_price'; value: string; before: string | null; after: string };
export type PriceExplanation = {
  engine: string; evaluated_on: string; quantity_basis: number; currency: Currency; unit_price: string | null;
  status: 'priced' | 'missing' | 'no_price_list' | 'identity_changed' | 'currency_mismatch' | 'out_of_range';
  profile: { id: string; version: number } | null; price_list: PriceListRef | null;
  base: { price_list_id: string; price_list_code: string; fallback: boolean; entry_revision: number; unit_price: string; currency: Currency; updated_at: string | null } | null;
  steps: PriceStep[]; discarded: { rule_id: string; name: string; reason: string }[]; next_breaks: { min_quantity: number; unit_price: string; rule_name: string }[];
  floor_price: string | null; codes: string[]; rounding: string; fingerprint: string;
};
export type DraftSuggestion = { unit_price: string | null; fingerprint: string; explanation: PriceExplanation };
export type DraftPricing = { engine: string; configured: boolean; price_list: PriceListRef | null; stale: number; fillable: number };
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
  currency: Currency; terms: string; terms_origin: 'saved' | 'previous' | 'none'; updated_at: string | null; updated_by: { name: string } | null;
  permissions: { can_publish: boolean; publish_requires: MembershipPermission }; pricing: DraftPricing; lines: QuoteDraftLine[]; order_exceptions: DraftException[];
  summary: { blocking: number; to_confirm: number; info: number; total: string; line_count: number };
  // Prices the save that returned this draft recalculated from the supplier's list; empty on reads.
  repriced: RepricedLine[];
};
export type QuoteDraftReprice = { save_id: string; expected_draft_version: number; scope: RepriceScope; order_line_ids?: string[] };
export type QuoteDraftEnvelope = { editable: boolean; draft: QuoteDraft | null };
export type QuoteDraftLineChange = { order_line_id: string; quantity?: number | null; unit_price?: string | null; note?: string;
  acknowledge?: DraftAcknowledgement[]; revoke?: string[] };
export type QuoteDraftSave = { save_id: string; expected_draft_version: number; currency?: 'USD' | 'PAB'; terms?: string; lines?: QuoteDraftLineChange[] };
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
