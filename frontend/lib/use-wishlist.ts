'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { request, WishlistState } from './types';

type Snapshot = { owner: number; ids: Set<string> };

export function useWishlist(userId: number | undefined, enabled: boolean) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const current = useRef<Snapshot | undefined>(undefined);
  const generation = useRef(0);
  const readVersion = useRef(0);
  const mutations = useRef(new Set<string>());
  const ready = enabled && snapshot?.owner === userId;
  const ids = ready && snapshot ? snapshot.ids : new Set<string>();

  const refresh = useCallback(async () => {
    if (!enabled || userId === undefined) return;
    const scope = generation.current, version = ++readVersion.current;
    try {
      const result = await request<WishlistState>('/api/market/wishlist/state');
      if (scope !== generation.current || version !== readVersion.current) return;
      const next = { owner: userId, ids: new Set(result.part_ids) };
      current.current = next; setSnapshot(next); setError('');
      return next;
    } catch (cause) {
      if (scope === generation.current && version === readVersion.current) setError(cause instanceof Error ? cause.message : 'No se pudieron cargar tus favoritos.');
      throw cause;
    }
  }, [enabled, userId]);

  useEffect(() => {
    generation.current += 1; current.current = undefined; mutations.current.clear();
    setSnapshot(undefined); setPending(new Set()); setError('');
    if (!enabled) return;
    const reload = () => {
      if (document.visibilityState === 'visible' && !mutations.current.size) refresh().then(() => setRevision(value => value + 1)).catch(() => {});
    };
    refresh().catch(() => {});
    window.addEventListener('focus', reload);
    document.addEventListener('visibilitychange', reload);
    return () => {
      generation.current += 1;
      window.removeEventListener('focus', reload);
      document.removeEventListener('visibilitychange', reload);
    };
  }, [enabled, userId, refresh]);

  async function toggle(partId: string) {
    if (!enabled || userId === undefined || mutations.current.has(partId)) return;
    const scope = generation.current;
    mutations.current.add(partId); setPending(new Set(mutations.current));
    try {
      const before = current.current?.owner === userId ? current.current : await refresh();
      if (scope !== generation.current) return;
      if (!before) throw new Error('Actualiza tus favoritos y vuelve a intentarlo.');
      // Invalidate older reads so a delayed response cannot undo a successful save.
      readVersion.current += 1;
      const saved = before.ids.has(partId);
      const result = await request<{ part_id: string } | undefined>(`/api/market/wishlist/${partId}`, { method: saved ? 'DELETE' : 'PUT' });
      if (scope !== generation.current) return;
      const next = { owner: userId, ids: new Set(current.current?.ids || before.ids) };
      if (saved) next.ids.delete(partId);
      else { next.ids.delete(partId); next.ids.add(result?.part_id || partId); }
      current.current = next; setSnapshot(next); setError(''); setRevision(value => value + 1);
      return !saved;
    } finally {
      if (scope === generation.current) { mutations.current.delete(partId); setPending(new Set(mutations.current)); }
    }
  }

  return { ids, count: ids.size, ready, pending, error, revision, refresh, toggle };
}
