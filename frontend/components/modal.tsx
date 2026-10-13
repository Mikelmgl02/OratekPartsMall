'use client';
import { useEffect, useRef, useId, type DialogHTMLAttributes } from 'react';
import { X } from 'lucide-react';
import UpdateNotice from './update-notice';
type DragEvents = Pick<DialogHTMLAttributes<HTMLDialogElement>, 'onDragEnter' | 'onDragOver' | 'onDragLeave' | 'onDrop'>;

// A click on the backdrop closes the dialog, except in its first moments: the second click of the double-click that opened it lands there.
export default function Modal({ title, children, onClose, wide = false, drawer = false, className = '', dragEvents }: { title: string; children: React.ReactNode; onClose: () => void; wide?: boolean; drawer?: boolean; className?: string; dragEvents?: DragEvents }) {
  const ref = useRef<HTMLDialogElement>(null);
  const label = useId();
  const opened = useRef(0);
  // Escape closes an open grid menu or popup editor first, never the dialog under it: the grid removes its popup during the keydown, so
  // whether one was open is noted on the way in (capture), before the dialog's cancel.
  const popupEscape = useRef(false);
  // A press that started inside (a column dragged out of a grid, text selected past the edge) never closes it where it ends.
  const pressedOutside = useRef(false);
  const outside = (event: React.MouseEvent<HTMLDialogElement>) => {
    if (event.target !== ref.current) return false;
    const rect = ref.current.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
  };
  const closing = useRef(onClose); closing.current = onClose;
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    let mounted = true;
    opened.current = performance.now(); dialog.showModal();
    // The browser can close a dialog on Escape without a cancel event the page may refuse (repeated close requests): ask the owner, and
    // open it again when the owner keeps it (a batch in progress), so what shows always matches what React renders.
    const closed = () => {
      if (!mounted || dialog.open) return;   // a close queued by an earlier mount (StrictMode runs effects twice): this one is open
      closing.current();
      requestAnimationFrame(() => { if (mounted && !dialog.open) dialog.showModal(); });
    };
    dialog.addEventListener('close', closed);
    return () => { mounted = false; dialog.removeEventListener('close', closed); dialog.close(); };
  }, []);
  return <dialog ref={ref} aria-labelledby={label} className={`modal ${wide ? 'wide' : ''} ${drawer ? 'drawer' : ''} ${className}`} {...dragEvents}
    onKeyDownCapture={event => {
      const popups = event.key === 'Escape' ? [...ref.current?.querySelectorAll('.ag-popup') || []] : [];
      popupEscape.current = popups.length > 0;
      // In a dialog over another one (Ampliar in the conversations panel) the grid can miss its own Escape. When it had its chance and a
      // lone menu is still open, a press outside it, which the grid always honours, closes it; stacked popups (a submenu, a filter's list)
      // stay the grid's to close.
      if (popups.length === 1) setTimeout(() => {
        if (popups[0].isConnected) ref.current?.querySelector('.modal-header')?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      }, 0);
    }}
    onCancel={event => { if (event.target !== event.currentTarget) return; event.preventDefault(); if (popupEscape.current) { popupEscape.current = false; return; } onClose(); }} onMouseDown={event => { pressedOutside.current = outside(event); }}
    onClick={event => { if (pressedOutside.current && outside(event) && performance.now() - opened.current > 400) onClose(); pressedOutside.current = false; }}>
    <div className="modal-header"><h2 id={label}>{title}</h2><button className="icon-button" aria-label="Cerrar ventana" onClick={onClose}><X size={20}/></button></div>
    {children}
    <UpdateNotice/>
  </dialog>;
}
