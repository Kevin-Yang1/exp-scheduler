import React, { useEffect, useId, useMemo, useRef, useState } from 'react';

export interface ChartPoint {
  time: number;
  values: Record<string, number | null>;
  minimum?: Record<string, number | null>;
  maximum?: Record<string, number | null>;
}

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
}

export const CHART_COLORS = ['var(--chart-blue)', 'var(--chart-green)', 'var(--chart-purple)', 'var(--chart-amber)', 'var(--chart-cyan)', 'var(--chart-rose)'];

export function formatBytes(value: number | null | undefined, rate = false) {
  if (value == null || !Number.isFinite(value)) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  const index = value > 0 ? Math.min(5, Math.max(0, Math.floor(Math.log(value) / Math.log(1024)))) : 0;
  return `${(value / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}${rate ? '/s' : ''}`;
}

export function formatPercent(value: number | null | undefined) {
  return value == null ? '—' : `${value.toFixed(1)}%`;
}

function niceCeiling(value: number) {
  if (value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  return ([1, 2, 5, 10].find(step => step * power >= value) ?? 10) * power;
}

function clock(time: number) {
  return new Date(time).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export default function MonitorChart({
  title, subtitle, points, series, period, formatValue, percent = false, percentLimit = 100, cursorTime, onCursorTime, range, stepSeconds = 2, onZoom,
}: {
  title: string;
  subtitle?: string;
  points: ChartPoint[];
  series: ChartSeries[];
  period: number;
  formatValue: (value: number | null) => string;
  percent?: boolean;
  percentLimit?: number;
  cursorTime: number | null;
  onCursorTime: (time: number | null) => void;
  range?: { start: number; end: number };
  stepSeconds?: number;
  onZoom?: (start: number, end: number) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(480);
  const id = useId();
  const [dragStart, setDragStart] = useState<number | null>(null);
  const [dragEnd, setDragEnd] = useState<number | null>(null);
  useEffect(() => {
    const observer = new ResizeObserver(entries => {
      if (entries[0]) setWidth(Math.max(240, Math.floor(entries[0].contentRect.width)));
    });
    if (container.current) observer.observe(container.current);
    return () => observer.disconnect();
  }, []);

  const end = range?.end ?? points.at(-1)?.time ?? Date.now();
  const span = range ? range.end - range.start : Math.min(period, Math.max(10000, end - (points[0]?.time ?? end)));
  const start = end - span;
  const visible = useMemo(() => points.filter(point => point.time >= start && point.time <= end), [points, start, end]);
  const height = 160, left = 60, right = width - 14, top = 12, bottom = height - 28;
  const values = visible.flatMap(point => series.flatMap(item => [point.values[item.key], point.maximum?.[item.key]]).filter((value): value is number => value != null));
  const maximum = Math.max(0, ...values);
  const unit = 1024 ** Math.max(0, Math.floor(Math.log(Math.max(1, maximum)) / Math.log(1024)));
  const ceiling = percent
    ? Math.min(percentLimit, niceCeiling(maximum > 0 ? maximum * 1.08 : 100))
    : maximum > 0 ? niceCeiling(maximum * 1.08 / unit) * unit : 1024;
  const x = (time: number) => left + (time - start) / span * (right - left);
  const y = (value: number) => bottom - Math.max(0, Math.min(ceiling, value)) / ceiling * (bottom - top);
  const selected = cursorTime == null || !visible.length ? null : visible.reduce((best, point) => Math.abs(point.time - cursorTime) < Math.abs(best.time - cursorTime) ? point : best);
  const latest = visible.at(-1);
  const ticks = width < 500 ? 3 : 5;
  const maxGap = Math.max(10000, stepSeconds * 2500);

  function pointerTime(clientX: number) {
    const bounds = container.current?.getBoundingClientRect();
    return start + Math.max(0, Math.min(1, (clientX - (bounds?.left ?? 0) - left) / (right - left))) * span;
  }

  function select(clientX: number) {
    const bounds = container.current?.getBoundingClientRect();
    if (!bounds || !visible.length) return;
    const time = start + Math.max(0, Math.min(1, (clientX - bounds.left - left) / (right - left))) * span;
    onCursorTime(visible.reduce((best, point) => Math.abs(point.time - time) < Math.abs(best.time - time) ? point : best).time);
  }

  function lineSegments(key: string) {
    const segments: ChartPoint[][] = [];
    for (const point of visible) {
      if (point.values[key] == null) {
        if (segments.at(-1)?.length) segments.push([]);
      } else {
        if (!segments.length) segments.push([]);
        const previous = segments.at(-1)!.at(-1);
        if (previous && point.time - previous.time > maxGap) segments.push([]);
        segments.at(-1)!.push(point);
      }
    }
    return segments.filter(segment => segment.length);
  }

  return (
    <section className="rm-chart" aria-labelledby={`${id}-title`}>
      <div className="rm-chart-heading">
        <div><h3 id={`${id}-title`}>{title}</h3>{subtitle && <p>{subtitle}</p>}</div>
        {series.length === 1 && <strong>{formatValue(latest?.values[series[0].key] ?? null)}</strong>}
      </div>
      <div
        ref={container}
        className="rm-chart-plot"
        tabIndex={0}
        role="group"
        aria-label={`${title}趋势图，使用左右方向键查看采样读数`}
        onPointerMove={event => { select(event.clientX); if (dragStart != null) setDragEnd(pointerTime(event.clientX)); }}
        onPointerDown={event => { if (onZoom && event.button === 0) { setDragStart(pointerTime(event.clientX)); setDragEnd(null); event.currentTarget.setPointerCapture(event.pointerId); } }}
        onPointerUp={event => {
          if (dragStart != null && onZoom) {
            const end = pointerTime(event.clientX);
            if (Math.abs(end - dragStart) >= Math.max(2000, span * .02)) onZoom(Math.min(dragStart, end), Math.max(dragStart, end));
          }
          setDragStart(null); setDragEnd(null);
        }}
        onPointerCancel={() => { setDragStart(null); setDragEnd(null); }}
        onPointerLeave={() => onCursorTime(null)}
        onFocus={() => { if (latest) onCursorTime(latest.time); }}
        onBlur={() => onCursorTime(null)}
        onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return;
          event.preventDefault();
          if (event.key === 'Escape') { onCursorTime(null); return; }
          const index = Math.max(0, visible.findIndex(point => point.time === selected?.time));
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? visible.length - 1 : Math.max(0, Math.min(visible.length - 1, index + (event.key === 'ArrowLeft' ? -1 : 1)));
          if (visible[next]) onCursorTime(visible[next].time);
        }}
      >
        <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img" aria-label={`${title}，${series.map(item => `${item.label} ${formatValue(latest?.values[item.key] ?? null)}`).join('，')}`}>
          {[0, .5, 1].map(fraction => <g key={fraction}>
            <line x1={left} x2={right} y1={y(ceiling * fraction)} y2={y(ceiling * fraction)} className="rm-grid-line" />
            <text x={left - 10} y={y(ceiling * fraction) + 4} textAnchor="end" className="rm-axis">{formatValue(ceiling * fraction).replace(/\.0(?=\s)/, '')}</text>
          </g>)}
          {Array.from({ length: ticks }, (_, index) => {
            const time = start + span * index / (ticks - 1);
            const label = span >= 86400000 ? new Date(time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : span >= 3600000 ? clock(time).slice(0, 5) : clock(time);
            return <text key={index} x={x(time)} y={height - 5} textAnchor={index === 0 ? 'start' : index === ticks - 1 ? 'end' : 'middle'} className="rm-axis">{label}</text>;
          })}
          {series.map(item => <g key={item.key}>{lineSegments(item.key).map((segment, index) => {
            const path = segment.map((point, index) => `${index ? 'L' : 'M'}${x(point.time).toFixed(2)},${y(point.values[item.key]!).toFixed(2)}`).join(' ');
            const last = segment.at(-1)!;
            const band = [...segment.map((point, index) => `${index ? 'L' : 'M'}${x(point.time)},${y(point.maximum?.[item.key] ?? point.values[item.key]!)}`), ...[...segment].reverse().map(point => `L${x(point.time)},${y(point.minimum?.[item.key] ?? point.values[item.key]!)}`)].join(' ') + ' Z';
            return <g key={index}>
              {series.length === 1 && segment.length > 1 && <path d={`${path} L${x(last.time)},${bottom} L${x(segment[0].time)},${bottom} Z`} fill={item.color} opacity=".07" />}
              {segment.length > 1 && <path d={band} fill={item.color} opacity=".15" />}
              <path d={path} stroke={item.color} strokeWidth="1.75" fill="none" strokeLinejoin="round" />
              <circle cx={x(last.time)} cy={y(last.values[item.key]!)} r="2" fill={item.color} />
            </g>;
          })}</g>)}
          {!values.length && <text x={(left + right) / 2} y={(top + bottom) / 2} textAnchor="middle" className="rm-axis">等待采样数据</text>}
          {dragStart != null && dragEnd != null && <rect x={x(Math.min(dragStart, dragEnd))} y={top} width={Math.abs(x(dragEnd) - x(dragStart))} height={bottom - top} fill="var(--chart-blue)" opacity=".12" />}
          {selected && <g pointerEvents="none">
            <line x1={x(selected.time)} x2={x(selected.time)} y1={top} y2={bottom} className="rm-cursor-line" />
            {series.filter(item => selected.values[item.key] != null).map(item => <circle key={item.key} cx={x(selected.time)} cy={y(selected.values[item.key]!)} r="3.5" fill={item.color} stroke="white" strokeWidth="1.5" />)}
          </g>}
        </svg>
        {selected && <div className="rm-chart-tooltip" role="status" style={{ left: Math.max(0, Math.min(width - 190, x(selected.time) - 95)), top: 8 }}>
          <time>{new Date(selected.time).toLocaleString('zh-CN', { hour12: false })}</time>
          {series.map(item => <React.Fragment key={item.key}><div><span><i style={{ background: item.color }} />{item.label}</span><strong>{formatValue(selected.values[item.key] ?? null)}</strong></div>{selected.maximum?.[item.key] != null && selected.maximum[item.key] !== selected.values[item.key] && <small>区间峰值 {formatValue(selected.maximum[item.key])}</small>}</React.Fragment>)}
        </div>}
      </div>
      <div className="rm-chart-legend">{series.map(item => <span key={item.key}><i style={{ background: item.color }} />{item.label}{series.length > 1 && <b>{formatValue(latest?.values[item.key] ?? null)}</b>}</span>)}</div>
    </section>
  );
}
