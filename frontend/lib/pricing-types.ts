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
export type QuoteDraftLine = {
  order_line_id: string; codigo: string; brand: string; description: string; requested: number; stock: DraftStock | null;
  quantity: number | null; quantity_source: QuantitySource; unit_price: string | null; price_source: PriceSource;
  suggestion: { unit_price: string; fingerprint: string; explanation: Record<string, unknown> } | null; note: string; exceptions: DraftException[];
};
export type QuoteDraft = {
  persisted: boolean; draft_version: number; status: 'editing' | 'review_requested'; base_quotation_id: string | null;
  currency: 'USD' | 'PAB'; terms: string; terms_origin: 'saved' | 'previous' | 'none'; updated_at: string | null; updated_by: { name: string } | null;
  permissions: { can_publish: boolean; publish_requires: MembershipPermission }; lines: QuoteDraftLine[]; order_exceptions: DraftException[];
  summary: { blocking: number; to_confirm: number; info: number; total: string; line_count: number };
};
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
