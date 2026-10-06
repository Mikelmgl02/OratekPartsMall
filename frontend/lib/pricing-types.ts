// Supplier-only pricing and draft payloads. Never rendered on client screens.
import type { MembershipPermission } from './types';

export type PriceSource = 'engine' | 'previous' | 'manual' | 'none';
export type QuantitySource = 'requested' | 'available' | 'previous' | 'manual' | 'assistant';
export type DraftException = { code: string; severity: 'block' | 'confirm' | 'info'; message: string; context: string; acknowledged: boolean };
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
export type QuoteDraftLineChange = { order_line_id: string; quantity?: number | null; unit_price?: string | null; note?: string };
export type QuoteDraftSave = { save_id: string; expected_draft_version: number; currency?: 'USD' | 'PAB'; terms?: string; lines?: QuoteDraftLineChange[] };
