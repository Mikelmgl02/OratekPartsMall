export default function SupplierOrderSkeleton() {
  return <div className="supplier-order-loading" role="status"><span className="sr-only">Cargando orden…</span><div aria-hidden="true"><div className="skeleton-block"/><div className="skeleton-block"/><div className="skeleton-block"/></div></div>;
}
