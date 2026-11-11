import React, { useEffect, useState } from 'react';
import { ArrowLeft, ArrowRight, Radio } from 'lucide-react';
import { ChartPoint } from './MonitorChart';

export type TimeWindow = { start: number; end: number };
export const PERIODS = [
  [300000, '最近 5 分钟'], [900000, '最近 15 分钟'], [3600000, '最近 1 小时'],
  [21600000, '最近 6 小时'], [86400000, '最近 24 小时'], [604800000, '最近 7 天'],
] as const;

interface HistoryResponse {
  points: ChartPoint[];
  step_seconds: number;
  coverage_start: number | null;
  coverage_end: number | null;
  warning: string | null;
}

export function useMonitorHistory(period: number, window: TimeWindow | null, paused: boolean, active: boolean, refresh: number) {
  const [data, setData] = useState<HistoryResponse>({ points: [], step_seconds: 2, coverage_start: null, coverage_end: null, warning: null });
  const [error, setError] = useState('');
  useEffect(() => {
    if (!active || paused) return;
    let disposed = false, timer: number;
    let controller: AbortController;
    async function load() {
      if (document.hidden) { timer = globalThis.window.setTimeout(load, 6000); return; }
      const end = window?.end ?? Date.now(), start = window?.start ?? end - period;
      controller = new AbortController();
      const timeout = globalThis.window.setTimeout(() => controller.abort(), 8000);
      try {
        const response = await fetch(`/api/system/history?start=${start / 1000}&end=${end / 1000}&max_points=900`, { signal: controller.signal });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || '历史查询失败');
        if (!disposed) { setData(result); setError(result.warning ? '历史采样异常，正在重试' : ''); }
      } catch (error) {
        if (!disposed) setError(error instanceof Error ? error.message : '历史查询失败');
      } finally {
        globalThis.window.clearTimeout(timeout);
        if (!disposed && !window) timer = globalThis.window.setTimeout(load, 6000);
      }
    }
    void load();
    return () => { disposed = true; controller?.abort(); globalThis.window.clearTimeout(timer); };
  }, [period, window, paused, active, refresh]);
  return { ...data, error };
}

function localInput(time: number) {
  const date = new Date(time);
  return new Date(time - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
}

export default function HistoryTimeline({ period, range, onRange, onPeriod, coverageStart, step, customOpen, onCustomOpen }: {
  period: number; range: TimeWindow | null; onRange: (range: TimeWindow | null) => void;
  onPeriod: (period: number) => void; coverageStart: number | null; step: number;
  customOpen: boolean; onCustomOpen: (open: boolean) => void;
}) {
  const [from, setFrom] = useState(''), [to, setTo] = useState(''), [error, setError] = useState('');
  const end = range?.end ?? Date.now(), start = range?.start ?? end - period;
  useEffect(() => {
    if (customOpen) { setFrom(localInput(start)); setTo(localInput(end)); setError(''); }
  }, [customOpen]);
  function move(direction: number) {
    const nextEnd = Math.min(Date.now(), end + direction * period * .8);
    const nextStart = nextEnd - period;
    if (nextStart < Date.now() - 604800000) return;
    onRange({ start: nextStart, end: nextEnd });
  }
  return <div className="rm-timeline">
    <div className="rm-timeline-main">
      <div className="rm-time-navigation">
        <button type="button" aria-label="查看更早的监控" disabled={start - period * .8 < Date.now() - 604800000} onClick={() => move(-1)}><ArrowLeft size={13} /></button>
        <span>{new Date(start).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} — {new Date(end).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>
        <button type="button" aria-label="查看更晚的监控" disabled={!range || end >= Date.now() - 2000} onClick={() => move(1)}><ArrowRight size={13} /></button>
        {range ? <button type="button" onClick={() => onRange(null)}><Radio size={13} />回到实时</button> : <span className="rm-live-label"><i />实时</span>}
      </div>
      <span className="rm-hint">{coverageStart ? `历史始于 ${new Date(coverageStart).toLocaleString('zh-CN')}` : '正在建立服务端历史'} · {step} 秒 / 点 · 拖动图表框选放大</span>
    </div>
    {customOpen && <form className="rm-custom-range" onSubmit={event => {
      event.preventDefault();
      const start = new Date(from).getTime(), end = new Date(to).getTime(), now = Date.now();
      if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < 2000 || end > now + 1000 || start < now - 604800000) {
        setError('请选择最近 7 天内的有效起止时间，至少相隔 2 秒'); return;
      }
      onPeriod(end - start); onRange({ start, end }); onCustomOpen(false);
    }}>
      <label>开始<input type="datetime-local" step="1" value={from} onChange={event => setFrom(event.target.value)} required /></label>
      <label>结束<input type="datetime-local" step="1" value={to} onChange={event => setTo(event.target.value)} required /></label>
      <button type="submit">应用范围</button><button type="button" onClick={() => onCustomOpen(false)}>收起</button>
      {error && <span className="rm-danger" role="alert">{error}</span>}
    </form>}
  </div>;
}
