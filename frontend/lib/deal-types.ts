import type { ClientRequestDetail, SupplierRequestDetail } from './request-types';
// In lifecycle order (mall SupplierRequest.STATUS_CHOICES). After the handshake the supplier marks Procesando, En camino (skipped when the
// client picks it up) and Entregado; only then can the client mark Recibido, which closes the order. Nothing completes on its own.
export type DealStatus = 'pending' | 'reviewed' | 'quoted' | 'adjustment' | 'handshaked' | 'processing' | 'in_transit' | 'delivered' | 'received' | 'cancelled';
export const dealStatusLabels: Record<DealStatus, string> = {
  pending: 'Enviada · por revisar', reviewed: 'En revisión', quoted: 'Cotizada · por confirmar', adjustment: 'Ajuste solicitado',
  handshaked: 'Acuerdo confirmado', processing: 'Procesando', in_transit: 'En camino', delivered: 'Entregado · por recibir', received: 'Recibido', cancelled: 'Cancelado',
};
// The same groups as mall.request_models: GOODS_HELD still allows a shortfall or a party's cancellation; ACTIVE_AGREEMENT is confirmed and
// before the supplier's Entregado; OPEN_AGREEMENT is confirmed and not closed (a delivered order waits for the client's Recibido).
export const PRE_HANDSHAKE: DealStatus[] = ['pending', 'reviewed', 'quoted', 'adjustment'];
export const GOODS_HELD: DealStatus[] = ['handshaked', 'processing'];
export const ACTIVE_AGREEMENT: DealStatus[] = ['handshaked', 'processing', 'in_transit'];
export const OPEN_AGREEMENT: DealStatus[] = [...ACTIVE_AGREEMENT, 'delivered'];
export const CLOSED_STATUSES: DealStatus[] = ['received', 'cancelled'];
export const AFTER_HANDSHAKE: DealStatus[] = [...OPEN_AGREEMENT, 'received', 'cancelled'];
// A delivered order, as each side reads it in a list: the supplier waits, the client has something to do.
export const receiptHint = (supplier: boolean) => supplier ? 'Esperando recibido del cliente' : 'Confirma que la recibiste';
// After the handshake: the agreement after the shortfalls the client accepted, the shortfalls, the order's progress and a cancellation.
export type DealAgreementLine = { order_line_id: string; codigo: string; description: string; agreed: number; missing: number; pending_missing: number; final: number; unit_price: string; total: string };
export type DealAgreement = { quotation_id: string; units: number; total: string; lines: DealAgreementLine[] };
export type DealShortfallReason = 'no_stock' | 'damaged' | 'other';
export type DealShortfall = { id: string; status: 'pending' | 'accepted' | 'declined'; reason: DealShortfallReason; reason_label: string; note: string;
  reported_at: string; responded_at: string | null; lines: { order_line_id: string; codigo: string; description: string; agreed: number; missing: number }[] };
export type DealCancelSide = 'client' | 'supplier' | 'admin';
export type DealCancellation = { side: DealCancelSide; reason: string; reason_label: string; note: string; cancelled_at: string };
export const shortfallReasons: [DealShortfallReason, string][] = [['no_stock', 'Sin existencias'], ['damaged', 'Dañado'], ['other', 'Otro motivo']];
// The reasons each party picks from (the server accepts the codes of mall SupplierRequest.CANCEL_REASONS).
export const cancelReasons: Record<'client' | 'supplier', [string, string][]> = {
  client: [['not_needed', 'Ya no lo necesito'], ['found_elsewhere', 'Lo conseguí con otro proveedor'], ['delay', 'Demora en la entrega'], ['shortfall', 'No acepto el faltante'], ['other', 'Otro motivo']],
  supplier: [['no_stock', 'Sin existencias'], ['price_error', 'Error de precio'], ['cannot_deliver', 'No puedo entregar'], ['other', 'Otro motivo']],
};
export const cancelSides: Record<DealCancelSide, string> = { client: 'el cliente', supplier: 'el proveedor', admin: 'MotionPartes' };
export type Quotation = {
  id: string; revision: number; currency: 'USD' | 'PAB'; terms: string; total: string;
  created_at: string; supplier_confirmed_at: string; client_confirmed_at: string | null;
  lines: { order_line_id: string; codigo: string; sku: string; description: string; quantity: number; unit_price: string; total: string }[];
};
export type DealEvent = { id: number; kind: string; description: string; account_name: string; actor_name: string; created_at: string; quotation_id: string | null };
// A file in a deal's chat (mall.deal_attachments): opened through /api/market/accounts/<account>/deals/<deal>/attachments/<id>.
export type DealAttachment = { id: number; kind: 'image' | 'document' | 'audio' | 'video'; name: string; content_type: string; size: number; voice: boolean; failed: boolean; transcript: string; retryable?: boolean };
// order_id / order_reference: the order the message is about (null: about the relationship, untagged).
export type DealMessage = { id: number; message_id: string; body: string; account_id: string; account_name: string; actor_name: string; created_at: string; channel?: 'app' | 'whatsapp'; attachments?: DealAttachment[]; order_id?: string | null; order_reference?: string | null };
export type Deal = (ClientRequestDetail | SupplierRequestDetail) & {
  version: number; client: { id: string; name: string }; supplier: { id: string; name: string };
  quotations: Quotation[]; quotation: Quotation | null; events: DealEvent[]; handshaked_at: string | null;
  agreement?: DealAgreement | null; shortfalls?: DealShortfall[]; delivered_at?: string | null; cancellation?: DealCancellation | null;
  // When the supplier marked Procesando and En camino, and the client Recibido (optional: replayed results and older payloads lack them).
  processing_at?: string | null; in_transit_at?: string | null; received_at?: string | null;
  // stock_reserved: the handshake reserved stock; can_decide (supplier reads only): this member may take the supplier's decisions.
  stock_reserved?: boolean; can_decide?: boolean;
  // Supplier payloads only, after the handshake: units the deal still holds, those its count already discounted, and those a cancellation
  // after dispatch gave back (mall/request_views.py stock_units).
  stock_units?: { held: number; settled: number; released_after_dispatch: number } | null;
  // While the client decides on the sent quotation: its units held until when, or the hold that lapsed (a confirmation checks stock again).
  stock_hold?: { state: 'held' | 'lapsed'; units: number; until: string | null } | null;
};
export type QuotePayload = {
  currency: 'USD' | 'PAB'; terms: string; lines: { order_line_id: string; quantity: number; unit_price: string }[];
  // Binds publishing to the reviewed server draft: the server refuses a quote without it (QUOTES_REQUIRE_DRAFT).
  draft_version: number;
};
