import type { DealStatus } from './deal-types';
export type SupplierRequestSummary = {
  id: string; reference: string; client: { id: string; name: string };
  status: DealStatus; version?: number; handshaked_at?: string | null; updated_at?: string; created_at: string; reviewed_at: string | null;
  line_count: number; unit_count: number; quoted_unit_count?: number | null; quotation_total?: string | null; quotation_currency?: string | null; quotation_revision?: number | null;
  // After the handshake: a shortfall waiting for the client, the order's progress (Procesando, En camino, Entregado, Recibido) and who cancelled.
  shortfall_pending?: boolean; processing_at?: string | null; in_transit_at?: string | null; delivered_at?: string | null; received_at?: string | null;
  cancelled_at?: string | null; cancelled_side?: 'client' | 'supplier' | 'admin' | null;
  agreed_unit_count?: number | null;
};
// The lists' status filter (mall request_views.STATUS_GROUPS): a status, or agreements (every confirmed order), active_agreements
// (confirmed, before the supplier's Entregado) or open_agreements (confirmed and not received or cancelled); '' is every order.
export type RequestStatusFilter = DealStatus | 'agreements' | 'active_agreements' | 'open_agreements' | '';
// Supplier list rows only: private badges that never reach the client's payloads.
export type SupplierRequestListItem = SupplierRequestSummary & {
  draft_state?: 'editing' | 'review_requested' | null; availability_alert?: 'blocked' | 'accepted_with_shortfall' | null;
};
export type SupplierRequestLine = {
  id: string; supplier_item_id: string; supplier_invent_id: string; part_id: string;
  sku: string; name: string; codigo: string; brand: string; description: string; quantity: number;
  // The part's catalog grupo and subgrupo ('' when unknown; optional: older payloads lack them). The document orders and groups by them.
  grupo?: string; subgrupo?: string;
  // held_for_deal: units this confirmed deal still holds (not a shortfall of its own lines).
  stock: { reported_quantity: number; reserved_quantity: number; available_quantity: number; shortfall: number; held_for_deal?: number; updated_at: string } | null;
};
export type SupplierRequestDetail = SupplierRequestSummary & { notes: string; lines: SupplierRequestLine[] };
export type RequestSubmissionResult = {
  submission_id: string; created_at: string;
  requests: { id: string; reference: string; supplier: { id: string; name: string }; line_count: number; unit_count: number; appended?: boolean; added_unit_count?: number }[];
};
// GET accounts/<client>/sent-requests/summary: the client's orders by stage, for the cards over its list. Amounts come per currency; in_progress
// and to_receive are what the confirmed orders come to after the shortfalls the client accepted; oldest is when the oldest order of each
// stage that waits on the supplier was sent.
export type MoneyTotal = { currency: string; total: string };
export type SentRequestStages = { total: number; statuses: Record<DealStatus, number>; oldest: Partial<Record<'pending' | 'reviewed' | 'adjustment', string>>;
  to_confirm: MoneyTotal[]; in_progress: MoneyTotal[]; to_receive: MoneyTotal[]; shortfall_pending: number; month_start: string;
  sent_this_month: number; received_this_month: number; updated_at: string };
export type ClientRequestSummary = Omit<SupplierRequestSummary, 'client'> & {
  supplier: { id: string; name: string }; submission_id: string;
};
export type ClientRequestDetail = ClientRequestSummary & {
  notes: string; lines: Omit<SupplierRequestLine, 'supplier_item_id' | 'supplier_invent_id' | 'stock'>[];
};
export type RequestedQuantities = { sent_quantity: number; pending_quantity: number; reviewed_quantity: number; quoted_quantity?: number; adjustment_quantity?: number; handshaked_quantity?: number; delivered_quantity?: number };
export type ClientPartRequestState = {
  part_id: string; totals: RequestedQuantities;
  items: (RequestedQuantities & {supplier_item_id: string; supplier_id: string; codigo: string; brand: string})[];
};
