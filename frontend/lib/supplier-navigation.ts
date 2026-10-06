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
// Precios sub-sections; a client's private profile opens inside Clientes.
export type PricingTab = 'lists' | 'clients' | 'rules' | 'simulator' | 'history' | 'settings';
export const pricingTabs: Record<PricingTab, string> = { lists: 'listas', clients: 'clientes', rules: 'reglas', simulator: 'simulador', history: 'historial', settings: 'configuracion' };
export function supplierPricingHref(accountId: string, tab: PricingTab = 'lists', clientId?: string) {
  return `/?${new URLSearchParams({ vista: 'proveedor', seccion: supplierSections.pricing, cuenta: accountId, pestana: pricingTabs[tab], ...(clientId ? { cliente: clientId } : {}) })}`;
}
