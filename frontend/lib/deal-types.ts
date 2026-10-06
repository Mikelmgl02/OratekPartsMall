import type { ClientRequestDetail, SupplierRequestDetail } from './request-types';
export type DealStatus = 'pending' | 'reviewed' | 'quoted' | 'adjustment' | 'handshaked';
export const dealStatusLabels: Record<DealStatus, string> = {
  pending: 'Enviada · por revisar', reviewed: 'En revisión', quoted: 'Cotizada · por confirmar',
  adjustment: 'Ajuste solicitado', handshaked: 'Acuerdo confirmado',
};
export type Quotation = {
  id: string; revision: number; currency: 'USD' | 'PAB'; terms: string; total: string;
  created_at: string; supplier_confirmed_at: string; client_confirmed_at: string | null;
  lines: { order_line_id: string; codigo: string; sku: string; description: string; quantity: number; unit_price: string; total: string }[];
};
export type DealEvent = { id: number; kind: string; description: string; account_name: string; actor_name: string; created_at: string; quotation_id: string | null };
export type DealMessage = { id: number; message_id: string; body: string; account_id: string; account_name: string; actor_name: string; created_at: string };
export type Deal = (ClientRequestDetail | SupplierRequestDetail) & {
  version: number; client: { id: string; name: string }; supplier: { id: string; name: string };
  quotations: Quotation[]; quotation: Quotation | null; events: DealEvent[]; handshaked_at: string | null;
};
