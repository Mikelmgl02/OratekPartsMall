export default function CatalogSkeleton() {
  return <div className="catalog-skeleton" aria-hidden="true">
    <div className="part-image skeleton-image"><span className="skeleton-block skeleton-visual"/></div>
    <div className="part-card-body">
      <div className="skeleton-block skeleton-stock"/>
      <div className="skeleton-block skeleton-meta"/>
      <div className="skeleton-block skeleton-title"/>
      <div className="skeleton-block skeleton-description"/>
      <div className="skeleton-classification"><span className="skeleton-block"/><span className="skeleton-block"/></div>
      <div className="skeleton-block skeleton-code"/>
      <div className="part-card-bottom skeleton-footer"><span className="skeleton-block"/><span className="skeleton-block"/></div>
    </div>
  </div>;
}
