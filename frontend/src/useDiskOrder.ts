import { RefObject, useCallback, useEffect, useMemo, useRef, useState } from 'react';

export default function useDiskOrder<T extends { mountpoint: string }>(
  disks: T[], savedOrder: string[], region: RefObject<HTMLElement | null>,
  onSaved: (order: string[]) => void, view: string,
) {
  const [draft, setDraft] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const editing = draft !== null;
  const complete = (order: string[]) => [...new Set([...order, ...disks.map(disk => disk.mountpoint)])];
  const ordered = useMemo(() => {
    const ranks = new Map((draft ?? savedOrder).map((mountpoint, index) => [mountpoint, index]));
    return [...disks].sort((a, b) => (ranks.get(a.mountpoint) ?? ranks.size) - (ranks.get(b.mountpoint) ?? ranks.size));
  }, [disks, draft, savedOrder]);

  const cancel = useCallback(() => {
    if (pending.current) return;
    setDraft(null); setError('');
  }, []);

  useEffect(() => { cancel(); }, [view, cancel]);
  useEffect(() => {
    if (!editing) return;
    const outside = (event: PointerEvent) => {
      if (!region.current?.contains(event.target as Node)) cancel();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); cancel(); }
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', escape, true);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', escape, true);
    };
  }, [editing, cancel, region]);

  function move(source: string, target: string, after = false) {
    if (pending.current || source === target) return;
    setDraft(previous => {
      if (!previous) return previous;
      const next = complete(previous).filter(mountpoint => mountpoint !== source);
      const index = next.indexOf(target);
      if (index < 0) return previous;
      next.splice(index + Number(after), 0, source);
      return next;
    });
  }

  async function toggle() {
    if (pending.current) return;
    setError('');
    if (!draft) { setDraft(complete(savedOrder)); return; }
    pending.current = true; setSaving(true);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/api/system/disks/order', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mountpoints: complete(draft) }), signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result: { mountpoints: string[] } = await response.json();
      onSaved(result.mountpoints); setDraft(null);
    } catch {
      setError('保存顺序失败，请重试；点击磁盘块外可放弃本次调整。');
    } finally {
      window.clearTimeout(timeout);
      pending.current = false; setSaving(false);
    }
  }

  return { disks: ordered, editing, saving, error, move, toggle };
}
