'use client';
import { useEffect, useRef, useId, type DialogHTMLAttributes } from 'react';
import { X } from 'lucide-react';
type DragEvents = Pick<DialogHTMLAttributes<HTMLDialogElement>, 'onDragEnter' | 'onDragOver' | 'onDragLeave' | 'onDrop'>;
export default function Modal({ title, children, onClose, wide = false, drawer = false, className = '', dragEvents }: { title: string; children: React.ReactNode; onClose: () => void; wide?: boolean; drawer?: boolean; className?: string; dragEvents?: DragEvents }) {
  const ref = useRef<HTMLDialogElement>(null);
  const label = useId();
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog ref={ref} aria-labelledby={label} className={`modal ${wide ? 'wide' : ''} ${drawer ? 'drawer' : ''} ${className}`} {...dragEvents} onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => { if (event.target === ref.current) { const rect = ref.current.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose(); } }}>
    <div className="modal-header"><h2 id={label}>{title}</h2><button className="icon-button" aria-label="Cerrar ventana" onClick={onClose}><X size={20}/></button></div>
    {children}
  </dialog>;
}
