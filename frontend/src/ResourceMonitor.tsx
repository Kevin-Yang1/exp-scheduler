import React, { ReactNode, useEffect, useId, useMemo, useState } from 'react';
import { Activity, ArrowDown, ArrowUp, ChevronDown, Cpu, HardDrive, Maximize2, MemoryStick, Minimize2, Pause, Play, RefreshCcw, Terminal, TriangleAlert } from 'lucide-react';
import MonitorChart, { CHART_COLORS, ChartPoint, ChartSeries, formatBytes, formatPercent } from './MonitorChart';
import './ResourceMonitor.css';
import HistoryTimeline, { PERIODS, TimeWindow, useMonitorHistory } from './HistoryTimeline';
import ProcessMonitor from './ProcessMonitor';

interface HostMetrics {
  sampled_at: string;
  uptime_seconds: number | null;
  cpu: { percent: number | null; logical_count: number; model: string | null; load_average: number[] | null; cores: { index: number; percent: number | null }[] } | null;
  memory: { total_bytes: number; used_bytes: number; available_bytes: number; percent: number; swap_total_bytes: number; swap_used_bytes: number; swap_percent: number } | null;
  disks: DiskMetrics[];
  errors: Record<string, string>;
}

interface DiskMetrics {
  device: string;
  device_id: string;
  mountpoint: string;
  filesystem: string;
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  percent: number;
  read_bytes_per_second: number | null;
  write_bytes_per_second: number | null;
}

interface MonitorGPU {
  index: number;
  name: string;
  utilization_gpu: number;
  memory_used_mb: number;
  memory_total_mb: number;
  globally_enabled: boolean;
  scheduler_occupied: boolean;
  has_processes: boolean;
  cooldown_remaining_seconds?: number;
  leased_by_agent?: boolean;
}

type View = 'overview' | 'gpu' | 'cpu' | 'disk' | 'processes' | 'terminal';
type RunningTask = { id: string; name: string; gpu?: number };
const RETENTION = 2 * 60 * 1000;
const VIEW_LABELS: { id: View; label: string }[] = [
  { id: 'overview', label: '总览' }, { id: 'gpu', label: 'GPU' },
  { id: 'cpu', label: 'CPU 与内存' }, { id: 'disk', label: '磁盘' }, { id: 'processes', label: '进程' },
];
const CPU_SERIES: ChartSeries[] = [{ key: 'cpu', label: 'CPU 使用率', color: CHART_COLORS[0] }];
const MEMORY_SERIES: ChartSeries[] = [{ key: 'memory', label: '已用内存', color: CHART_COLORS[1] }];
const IO_SERIES: ChartSeries[] = [
  { key: 'read', label: '读取', color: CHART_COLORS[0] },
  { key: 'write', label: '写入', color: CHART_COLORS[1] },
];
const bytesValue = (value: number | null) => formatBytes(value);
const rateValue = (value: number | null) => formatBytes(value, true);

function sumRates(disks: DiskMetrics[], key: 'read_bytes_per_second' | 'write_bytes_per_second') {
  const known = disks.map(disk => disk[key]).filter((value): value is number => value != null);
  return known.length ? known.reduce((sum, value) => sum + value, 0) : null;
}

function ratio(used: number, total: number) {
  return total > 0 ? Math.min(100, Math.max(0, used / total * 100)) : null;
}

function Meter({ value, label, warnAt = 101 }: { value: number | null; label: string; warnAt?: number }) {
  return <div className="rm-meter" role="meter" aria-label={label} aria-valuenow={value ?? undefined} aria-valuemin={0} aria-valuemax={100}>
    <span className={value != null && value >= warnAt ? 'is-high' : ''} style={{ width: `${value ?? 0}%` }} />
  </div>;
}

function Summary({ icon, label, value, detail }: { icon: ReactNode; label: string; value: string; detail: string }) {
  return <div className="rm-summary-item"><div className="rm-summary-label">{icon}{label}</div><div className="rm-summary-reading"><strong>{value}</strong><span>{detail}</span></div></div>;
}

function SectionHeading({ title, detail, children }: { title: string; detail?: string; children?: ReactNode }) {
  return <div className="rm-section-heading"><div><h3>{title}</h3>{detail && <span>{detail}</span>}</div>{children}</div>;
}

function EmptyState({ children }: { children: ReactNode }) {
  return <div className="rm-empty">{children}</div>;
}

function GPUState({ gpu, stale }: { gpu: MonitorGPU; stale: boolean }) {
  let label = '空闲', kind = 'good';
  if (gpu.scheduler_occupied) { label = '任务运行中'; kind = 'active'; }
  else if (gpu.has_processes) { label = '外部占用'; kind = 'warn'; }
  if (!gpu.globally_enabled) { label = '调度已关闭'; kind = 'muted'; }
  if (gpu.leased_by_agent) { label = 'Agent 占用'; kind = 'warn'; }
  if ((gpu.cooldown_remaining_seconds ?? 0) > 0) { label = `冷却 ${gpu.cooldown_remaining_seconds}s`; kind = 'warn'; }
  if (stale) { label = '数据未更新'; kind = 'warn'; }
  return <span className={`rm-state ${kind}`}><i />{label}</span>;
}

function MiniTrend({ history, gpu }: { history: ChartPoint[]; gpu: MonitorGPU }) {
  const points = history.filter(point => point.time >= (history.at(-1)?.time ?? 0) - 120000);
  const end = points.at(-1)?.time ?? 0;
  const span = Math.min(120000, Math.max(10000, end - (points[0]?.time ?? end)));
  let path = '', move = true, previous: number | null = null;
  for (const point of points) {
    const value = point.values[`gpu-${gpu.index}`];
    if (value == null) { move = true; continue; }
    if (previous != null && point.time - previous > 10000) move = true;
    path += `${move ? 'M' : 'L'}${((point.time - end + span) / span * 112).toFixed(1)},${(25 - Math.min(100, value) * .22).toFixed(1)} `;
    move = false;
    previous = point.time;
  }
  return <svg className="rm-mini-trend" viewBox="0 0 112 28" role="img" aria-label={`GPU ${gpu.index} 最近 2 分钟使用率趋势`}><line x1="0" x2="112" y1="26" y2="26" className="rm-grid-line" /><path d={path} fill="none" stroke={CHART_COLORS[0]} strokeWidth="1.5" vectorEffect="non-scaling-stroke" /></svg>;
}

export default function ResourceMonitor({ terminal, tasks }: {
  terminal: ReactNode;
  tasks: RunningTask[];
}) {
  const [view, setView] = useState<View>('overview');
  const [host, setHost] = useState<HostMetrics | null>(null);
  const [gpus, setGpus] = useState<MonitorGPU[]>([]);
  const [recentHistory, setRecentHistory] = useState<ChartPoint[]>([]);
  const [paused, setPaused] = useState(false);
  const [fullScreen, setFullScreen] = useState(false);
  const [error, setError] = useState('');
  const [gpuError, setGpuError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [lastUpdate, setLastUpdate] = useState<number | null>(null);
  const [period, setPeriod] = useState(3600000);
  const [range, setRange] = useState<TimeWindow | null>(null);
  const [customOpen, setCustomOpen] = useState(false);
  const [diskFilter, setDiskFilter] = useState('all');
  const [cursorTime, setCursorTime] = useState<number | null>(null);
  const id = useId();
  const terminalView = view === 'terminal';
  const inactive = terminalView || view === 'processes';
  const timeline = useMonitorHistory(period, range, paused, !inactive, refresh);
  const history = timeline.points;

  useEffect(() => {
    if (paused || inactive) return;
    let disposed = false;
    let timer: number;
    let controller: AbortController;
    async function poll() {
      if (document.hidden) { timer = window.setTimeout(poll, 2000); return; }
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 8000);
      async function read<T,>(path: string): Promise<T> {
        const response = await fetch(path, { signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      }
      const [hostResult, gpuResult] = await Promise.allSettled([
        read<HostMetrics>('/api/system/metrics'), read<{ gpus: MonitorGPU[] }>('/api/gpus'),
      ]);
      window.clearTimeout(timeout);
      if (disposed) return;
      const nextHost = hostResult.status === 'fulfilled' ? hostResult.value : null;
      const nextGpus = gpuResult.status === 'fulfilled' ? gpuResult.value.gpus : null;
      setError(nextHost ? '' : '主机指标刷新失败，保留上次数据');
      setGpuError(nextGpus ? '' : 'GPU 指标刷新失败，保留上次数据');
      if (nextHost) setHost(nextHost);
      if (nextGpus) setGpus(nextGpus);
      if (nextHost || nextGpus) setLastUpdate(Date.now());
      const time = nextHost ? new Date(nextHost.sampled_at).getTime() : Date.now();
      const values: Record<string, number | null> = {
        cpu: nextHost?.cpu?.percent ?? null,
        memory: nextHost?.memory?.used_bytes ?? null,
        read: nextHost ? sumRates(nextHost.disks, 'read_bytes_per_second') : null,
        write: nextHost ? sumRates(nextHost.disks, 'write_bytes_per_second') : null,
      };
      for (const gpu of nextGpus ?? []) {
        values[`gpu-${gpu.index}`] = gpu.utilization_gpu;
        values[`vram-${gpu.index}`] = ratio(gpu.memory_used_mb, gpu.memory_total_mb);
      }
      for (const disk of nextHost?.disks ?? []) {
        values[`read-${disk.device_id}`] = disk.read_bytes_per_second;
        values[`write-${disk.device_id}`] = disk.write_bytes_per_second;
      }
      setRecentHistory(previous => previous.at(-1)?.time === time ? previous : [...previous, { time, values }].filter(point => point.time >= time - RETENTION).slice(-65));
      setLoading(false);
      timer = window.setTimeout(poll, 2000);
    }
    void poll();
    return () => { disposed = true; window.clearTimeout(timer); controller?.abort(); };
  }, [paused, refresh, inactive]);

  useEffect(() => {
    if (!fullScreen) return;
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setFullScreen(false); };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, [fullScreen]);

  const cpu = host?.cpu, memory = host?.memory, disks = host?.disks ?? [];
  const averageGpu = gpus.length ? gpus.reduce((sum, gpu) => sum + gpu.utilization_gpu, 0) / gpus.length : null;
  const selectedDisks = diskFilter === 'all' ? disks : disks.filter(disk => disk.device_id === diskFilter);
  const ioPoints = useMemo(() => diskFilter === 'all' ? history : history.map(point => {
    const pick = (values: Record<string, number | null> = {}) => ({ read: values[`read-${diskFilter}`] ?? null, write: values[`write-${diskFilter}`] ?? null });
    return { time: point.time, values: pick(point.values), minimum: pick(point.minimum), maximum: pick(point.maximum) };
  }), [history, diskFilter]);
  const gpuSeries = gpus.map((gpu, index) => ({ key: `gpu-${gpu.index}`, label: `GPU ${gpu.index}`, color: CHART_COLORS[index % CHART_COLORS.length] }));
  const vramSeries = gpuSeries.map(item => ({ ...item, key: item.key.replace('gpu-', 'vram-') }));
  const chartProps = { points: history, period, cursorTime, onCursorTime: setCursorTime,
    range: range ?? { start: Date.now() - period, end: Date.now() }, stepSeconds: timeline.step_seconds,
    onZoom: (start: number, end: number) => { setPeriod(end - start); setRange({ start, end }); setCursorTime(null); } };
  const show = (section: View) => view === 'overview' || view === section;
  const partialErrors = Object.keys(host?.errors ?? {}).map(key => ({ cpu: 'CPU', memory: '内存', disks: '磁盘', disk_io: '磁盘读写', uptime_seconds: '运行时间' }[key] ?? (key.startsWith('disk:') ? `磁盘 ${key.slice(5)}` : '部分主机指标')));

  return (
    <div className={`resource-monitor${fullScreen ? ' rm-fullscreen' : ''}`}>
      <div className="rm-viewbar">
        <div className="rm-tabs" role="tablist" aria-label="资源监控视图" onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
          const index = tabs.indexOf(document.activeElement as HTMLButtonElement);
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
          tabs[next].focus(); tabs[next].click();
        }}>
          {[...VIEW_LABELS, { id: 'terminal' as View, label: 'nvitop 终端' }].map(tab => <button key={tab.id} id={`${id}-${tab.id}`} type="button" role="tab" aria-selected={view === tab.id} aria-controls={`${id}-panel`} tabIndex={view === tab.id ? 0 : -1} onClick={() => { setView(tab.id); setCursorTime(null); }}>{tab.id === 'terminal' && <Terminal size={13} />}{tab.label}</button>)}
        </div>
        <div className="rm-view-controls">
          {!inactive && <span className="rm-update-time">{lastUpdate ? `更新于 ${new Date(lastUpdate).toLocaleTimeString('zh-CN', { hour12: false })}` : '等待采样'}</span>}
          <div className="rm-actions">
            {!inactive && <>
              <select aria-label="监控时间范围" value={PERIODS.some(([value]) => value === period) ? String(period) : 'custom'} onChange={event => {
                if (event.target.value === 'custom') { setCustomOpen(true); return; }
                setPeriod(Number(event.target.value)); setRange(null); setCursorTime(null);
              }}>
                {PERIODS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}<option value="custom">自定义范围</option>
              </select>
              <button type="button" onClick={() => setPaused(!paused)} aria-label={paused ? '恢复监控刷新' : '暂停监控刷新'}>{paused ? <Play size={14} /> : <Pause size={14} />}<span>{paused ? '继续' : '暂停刷新'}</span></button>
              <button type="button" onClick={() => { setPaused(false); setRefresh(value => value + 1); }} aria-label="立即刷新监控" title="立即刷新"><RefreshCcw size={14} className={loading ? 'rm-spinning' : ''} /></button>
            </>}
            <button type="button" onClick={() => setFullScreen(!fullScreen)} aria-label={fullScreen ? '退出监控全屏' : '监控全屏'} title={fullScreen ? '退出全屏 · Esc' : '全屏'}>{fullScreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}</button>
          </div>
        </div>
      </div>

      <div id={`${id}-panel`} role="tabpanel" aria-labelledby={`${id}-${view}`} className="rm-panel">
        {terminalView ? <div className="rm-terminal">{terminal}</div> : view === 'processes' ? <ProcessMonitor /> : <>
          <HistoryTimeline period={period} range={range} onRange={setRange} onPeriod={setPeriod} coverageStart={timeline.coverage_start} step={timeline.step_seconds} customOpen={customOpen} onCustomOpen={setCustomOpen} />
          {timeline.error && <div className="rm-notice" role="alert"><TriangleAlert size={15} />历史数据：{timeline.error}</div>}
          {(error || gpuError || partialErrors.length > 0) && <div className="rm-notice" role="alert"><TriangleAlert size={15} /><span>{[error, gpuError, partialErrors.length ? `${partialErrors.join('、')}暂不可用` : ''].filter(Boolean).join('；')}</span></div>}

          <div className="rm-summary">
            <Summary icon={<Cpu size={14} />} label="GPU 使用率" value={formatPercent(averageGpu)} detail={`${gpus.length} 张设备 · ${gpus.filter(gpu => gpu.scheduler_occupied).length} 张运行任务`} />
            <Summary icon={<Activity size={14} />} label="CPU 使用率" value={formatPercent(cpu?.percent)} detail={`负载 ${cpu?.load_average?.[0]?.toFixed(2) ?? '—'}`} />
            <Summary icon={<MemoryStick size={14} />} label="内存" value={formatPercent(memory?.percent)} detail={`${formatBytes(memory?.used_bytes)} / ${formatBytes(memory?.total_bytes)}`} />
            <Summary icon={<HardDrive size={14} />} label="存储可用" value={disks.length ? formatBytes(disks.reduce((sum, disk) => sum + disk.available_bytes, 0)) : '—'} detail={`${disks.length} 个文件系统`} />
          </div>

          {show('gpu') && <section className="rm-section">
            <SectionHeading title="GPU 设备" detail={`${gpus.length} 张`}><span className="rm-hint">使用率趋势 · 最近 2 分钟</span></SectionHeading>
            <div className="rm-table-scroll"><table className="rm-table rm-gpu-table">
              <thead><tr><th scope="col">设备</th><th scope="col">状态</th><th scope="col">GPU 使用率</th><th scope="col">显存占用</th><th scope="col">趋势</th><th scope="col">当前任务</th></tr></thead>
              <tbody>{gpus.map(gpu => {
                const task = tasks.find(task => task.gpu === gpu.index);
                return <tr key={gpu.index}>
                  <th scope="row"><div className="rm-device-name">GPU {gpu.index}<span>{gpu.name}</span></div></th>
                  <td><GPUState gpu={gpu} stale={Boolean(gpuError)} /></td>
                  <td><div className="rm-usage"><strong>{formatPercent(gpu.utilization_gpu)}</strong><Meter value={gpu.utilization_gpu} label={`GPU ${gpu.index} 使用率`} /></div></td>
                  <td><div className="rm-vram"><span>{formatBytes(gpu.memory_used_mb * 1024 ** 2)} <em>/ {formatBytes(gpu.memory_total_mb * 1024 ** 2)}</em></span><Meter value={ratio(gpu.memory_used_mb, gpu.memory_total_mb)} label={`GPU ${gpu.index} 显存使用率`} warnAt={90} /></div></td>
                  <td><MiniTrend history={recentHistory} gpu={gpu} /></td>
                  <td><span className="rm-task" title={task?.name}>{task ? <><span className="rm-task-id">#{task.id}</span>{task.name}</> : <span className="rm-secondary">—</span>}</span></td>
                </tr>;
              })}</tbody>
            </table></div>
            {!gpus.length && <EmptyState>{loading ? '正在读取 GPU…' : gpuError ? 'GPU 指标暂不可用' : '当前服务器没有可用的 GPU 设备'}</EmptyState>}
            {view === 'gpu' && gpus.length > 0 && <div className="rm-chart-grid rm-detail-charts">
              <MonitorChart {...chartProps} title="GPU 计算使用率" subtitle="各设备使用率 · %" series={gpuSeries} formatValue={formatPercent} percent />
              <MonitorChart {...chartProps} title="GPU 显存使用率" subtitle="各设备显存占比 · %" series={vramSeries} formatValue={formatPercent} percent />
            </div>}
          </section>}

          {show('cpu') && <section className="rm-section">
            <div className="rm-chart-grid">
              <MonitorChart {...chartProps} title="CPU 使用率" subtitle={`${cpu?.logical_count ?? '—'} 个逻辑核心 · %`} series={CPU_SERIES} formatValue={formatPercent} percent />
              <MonitorChart {...chartProps} title="内存使用量" subtitle={`总计 ${formatBytes(memory?.total_bytes)} · 可用 ${formatBytes(memory?.available_bytes)}`} series={MEMORY_SERIES} formatValue={bytesValue} />
            </div>
            <div className="rm-resource-notes"><span>系统负载 <b>{cpu?.load_average?.map(value => value.toFixed(2)).join(' / ') ?? '—'}</b><em>1 / 5 / 15 分钟</em></span><span>Swap <b>{memory?.swap_total_bytes === 0 ? '未启用' : `${formatBytes(memory?.swap_used_bytes)} / ${formatBytes(memory?.swap_total_bytes)}`}</b></span></div>
            <details className="rm-cores" open={view === 'cpu' || undefined}>
              <summary><ChevronDown size={14} />CPU 核心详情<span>{cpu?.model ?? '处理器信息不可用'}</span></summary>
              <div className="rm-core-grid">{cpu?.cores.map(core => <div key={core.index} className={core.percent != null && core.percent >= 90 ? 'is-high' : ''} title={`CPU ${core.index}: ${formatPercent(core.percent)}`}><span>{String(core.index).padStart(2, '0')}</span><strong>{formatPercent(core.percent)}</strong><Meter value={core.percent} label={`CPU ${core.index} 使用率`} /></div>) ?? <EmptyState>CPU 核心指标不可用</EmptyState>}</div>
            </details>
          </section>}

          {show('disk') && <section className="rm-section">
            <SectionHeading title="磁盘与存储" detail={`${disks.length} 个文件系统`}>
              <div className="rm-io-summary"><span><ArrowDown size={12} />读取 <b>{formatBytes(sumRates(disks, 'read_bytes_per_second'), true)}</b></span><span><ArrowUp size={12} />写入 <b>{formatBytes(sumRates(disks, 'write_bytes_per_second'), true)}</b></span></div>
            </SectionHeading>
            <div className="rm-table-scroll"><table className="rm-table rm-disk-table">
              <colgroup><col className="rm-disk-col-mount" /><col className="rm-disk-col-size" span={3} /><col className="rm-disk-col-usage" /><col className="rm-disk-col-rate" span={2} /></colgroup>
              <thead><tr><th scope="col">挂载点 / 设备</th><th scope="col">容量</th><th scope="col">已用</th><th scope="col">可用</th><th scope="col">使用率</th><th scope="col">读取 / 秒</th><th scope="col">写入 / 秒</th></tr></thead>
              <tbody>{disks.map(disk => <tr key={disk.device_id}>
                <th scope="row"><div className="rm-mount" title={disk.mountpoint}>{disk.mountpoint}<span title={`${disk.device} · ${disk.filesystem}`}>{disk.device} · {disk.filesystem}</span></div></th>
                <td>{formatBytes(disk.total_bytes)}</td><td>{formatBytes(disk.used_bytes)}</td><td className={disk.percent >= 90 ? 'rm-danger' : ''}>{formatBytes(disk.available_bytes)}</td>
                <td><div className="rm-usage"><strong className={disk.percent >= 90 ? 'rm-danger' : ''}>{formatPercent(disk.percent)}</strong><Meter value={disk.percent} label={`${disk.mountpoint} 磁盘使用率`} warnAt={90} /></div></td>
                <td>{formatBytes(disk.read_bytes_per_second, true)}</td><td>{formatBytes(disk.write_bytes_per_second, true)}</td>
              </tr>)}</tbody>
            </table></div>
            {!disks.length && <EmptyState>{loading ? '正在读取磁盘…' : '没有可用的本地文件系统指标'}</EmptyState>}
            {view === 'disk' && <div className="rm-detail-charts">
              <div className="rm-disk-filter"><label htmlFor={`${id}-disk-filter`}>读写趋势</label><select id={`${id}-disk-filter`} value={diskFilter} onChange={event => setDiskFilter(event.target.value)}><option value="all">全部文件系统</option>{disks.map(disk => <option key={disk.device_id} value={disk.device_id}>{disk.mountpoint}</option>)}</select></div>
              <MonitorChart {...chartProps} points={ioPoints} title="磁盘读写吞吐" subtitle={diskFilter === 'all' ? '所有可用设备的合计速率' : selectedDisks[0]?.mountpoint} series={IO_SERIES} formatValue={rateValue} />
              <p className="rm-hint rm-io-note">未提供块设备计数的文件系统不计入读写合计。</p>
            </div>}
          </section>}
          <footer className="rm-footer"><span><i />{paused ? '刷新已暂停' : '每 2 秒更新'} · 悬停图表查看读数</span><span>服务端保留 7 天历史 · 阴影表示采样区间范围 · 空间不足 10% 标红</span></footer>
        </>}
      </div>
    </div>
  );
}
