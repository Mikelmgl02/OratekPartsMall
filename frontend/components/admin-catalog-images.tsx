'use client';
import { DragEvent, FormEvent, useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Check, ImagePlus, LoaderCircle, Star, Trash2 } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';
import { CatalogImage, ManagedPart, request } from '@/lib/types';
import { CatalogPhoto } from './part-image';

export default function CatalogImages({ part, onClose, onChanged }: { part: ManagedPart; onClose: () => void; onChanged: () => void }) {
  const base = `/api/management/catalog/${part.id}/images`;
  const [images, setImages] = useState<CatalogImage[]>([]);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [removing, setRemoving] = useState('');
  const [revision, setRevision] = useState(0);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const lock = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    setReady(false); setError('');
    request<CatalogImage[]>(base, { signal: controller.signal }).then(values => { if (!controller.signal.aborted) { setImages(values); setReady(true); } }).catch(caught => { if (!controller.signal.aborted) setError(caught.message); });
    return () => controller.abort();
  }, [base, revision]);
  async function upload(files: File[]) {
    if (lock.current || !ready || !files.length) return;
    lock.current = true; setBusy(true); setError(''); setNotice('');
    const failures: string[] = []; let uploaded = 0;
    try {
      for (const [index, file] of files.entries()) {
        setProgress(`Subiendo ${index + 1} de ${files.length}…`);
        if (file.type ? !['image/jpeg', 'image/png', 'image/webp'].includes(file.type) : !/\.(jpe?g|png|webp)$/i.test(file.name)) { failures.push(`${file.name}: usa una imagen JPEG, PNG o WebP.`); continue; }
        if (file.size > 10 * 1024 * 1024) { failures.push(`${file.name}: supera los 10 MB.`); continue; }
        const form = new FormData(); form.set('file', file);
        try {
          const image = await request<CatalogImage>(base, { method: 'POST', body: form });
          setImages(values => [...values, image]); uploaded++;
        } catch (caught) { failures.push(`${file.name}: ${caught instanceof Error ? caught.message : 'No se pudo subir.'}`); }
      }
      if (uploaded) { onChanged(); setNotice(`${uploaded} ${uploaded === 1 ? 'imagen agregada' : 'imágenes agregadas'}.`); }
      if (failures.length) setError(failures.join('\n'));
    } finally { setBusy(false); setProgress(''); lock.current = false; }
  }
  function dragEnter(event: DragEvent<HTMLDialogElement>) {
    if (!event.dataTransfer.types.includes('Files')) return;
    event.preventDefault();
    dragDepth.current++;
    if (ready && !lock.current) setDragging(true);
  }
  function dragOver(event: DragEvent<HTMLDialogElement>) {
    // Prevent the browser from opening dropped files or links over the dialog.
    event.preventDefault();
    event.dataTransfer.dropEffect = event.dataTransfer.types.includes('Files') && ready && !lock.current ? 'copy' : 'none';
  }
  function dragLeave() {
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (!dragDepth.current) setDragging(false);
  }
  function drop(event: DragEvent<HTMLDialogElement>) {
    event.preventDefault(); event.stopPropagation();
    dragDepth.current = 0; setDragging(false);
    void upload(Array.from(event.dataTransfer.files));
  }
  async function change(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(''); setNotice('');
    try { await action(); onChanged(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'No se pudo guardar el cambio.'); }
    finally { setBusy(false); lock.current = false; }
  }
  function move(id: string, to: number) {
    const ordered = [...images]; const from = ordered.findIndex(image => image.id === id);
    ordered.splice(to, 0, ordered.splice(from, 1)[0]);
    void change(async () => { setImages(await request<CatalogImage[]>(`${base}/order`, { method: 'POST', body: JSON.stringify({ image_ids: ordered.map(image => image.id) }) })); });
  }
  async function caption(event: FormEvent<HTMLFormElement>, image: CatalogImage) {
    event.preventDefault(); const alt_text = new FormData(event.currentTarget).get('alt_text');
    await change(async () => {
      const updated = await request<CatalogImage>(`${base}/${image.id}`, { method: 'PATCH', body: JSON.stringify({ alt_text }) });
      setImages(values => values.map(value => value.id === updated.id ? updated : value));
      setNotice('Descripción de imagen guardada.');
    });
  }
  return <Modal title={`Imágenes · ${part.sku}`} wide className={dragging ? 'catalog-media-dragging' : ''} dragEvents={{ onDragEnter: dragEnter, onDragOver: dragOver, onDragLeave: dragLeave, onDrop: drop }} onClose={() => { if (!lock.current) onClose(); }}>
    <div className="catalog-media-editor" aria-busy={busy}>
      <p>La primera imagen es la portada del catálogo. Agrega diferentes vistas del mismo repuesto y elige su orden.</p>
      <label className={`catalog-media-upload ${busy || !ready ? 'disabled' : ''}`}><ImagePlus size={25}/><strong>{dragging ? 'Suelta las imágenes para subirlas' : 'Arrastra tus imágenes aquí'}</strong><span>O suéltalas en cualquier parte de esta ventana.</span><span>JPEG, PNG o WebP · Hasta 10 MB por imagen</span><span className="button soft small">Seleccionar archivos</span><input className="sr-only" aria-label="Agregar imágenes al SKU" type="file" accept="image/jpeg,image/png,image/webp" multiple disabled={busy || !ready} onChange={event => { const files = Array.from(event.target.files || []); event.target.value = ''; void upload(files); }}/></label>
      {busy && <div className="notice" role="status"><LoaderCircle size={16} className="spin"/>{progress || 'Guardando cambios…'}</div>}
      {notice && <div className="notice success" role="status">{notice}</div>}
      {error && <div className="notice error catalog-media-error" role="alert">{error}<button type="button" disabled={busy} onClick={() => setRevision(value => value + 1)}>Actualizar galería</button></div>}
      {!ready && !error && <div className="loading" role="status"><LoaderCircle size={18} className="spin"/>Cargando imágenes…</div>}
      {ready && !images.length && <p className="empty-state compact">Este SKU todavía no tiene imágenes.</p>}
      <div className="catalog-media-grid" inert={!ready || undefined}>{images.map((image, index) => <article key={image.id} aria-label={`Imagen ${index + 1}`}>
        <div className="catalog-media-preview"><CatalogPhoto image={image} alt={part.sku} thumbnail/>{index === 0 && <span className="catalog-media-cover"><Star size={12}/>PORTADA</span>}</div>
        <div className="catalog-media-actions"><button type="button" className="button soft small" disabled={busy || index === 0} onClick={() => move(image.id, 0)}><Star size={13}/>Usar de portada</button><button type="button" className="icon-button" aria-label={`Mover imagen ${index + 1} antes`} disabled={busy || index === 0} onClick={() => move(image.id, index - 1)}><ArrowLeft size={15}/></button><button type="button" className="icon-button" aria-label={`Mover imagen ${index + 1} después`} disabled={busy || index === images.length - 1} onClick={() => move(image.id, index + 1)}><ArrowRight size={15}/></button></div>
        <form className="catalog-media-caption" onSubmit={event => void caption(event, image)}><label>Descripción de imagen<UppercaseInput name="alt_text" defaultValue={image.alt_text} placeholder="EJ.: VISTA FRONTAL" maxLength={250} disabled={busy}/></label><button type="submit" className="icon-button" aria-label={`Guardar descripción de imagen ${index + 1}`} disabled={busy}><Check size={16}/></button></form>
        {removing === image.id ? <div className="catalog-media-remove"><span>¿Eliminar esta imagen?</span><button className="button soft small danger-text" disabled={busy} onClick={() => void change(async () => { await request(`${base}/${image.id}`, { method: 'DELETE' }); setImages(values => values.filter(value => value.id !== image.id)); setRemoving(''); })}>Sí, eliminar</button><button type="button" className="button text small" disabled={busy} onClick={() => setRemoving('')}>Cancelar</button></div> : <button type="button" className="button text small danger-text" disabled={busy} onClick={() => setRemoving(image.id)}><Trash2 size={13}/>Eliminar imagen</button>}
      </article>)}</div>
    </div>
  </Modal>;
}
