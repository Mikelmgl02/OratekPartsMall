import type { DealStatus } from './deal-types';
export type SupplierRequestSummary = {
  id: string; reference: string; client: { id: string; name: string };
  status: DealStatus; version?: number; handshaked_at?: string | null; updated_at?: string; created_at: string; reviewed_at: string | null;
  line_count: number; unit_count: number; quoted_unit_count?: number | null; quotation_total?: string | null; quotation_currency?: string | null; quotation_revision?: number | null;
};
export type SupplierRequestLine = {
  id: string; supplier_item_id: string; supplier_invent_id: string; part_id: string;
  sku: string; name: string; codigo: string; brand: string; description: string; quantity: number;
  stock: { reported_quantity: number; reserved_quantity: number; available_quantity: number; shortfall: number; updated_at: string } | null;
};
export type SupplierRequestDetail = SupplierRequestSummary & { notes: string; lines: SupplierRequestLine[] };
export type RequestSubmissionResult = {
  submission_id: string; created_at: string;
  requests: { id: string; reference: string; supplier: { id: string; name: string }; line_count: number; unit_count: number; appended?: boolean; added_unit_count?: number }[];
};
export type ClientRequestSummary = Omit<SupplierRequestSummary, 'client'> & {
  supplier: { id: string; name: string }; submission_id: string;
};
export type ClientRequestDetail = ClientRequestSummary & {
  notes: string; lines: Omit<SupplierRequestLine, 'supplier_item_id' | 'supplier_invent_id' | 'stock'>[];
};
export type RequestedQuantities = { sent_quantity: number; pending_quantity: number; reviewed_quantity: number; quoted_quantity?: number; adjustment_quantity?: number; handshaked_quantity?: number };
export type ClientPartRequestState = {
  part_id: string; totals: RequestedQuantities;
  items: (RequestedQuantities & {supplier_item_id: string; supplier_id: string; codigo: string; brand: string})[];
};
