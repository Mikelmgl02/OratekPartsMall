export type SupplierSection = 'inventory' | 'pricing' | 'requests';
export const supplierSections: Record<SupplierSection, string> = { inventory: 'inventario', pricing: 'precios', requests: 'solicitudes' };

export function supplierSectionHref(accountId: string, section: SupplierSection) {
  return `/?${new URLSearchParams({ vista: 'proveedor', seccion: supplierSections[section], cuenta: accountId })}`;
}

export function supplierRequestsHref(accountId: string) {
  return supplierSectionHref(accountId, 'requests');
}
export function supplierOrderHref(accountId: string, orderId: string) {
  return `/proveedor/${encodeURIComponent(accountId)}/ordenes/${encodeURIComponent(orderId)}`;
}
