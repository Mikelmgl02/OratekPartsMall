'use client';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';

// A cesta quantity typed in place (the cesta grid, basket-grid.tsx, and a card's En cesta summary, part-units-summary.tsx).
// A typed quantity is saved once typing pauses (or the field is left, or Enter), not on every key. While there is unsaved typing, a quantity
// coming back from the server does not overwrite it; once saved, the server's (WhatsApp, another device) shows. Escape drops unsaved typing
// and, when this field already saved a quantity since it was entered, puts back the one it had then.
const SAVE_AFTER_MS = 400;
const clamp = (value: number) => Math.max(1, Math.min(9999, Math.trunc(value)));
export default function QuantityInput({ name, quantity, onChange, disabled = false, className = 'basket-grid-number' }: { name: string; quantity: number; onChange: (quantity: number) => void; disabled?: boolean; className?: string }) {
  const [value, setValue] = useState(String(quantity));
  // The typing is kept here too: on unmount (leaving the page) React has already let go of the input when the last save runs.
  const draft = useRef(String(quantity)), dirty = useRef(false), sent = useRef(false), entered = useRef(quantity);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef({ quantity, onChange });
  useLayoutEffect(() => { latest.current = { quantity, onChange }; });
  useEffect(() => { if (!dirty.current) setValue(String(quantity)); }, [quantity]);
  function save() {
    clearTimeout(timer.current);
    if (!dirty.current) return;
    dirty.current = false;
    const parsed = Number(draft.current);  // an empty field counts as 1, the least that can be asked for
    const next = Number.isFinite(parsed) ? clamp(parsed) : latest.current.quantity;
    setValue(String(next));
    if (next !== latest.current.quantity) { sent.current = true; latest.current.onChange(next); }
  }
  useEffect(() => () => save(), []);  // eslint-disable-line react-hooks/exhaustive-deps -- leaving the page keeps what was typed
  return <input className={className} type="number" min={1} max={9999} step={1} inputMode="numeric" autoComplete="off" disabled={disabled}
    aria-label={`Cantidad de ${name}`} value={value} onFocus={event => { entered.current = latest.current.quantity; sent.current = false; event.currentTarget.select(); }}
    onChange={event => {
      draft.current = event.target.value; setValue(event.target.value); dirty.current = true;
      clearTimeout(timer.current);
      const next = Number(event.target.value);
      if (event.target.value && Number.isInteger(next) && next >= 1 && next <= 9999) timer.current = setTimeout(save, SAVE_AFTER_MS);
    }}
    onBlur={() => { if (dirty.current) save(); else setValue(String(latest.current.quantity)); }}
    onKeyDown={event => {
      if (event.key === 'Enter') event.currentTarget.blur();
      if (event.key === 'Escape') {
        const back = sent.current && entered.current !== latest.current.quantity;
        // Only the typing is undone: a dialog around the field (a card's En cesta summary) stays open; a second Escape closes it.
        if (dirty.current || back) event.preventDefault();
        clearTimeout(timer.current); dirty.current = false;
        setValue(String(back ? entered.current : latest.current.quantity));
        if (back) latest.current.onChange(entered.current);
        sent.current = false;
        event.currentTarget.blur();
      }
    }}/>;
}
