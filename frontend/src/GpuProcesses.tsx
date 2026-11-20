import { ReactNode, useEffect, useState } from 'react';
import { ArrowUpRight, TriangleAlert } from 'lucide-react';
import { formatBytes, formatPercent } from './MonitorChart';
import type { ProcessResponse, ProcessRow } from './ProcessMonitor';

export default function GpuProcesses({ gpus, paused, refresh, onInspect, renderDevice }: {
  gpus: { index: number; name: string }[];
  paused: boolean;
  refresh: number;
  onInspect: (gpu: number, pid?: number) => void;
  renderDevice?: (gpu: number) => ReactNode;
}) {
  const compact = Boolean(renderDevice);
  const [data, setData] = useState<ProcessResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (paused) { setLoading(false); return; }
    setLoading(true);
    let disposed = false, timer: number, controller: AbortController;
    async function load() {
      if (document.hidden) { timer = window.setTimeout(load, 2000); return; }
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 10000);
      try {
        const items: ProcessRow[] = [];
        let offset = 0, page: ProcessResponse;
        // Fetch every page so GPUs are not silently missing processes after the API limit.
        do {
          const params = new URLSearchParams({ scope: 'gpu', sort: 'pid', descending: 'false', limit: '500', offset: String(offset) });
          const response = await fetch(`/api/system/processes?${params}`, { signal: controller.signal });
          page = await response.json();
          if (!response.ok) throw new Error('GPU 进程读取失败');
          if (page.gpu_error) throw new Error(page.gpu_error);
          if (!page.items.length && offset < page.total) throw new Error('进程列表发生变化，正在重新读取');
          items.push(...page.items);
          offset += page.items.length;
        } while (offset < page.total);
        if (!disposed) {
          const unique = [...new Map(items.map(row => [`${row.pid}:${row.create_time}`, row])).values()];
          setData({ ...page, items: unique }); setError('');
        }
      } catch (error) {
        if (!disposed) setError(error instanceof Error && error.name !== 'AbortError' ? error.message : 'GPU 进程读取超时');
      } finally {
        window.clearTimeout(timeout);
        if (!disposed) { setLoading(false); timer = window.setTimeout(load, 2000); }
      }
    }
    void load();
    return () => { disposed = true; controller?.abort(); window.clearTimeout(timer); };
  }, [paused, refresh]);

  return <div className={`rm-gpu-processes${compact ? ' rm-gpu-processes-compact' : ''}`} aria-label="按 GPU 分组的当前进程" aria-busy={loading && !data}>
    <div className="rm-section-heading">
      <div><h3>{compact ? 'GPU 与当前进程' : 'GPU 进程'}</h3><span>{compact ? `${gpus.length} 张设备` : '按 GPU 分组'}{data && ` · ${data.items.length} 个进程`}</span></div>
      <span className="rm-hint">{paused ? '刷新已暂停' : '当前进程 · 每 2 秒刷新'}</span>
    </div>
    {error && <div className="rm-notice" role="alert"><TriangleAlert size={14} /><span>{error}{data ? '；保留上次采样，请以恢复后的数据为准' : ''}</span></div>}
    <div className="rm-gpu-process-grid">{gpus.map(gpu => {
      const rows = (data?.items ?? []).filter(row => row.gpus.some(item => item.index === gpu.index)).sort((a, b) => a.pid - b.pid);
      const empty = error ? '进程信息暂不可用' : loading ? '正在读取进程…' : paused && !data ? '恢复刷新后读取进程' : '当前没有 GPU 进程';
      return <section className="rm-gpu-process-group" key={gpu.index} aria-label={`GPU ${gpu.index} 当前进程`}>
        {renderDevice?.(gpu.index)}
        <div className="rm-gpu-process-panel">
        {!compact && <div className="rm-gpu-process-heading">
          <div><h4>GPU {gpu.index}</h4><span title={gpu.name}>{gpu.name}</span></div>
          <span className="rm-gpu-process-count">{data ? `${rows.length} 个进程${error ? ' · 上次采样' : ''}` : '—'}</span>
          <button type="button" onClick={() => onInspect(gpu.index)} aria-label={`在工作台查看 GPU ${gpu.index} 进程`}>进程工作台<ArrowUpRight size={13} /></button>
        </div>}
        {rows.length > 0 ? <div className="rm-table-scroll rm-gpu-process-scroll">
          <table className="rm-table rm-gpu-process-table" aria-label={`GPU ${gpu.index} 进程列表`}>
            <colgroup><col className="rm-gp-col-pid" /><col className="rm-gp-col-user" /><col className="rm-gp-col-memory" />{!compact && <col className="rm-gp-col-cpu" />}<col /></colgroup>
            <thead><tr><th scope="col">PID</th><th scope="col">用户</th><th scope="col">显存占用</th>{!compact && <th scope="col" title="进程 CPU 使用率，单个逻辑核心满载为 100%">CPU %</th>}<th scope="col"><div className="rm-gpu-command-heading"><span>运行命令</span>{compact && <button type="button" className="rm-gpu-inspect" onClick={() => onInspect(gpu.index)} aria-label={`在工作台查看 GPU ${gpu.index} 进程`}>进程工作台<ArrowUpRight size={13} /></button>}</div></th></tr></thead>
            <tbody>{rows.map(row => {
              const command = compact ? row.command.match(/^(\S+)(.*)$/s) : null;
              return <tr key={`${row.pid}:${row.create_time}`}>
                <td><button type="button" className="rm-gpu-process-pid" onClick={() => onInspect(gpu.index, row.pid)} aria-label={`查看 GPU ${gpu.index} 进程 ${row.pid}`}>{row.pid}</button></td>
                <td title={row.user}>{row.user}</td>
                <td>{formatBytes(row.gpus.find(item => item.index === gpu.index)?.memory_bytes)}</td>
                {!compact && <td>{formatPercent(row.cpu_percent)}</td>}
                <td><div className="rm-gpu-process-command">
                  <code title={row.command}>{command ? <><strong>{command[1]}</strong>{command[2]}</> : row.command}</code>
                  {row.task && <span className="pm-task-tag" title={row.task.name}>任务 #{row.task.id}</span>}
                </div></td>
              </tr>;
            })}</tbody>
          </table>
        </div> : <p className="rm-gpu-process-empty"><span>{empty}</span>{compact && <button type="button" className="rm-gpu-inspect" onClick={() => onInspect(gpu.index)} aria-label={`在工作台查看 GPU ${gpu.index} 进程`}>进程工作台<ArrowUpRight size={13} /></button>}</p>}
        </div>
      </section>;
    })}</div>
  </div>;
}
