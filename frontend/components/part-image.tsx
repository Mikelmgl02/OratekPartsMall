'use client';
import { useState } from 'react';
import type { Part, CatalogImage } from '@/lib/types';
import PartArt from './part-art';

export function CatalogPhoto({ image, alt, thumbnail = false }: { image?: CatalogImage; alt: string; thumbnail?: boolean }) {
  const [failed, setFailed] = useState('');
  const src = thumbnail ? image?.thumbnail_url || image?.url : image?.url;
  if (!src || failed === src) return <PartArt name={alt}/>;
  return <img className="catalog-photo" src={src} alt={image?.alt_text || alt} width={image?.width} height={image?.height} loading="lazy" decoding="async" onError={() => setFailed(src)}/>;
}

export default function PartGallery({ part, preview }: { part: Part; preview: boolean }) {
  const [selected, setSelected] = useState(0);
  const images = part.images || [];
  const image = images[selected] || images[0];
  return <div className="part-gallery">
    <div className="detail-art"><span className="tiny-badge">{preview ? 'Repuesto de ejemplo' : 'SKU del catálogo'}</span><CatalogPhoto image={image} alt={part.name || part.sku}/>{images.length > 1 && <span className="gallery-counter">{selected + 1} / {images.length}</span>}</div>
    {images.length > 1 && <div className="gallery-thumbnails" aria-label="Imágenes del repuesto">{images.map((photo, index) => <button type="button" key={photo.id} aria-label={`Ver imagen ${index + 1} de ${part.sku}`} aria-pressed={image?.id === photo.id} onClick={() => setSelected(index)}><CatalogPhoto image={photo} alt={`${part.sku} · ${index + 1}`} thumbnail/></button>)}</div>}
  </div>;
}
