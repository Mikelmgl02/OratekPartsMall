export function supplierRequestsHref(accountId: string) {
  return `/?${new URLSearchParams({ vista: 'proveedor', seccion: 'solicitudes', cuenta: accountId })}`;
}

export function supplierOrderHref(accountId: string, orderId: string) {
  return `/proveedor/${encodeURIComponent(accountId)}/ordenes/${encodeURIComponent(orderId)}`;
}
