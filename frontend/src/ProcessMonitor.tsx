import React, { ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Copy, Pause, Play, RefreshCcw, Search, Square, X } from 'lucide-react';
import MonitorChart, { CHART_COLORS, ChartPoint, formatBytes, formatPercent } from './MonitorChart';

interface ProcessRow {
  pid: number; ppid: number; create_time: number; uid: number; user: string; name: string;
  command: string; status: string; cpu_percent: number | null; memory_percent: number;
  rss_bytes: number; vms_bytes: number; elapsed_seconds: number; cpu_seconds: number;
  threads: number; nice: number; read_bytes_per_second: number | null; write_bytes_per_second: number | null;
  gpus: { index: number; memory_bytes: number | null }[];
  task: { id: number; name: string } | null;
  can_signal: boolean; signal_disabled_reason: string | null; context: boolean;
}
interface ProcessResponse {
  items: ProcessRow[]; total: number; all_total: number; sampled_at: number; boot_id: string;
  service_uid: number; users: string[]; statuses: string[]; gpu_indices: number[]; gpu_error: string | null;
}
type Action = 'terminate' | 'kill' | 'stop' | 'continue';
type Result = { pid: number; ok: boolean; message: string };
const ACTIONS: Record<Action, string> = { terminate: '终止', kill: '强制终止', stop: '暂停进程', continue: '继续进程' };
const STATES: Record<string, string> = { running: '运行', sleeping: '睡眠', 'disk-sleep': 'I/O 等待', stopped: '已暂停', tracing: '跟踪', zombie: '僵尸', dead: '已结束', idle: '空闲' };
const identity = (row: Pick<ProcessRow, 'pid' | 'create_time'>) => `${row.pid}:${row.create_time}`;
function duration(seconds: number) {
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return days ? `${days}天 ${hours}时` : hours ? `${hours}时 ${minutes}分` : `${minutes}分 ${Math.floor(seconds % 60)}秒`;
}
async function readJson(response: Response) {
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`);
  return data;
}

function ProcessDialog({ title, onClose, children, drawer = false }: { title: string; onClose: () => void; children: ReactNode; drawer?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>('button')?.focus();
    function keydown(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const elements = Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, [tabindex="0"]') ?? []);
      if (!elements.length) return;
      const first = elements[0], last = elements.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); previous?.focus(); };
  }, []);
  return createPortal(<div className={`resource-monitor pm-overlay ${drawer ? 'pm-drawer-overlay' : ''}`}>
    <button type="button" className="pm-backdrop" aria-label="关闭弹窗" onClick={onClose} />
    <div ref={ref} className={drawer ? 'pm-drawer' : 'pm-dialog'} role="dialog" aria-modal="true" aria-label={title}>
      <div className="pm-dialog-heading"><h2>{title}</h2><button type="button" onClick={onClose} aria-label={`关闭${title}`}><X size={17} /></button></div>
      {children}
    </div>
  </div>, document.body);
}

export default function ProcessMonitor() {
  const [data, setData] = useState<ProcessResponse | null>(null);
  const [search, setSearch] = useState(''), [query, setQuery] = useState('');
  const [scope, setScope] = useState('all'), [user, setUser] = useState(''), [status, setStatus] = useState(''), [gpu, setGpu] = useState('');
  const [sort, setSort] = useState('cpu_percent'), [descending, setDescending] = useState(true);
  const [mode, setMode] = useState('list'), [offset, setOffset] = useState(0);
  const [paused, setPaused] = useState(false), [refresh, setRefresh] = useState(0), [loading, setLoading] = useState(true);
  const [error, setError] = useState(''), [selected, setSelected] = useState<Record<string, ProcessRow>>({});
  const [focused, setFocused] = useState<ProcessRow | null>(null);
  const [detail, setDetail] = useState<Record<string, any> | null>(null), [detailError, setDetailError] = useState('');
  const [processHistory, setProcessHistory] = useState<ChartPoint[]>([]), [cursorTime, setCursorTime] = useState<number | null>(null);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [confirmation, setConfirmation] = useState<{ action: Action; rows: ProcessRow[]; boot_id: string } | null>(null);
  const [busy, setBusy] = useState(false), [results, setResults] = useState<Result[]>([]), [auditWarning, setAuditWarning] = useState('');
  const latestBoot = useRef('');
  const displayOrder = useRef<string[]>([]);

  useEffect(() => { const timer = window.setTimeout(() => { setQuery(search); setOffset(0); }, 250); return () => window.clearTimeout(timer); }, [search]);
  useEffect(() => { setOffset(0); }, [scope, user, status, gpu, sort, descending, mode]);
  useEffect(() => {
    let disposed = false, timer: number, controller: AbortController;
    async function load() {
      if (document.hidden) { timer = window.setTimeout(load, 2000); return; }
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 10000);
      const params = new URLSearchParams({ search: query, scope, user, status, sort, descending: String(descending), offset: String(offset), limit: '100', tree: String(mode === 'tree') });
      if (gpu) params.set('gpu', gpu);
      try {
        const result: ProcessResponse = await readJson(await fetch(`/api/system/processes?${params}`, { signal: controller.signal }));
        if (disposed) return;
        if (latestBoot.current && latestBoot.current !== result.boot_id) { setSelected({}); setFocused(null); setConfirmation(null); }
        latestBoot.current = result.boot_id;
        setData(result); setError('');
        if (offset >= result.total && offset > 0) setOffset(Math.max(0, Math.floor((result.total - 1) / 100) * 100));
      } catch (error) {
        if (!disposed) setError(error instanceof Error ? error.message : '进程列表读取失败');
      } finally {
        window.clearTimeout(timeout);
        if (!disposed) { setLoading(false); if (!paused) timer = window.setTimeout(load, 2000); }
      }
    }
    void load();
    return () => { disposed = true; window.clearTimeout(timer); controller?.abort(); };
  }, [query, scope, user, status, gpu, sort, descending, offset, mode, paused, refresh]);

  useEffect(() => {
    setDetail(null); setDetailError(''); setProcessHistory([]); setCursorTime(null);
  }, [focused?.pid, focused?.create_time]);
  useEffect(() => {
    if (!focused || !data) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ create_time: String(focused.create_time), boot_id: data.boot_id });
    fetch(`/api/system/processes/${focused.pid}?${params}`, { signal: controller.signal }).then(readJson)
      .then(value => { setDetail(value); setDetailError(''); })
      .catch(error => { if (!controller.signal.aborted) setDetailError(error.message); });
    const row = data.items.find(row => identity(row) === identity(focused));
    if (row) setProcessHistory(previous => [...previous, { time: data.sampled_at * 1000, values: { cpu: row.cpu_percent, memory: row.rss_bytes } }].filter(point => point.time >= Date.now() - 600000).slice(-300));
    return () => controller.abort();
  }, [focused?.pid, focused?.create_time, data?.sampled_at]);

  const rows = useMemo(() => {
    if (mode !== 'tree') {
      const items = [...(data?.items ?? [])];
      if (Object.keys(selected).length) {
        const ranks = new Map(displayOrder.current.map((key, index) => [key, index]));
        items.sort((a, b) => (ranks.get(identity(a)) ?? Infinity) - (ranks.get(identity(b)) ?? Infinity));
      }
      displayOrder.current = items.map(identity);
      return items.map(row => ({ row, depth: 0, hasChildren: false }));
    }
    const items = data?.items ?? [], ids = new Set(items.map(row => row.pid));
    const children = new Map<number, ProcessRow[]>();
    for (const row of items) { const parent = ids.has(row.ppid) && row.ppid !== row.pid ? row.ppid : 0; children.set(parent, [...(children.get(parent) ?? []), row]); }
    const result: { row: ProcessRow; depth: number; hasChildren: boolean }[] = [], visited = new Set<number>();
    function visit(row: ProcessRow, depth: number) {
      if (visited.has(row.pid)) return;
      visited.add(row.pid); result.push({ row, depth, hasChildren: children.has(row.pid) });
      if (!collapsed.has(row.pid)) for (const child of children.get(row.pid) ?? []) visit(child, depth + 1);
    }
    for (const row of children.get(0) ?? []) visit(row, 0);
    return result;
  }, [data, mode, collapsed, Object.keys(selected).length > 0]);
  const checked = Object.values(selected);
  const selectable = rows.map(item => item.row).filter(row => row.can_signal && !row.context);
  const liveFocused = data?.items.find(row => focused && identity(row) === identity(focused)) ?? focused;

  function toggle(row: ProcessRow) {
    setSelected(previous => {
      const next = { ...previous }, key = identity(row);
      if (next[key]) delete next[key]; else if (Object.keys(next).length < 100) next[key] = row;
      return next;
    });
  }
  function requestAction(action: Action, rows = checked) {
    if (data && rows.length) setConfirmation({ action, rows: rows.map(row => ({ ...row })), boot_id: data.boot_id });
  }
  async function execute() {
    if (!confirmation || busy) return;
    setBusy(true);
    try {
      const result = await readJson(await fetch('/api/system/processes/signal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: confirmation.action, boot_id: confirmation.boot_id, targets: confirmation.rows.map(({ pid, create_time }) => ({ pid, create_time })) }) }));
      setResults(result.results); setAuditWarning(result.audit_warning ?? ''); setConfirmation(null);
      setSelected({}); setPaused(false); setRefresh(value => value + 1);
    } catch (error) { setError(error instanceof Error ? error.message : '进程操作失败'); }
    finally { setBusy(false); }
  }
  function sortHeading(key: string, label: string) {
    return <th scope="col" aria-sort={sort === key ? descending ? 'descending' : 'ascending' : 'none'}><button type="button" onClick={() => { setSort(key); setDescending(sort === key ? !descending : true); }}>{label}{sort === key && (descending ? <ArrowDown size={11} /> : <ArrowUp size={11} />)}</button></th>;
  }

  return <section className="pm-workbench" aria-label="进程工作台">
    <div className="pm-heading"><div><h3>运行进程 <span>{data?.all_total ?? '—'}</span></h3><p>CPU 以单核 100% 计 · {paused ? '列表刷新已暂停' : '每 2 秒刷新'}</p></div><div className="pm-refresh"><button type="button" aria-label={paused ? '恢复进程列表刷新' : '暂停进程列表刷新'} onClick={() => setPaused(!paused)}>{paused ? <Play size={13} /> : <Pause size={13} />}{paused ? '继续刷新' : '暂停刷新'}</button><button type="button" aria-label="立即刷新进程列表" onClick={() => { setPaused(false); setRefresh(value => value + 1); }}><RefreshCcw size={13} /></button></div></div>
    <div className="pm-filters">
      <label className="pm-search"><Search size={15} /><input aria-label="搜索进程" value={search} placeholder="搜索 PID、进程名或命令…" onChange={event => setSearch(event.target.value)} />{search && <button type="button" aria-label="清空进程搜索" onClick={() => setSearch('')}><X size={13} /></button>}</label>
      <select aria-label="筛选进程用户" value={user} onChange={event => setUser(event.target.value)}><option value="">所有用户</option>{data?.users.map(user => <option key={user}>{user}</option>)}</select>
      <select aria-label="筛选进程状态" value={status} onChange={event => setStatus(event.target.value)}><option value="">所有状态</option>{data?.statuses.map(status => <option key={status} value={status}>{STATES[status] ?? status}</option>)}</select>
      <select aria-label="筛选进程 GPU" value={gpu} onChange={event => setGpu(event.target.value)}><option value="">所有 GPU</option>{data?.gpu_indices.map(index => <option key={index} value={index}>GPU {index}</option>)}</select>
      <select aria-label="进程显示方式" value={mode} onChange={event => setMode(event.target.value)}><option value="list">列表视图</option><option value="tree">进程树</option><option value="io">I/O 视图</option></select>
    </div>
    <div className="pm-scopebar"><div className="pm-scopes">{[['all', '全部进程'], ['mine', '我的进程'], ['gpu', 'GPU 进程'], ['tasks', '调度任务']].map(([value, label]) => <button type="button" key={value} aria-pressed={scope === value} onClick={() => setScope(value)}>{label}</button>)}</div><span className="rm-hint">匹配 {data?.total ?? 0} 个{data && ` · 更新于 ${new Date(data.sampled_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}`}</span></div>
    {error && <div className="rm-notice" role="alert">{error}</div>}
    {data?.gpu_error && <div className="rm-notice" role="status">GPU 进程指标暂不可用；其他进程数据可继续查看。</div>}
    <div className="pm-actions"><span>已选 {checked.length} / 100</span>{(Object.keys(ACTIONS) as Action[]).map(action => <button type="button" key={action} disabled={!checked.length} className={action === 'kill' ? 'pm-danger-button' : ''} onClick={() => requestAction(action)}>{action === 'terminate' && <Square size={12} />}{ACTIONS[action]}</button>)}{checked.length > 0 && <button type="button" onClick={() => setSelected({})}>清除选择</button>}</div>
    {results.length > 0 && <div className="pm-results" role="status"><div><strong>操作结果 · {results.filter(result => result.ok).length}/{results.length} 个信号已发送</strong><button type="button" aria-label="关闭进程操作结果" onClick={() => setResults([])}><X size={13} /></button></div>{results.map((result, index) => <p key={index} className={result.ok ? '' : 'rm-danger'}>PID {result.pid} · {result.message}</p>)}{auditWarning && <p className="rm-danger">{auditWarning}</p>}</div>}
    <div className="rm-table-scroll pm-table-scroll"><table className={`rm-table pm-table${mode === 'io' ? ' pm-table-io' : ''}`}>
      <colgroup>
        <col className="pm-col-select" /><col className="pm-col-pid" /><col className="pm-col-user" />
        <col className="pm-col-state" /><col className="pm-col-percent" /><col className="pm-col-percent" />
        <col className="pm-col-memory" />
        {mode === 'io' ? <><col className="pm-col-io" /><col className="pm-col-io" /></> : <col className="pm-col-gpu" />}
        <col className="pm-col-elapsed" /><col />
      </colgroup>
      <thead><tr><th scope="col"><input type="checkbox" aria-label="选择本页可操作进程" checked={selectable.length > 0 && selectable.every(row => selected[identity(row)])} onChange={event => {
        if (!event.target.checked) setSelected(previous => Object.fromEntries(Object.entries(previous).filter(([key]) => !selectable.some(row => identity(row) === key))));
        else setSelected(previous => Object.fromEntries([...Object.entries(previous), ...selectable.map(row => [identity(row), row] as const)].slice(0, 100)));
      }} /></th>{sortHeading('pid', 'PID')}{sortHeading('user', '用户')}<th scope="col">状态</th>{sortHeading('cpu_percent', 'CPU %')}{sortHeading('memory_percent', '内存 %')}{sortHeading('rss_bytes', 'RES')}{mode === 'io' ? <>{sortHeading('read_bytes_per_second', '读取 / 秒')}{sortHeading('write_bytes_per_second', '写入 / 秒')}</> : <th scope="col">GPU / 显存</th>}{sortHeading('elapsed_seconds', '运行时长')}<th scope="col">命令</th></tr></thead>
      <tbody>{rows.map(({ row, depth, hasChildren }) => <tr key={identity(row)} className={`${selected[identity(row)] ? 'pm-selected' : ''} ${row.context ? 'pm-context' : ''}`}>
        <td><input type="checkbox" aria-label={`选择进程 ${row.pid}`} checked={Boolean(selected[identity(row)])} disabled={!row.can_signal || (checked.length >= 100 && !selected[identity(row)])} title={row.signal_disabled_reason ?? undefined} onChange={() => toggle(row)} /></td>
        <th scope="row"><button type="button" className="pm-pid" onClick={() => setFocused(row)}>{row.pid}</button></th><td title={row.user}>{row.user}</td><td title={STATES[row.status] ?? row.status}><span className={`pm-state ${row.status === 'running' ? 'good' : ''}`}>{STATES[row.status] ?? row.status}</span></td>
        <td className={row.cpu_percent != null && row.cpu_percent >= 100 ? 'pm-busy' : ''}>{formatPercent(row.cpu_percent)}</td><td>{formatPercent(row.memory_percent)}</td><td>{formatBytes(row.rss_bytes)}</td>
        {mode === 'io' ? <><td>{formatBytes(row.read_bytes_per_second, true)}</td><td>{formatBytes(row.write_bytes_per_second, true)}</td></> : <td title={row.gpus.map(gpu => `GPU ${gpu.index}: ${formatBytes(gpu.memory_bytes)}`).join('，')}>{row.gpus.length ? row.gpus.map(gpu => <span key={gpu.index} className="pm-gpu">{gpu.index} <em>{formatBytes(gpu.memory_bytes)}</em></span>) : <span className="rm-secondary">—</span>}</td>}
        <td>{duration(row.elapsed_seconds)}</td><td><div className="pm-command" style={{ paddingLeft: mode === 'tree' ? Math.min(12, depth) * 14 : 0 }}>{mode === 'tree' && (hasChildren ? <button type="button" aria-label={`${collapsed.has(row.pid) ? '展开' : '折叠'}进程 ${row.pid}`} onClick={() => setCollapsed(previous => { const next = new Set(previous); if (next.has(row.pid)) next.delete(row.pid); else next.add(row.pid); return next; })}>{collapsed.has(row.pid) ? <ChevronRight size={12} /> : <ChevronDown size={12} />}</button> : <span className="pm-tree-spacer" />)}<button type="button" title={row.command} onClick={() => setFocused(row)}>{row.command}</button>{row.task && <span className="pm-task-tag" title={row.task.name}>任务 #{row.task.id}</span>}</div></td>
      </tr>)}</tbody>
    </table>{!rows.length && <div className="rm-empty">{loading ? '正在读取进程…' : '没有匹配的进程'}</div>}</div>
    <div className="pm-footer"><span>查看所有可读进程；操作限服务用户 UID {data?.service_uid ?? '—'}。取消调度任务请前往任务队列。</span><div><span>{data?.total ? `${offset + 1}–${Math.min(offset + 100, data.total)} / ${data.total}` : '0 / 0'}</span><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>上一页</button><button type="button" disabled={!data || offset + 100 >= data.total} onClick={() => setOffset(offset + 100)}>下一页</button></div></div>

    {focused && !confirmation && <ProcessDialog title={`进程详情 · ${focused.pid}`} drawer onClose={() => setFocused(null)}>
      <div className="pm-detail-body">
        <div className="pm-detail-title"><h3>{liveFocused?.name}</h3><span>{detail?.status ? STATES[detail.status] ?? detail.status : STATES[focused.status] ?? focused.status}</span></div>
        {detailError && <div className="rm-notice" role="alert">{detailError}</div>}
        <div className="pm-detail-command"><pre>{detail?.command || focused.command}</pre><button type="button" aria-label="复制进程命令" onClick={() => { if (!navigator.clipboard) { setDetailError('复制不可用，请手动选择命令文本'); return; } void navigator.clipboard.writeText(detail?.command || focused.command).catch(() => setDetailError('复制失败，请手动选择命令文本')); }}><Copy size={14} /></button></div>
        <dl className="pm-detail-grid">{[
          ['用户', `${focused.user} (${focused.uid})`], ['父进程', detail?.ppid ?? focused.ppid], ['启动时间', new Date(focused.create_time * 1000).toLocaleString('zh-CN')],
          ['运行时长', duration(liveFocused?.elapsed_seconds ?? 0)], ['CPU', formatPercent(liveFocused?.cpu_percent)], ['累计 CPU', duration(liveFocused?.cpu_seconds ?? 0)],
          ['常驻内存 RES', formatBytes(liveFocused?.rss_bytes)], ['虚拟内存 VIRT', formatBytes(liveFocused?.vms_bytes)], ['线程', detail?.threads ?? focused.threads], ['Nice', detail?.nice ?? focused.nice],
          ['读取 / 秒', formatBytes(liveFocused?.read_bytes_per_second, true)], ['写入 / 秒', formatBytes(liveFocused?.write_bytes_per_second, true)], ['文件描述符', detail?.file_descriptors ?? '—'],
          ['工作目录', detail?.cwd ?? '—'], ['可执行文件', detail?.exe ?? '—'], ['关联任务', focused.task ? `#${focused.task.id} ${focused.task.name}` : '—'],
        ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
        <div className="pm-detail-charts"><MonitorChart title="进程 CPU" percent percentLimit={Infinity} subtitle="单核 100% · 选中期间采样" points={processHistory} series={[{ key: 'cpu', label: 'CPU', color: CHART_COLORS[0] }]} period={600000} formatValue={formatPercent} cursorTime={cursorTime} onCursorTime={setCursorTime} /><MonitorChart title="进程内存" subtitle="常驻内存 RES" points={processHistory} series={[{ key: 'memory', label: 'RES', color: CHART_COLORS[1] }]} period={600000} formatValue={formatBytes} cursorTime={cursorTime} onCursorTime={setCursorTime} /></div>
        <div className="pm-detail-actions">{(Object.keys(ACTIONS) as Action[]).map(action => <button type="button" key={action} disabled={!focused.can_signal || Boolean(detailError)} onClick={() => requestAction(action, [focused])}>{ACTIONS[action]}</button>)}</div>
        {focused.signal_disabled_reason && <p className="rm-hint">{focused.signal_disabled_reason}</p>}
      </div>
    </ProcessDialog>}
    {confirmation && <ProcessDialog title={`${ACTIONS[confirmation.action]} ${confirmation.rows.length} 个进程`} onClose={() => { if (!busy) setConfirmation(null); }}>
      <div className="pm-confirm-body"><p>{confirmation.action === 'kill' ? '将发送 SIGKILL，进程无法自行清理资源。' : `将发送 ${({ terminate: 'SIGTERM', stop: 'SIGSTOP', continue: 'SIGCONT' } as Record<string, string>)[confirmation.action]}。`}</p>
        <div className="pm-confirm-list">{confirmation.rows.map(row => <div key={identity(row)}><strong>PID {row.pid} · {row.user}</strong><span>{row.command}</span></div>)}</div>
        {confirmation.rows.some(row => row.task) && <p className="rm-hint">进程信号不会取消所属调度任务，任务可能按现有策略重新入队。</p>}
        {error && <p className="rm-danger" role="alert">{error}</p>}
        <div className="pm-confirm-actions"><button type="button" disabled={busy} onClick={() => setConfirmation(null)}>返回</button><button type="button" className={confirmation.action === 'kill' || confirmation.action === 'terminate' ? 'pm-danger-button' : 'pm-primary-button'} disabled={busy} onClick={execute}>{busy ? '正在发送…' : `确认${ACTIONS[confirmation.action]}`}</button></div>
      </div>
    </ProcessDialog>}
  </section>;
}
