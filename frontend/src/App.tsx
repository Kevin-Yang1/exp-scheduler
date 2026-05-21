/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import {
  Activity,
  Cpu,
  History,
  Layers,
  Play,
  Plus,
  RefreshCcw,
  Server,
  Settings,
  Terminal,
  Trash2,
  Pause,
  AlertCircle,
  CheckCircle2,
  Clock,
  ChevronRight,
  Monitor,
  RotateCw,
  FileText,
  Copy,
  Loader2,
  Maximize2,
  Minimize2,
	  Bookmark,
	  Edit2,
	  Link2,
	  Search
	} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import React, { useCallback, useEffect, useMemo, useRef, useState, ReactNode, FormEvent } from 'react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';

// --- Types ---
type GpuScheduleAction = 'enable' | 'disable';

interface GpuScheduleEntry {
  action: GpuScheduleAction;
  run_at: string;
}

interface GPUStatus {
  id: number;
  name: string;
  memoryUsed: number;
  memoryTotal: number;
  memoryFree: number;
  utilization: number;
  isBusy: boolean;
  isCoolingDown?: boolean;
  cooldownRemainingSeconds?: number;
}

interface Task {
  id: string;
  name: string;
  status: 'running' | 'pending' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
  command: string;
  workingDir?: string;
  gpu?: number;
  profile?: string;
  startedAt?: string;
  endedAt?: string;
  exitCode?: number;
  isUrgent?: boolean;
  attempts?: number;
  notes?: string;
  env?: Record<string, string>;
  requestedGpu?: number | null;
  gpuMemoryBudgetMb?: number | null;
  profileId?: number | null;
  queueName?: 'normal' | 'urgent';
  dependsOn?: number[];
  dependencyCount?: number;
  hasDependencies?: boolean;
  attemptLogs?: TaskLogEntry[];
  raw?: BackendTask;
}

type DependencyCandidate = Pick<Task, 'id' | 'name' | 'status'> & {
  isMissingDependency?: boolean;
};

interface BackendTask {
  id: number;
  name?: string | null;
  command: string;
  cwd?: string | null;
  env?: Record<string, string>;
  notes?: string | null;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
  assigned_gpu?: number | null;
  requested_gpu?: number | null;
  gpu_memory_budget_mb?: number | null;
  profile_id?: number | null;
  profile_name?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  exit_code?: number | null;
  attempt_count?: number | null;
  queue_name?: 'normal' | 'urgent';
  depends_on?: number[];
  dependency_count?: number;
  has_dependencies?: boolean;
  attempt_logs?: TaskLogEntry[];
}

interface TaskLogEntry {
  attempt: number;
  path: string;
  size_bytes: number;
  modified_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  status?: Task['status'] | 'retry_scheduled' | 'preempted' | 'interrupted_requeued' | null;
  exit_code?: number | null;
  is_current?: boolean;
}

interface TaskLogPayload {
  task?: BackendTask;
  content?: string;
  logs?: TaskLogEntry[];
  log?: TaskLogEntry | null;
  selected_attempt?: number | null;
}

interface BackendGPU {
  index: number;
  name: string;
  memory_used_mb: number;
  memory_total_mb: number;
  memory_free_mb?: number;
  utilization_gpu: number;
  is_idle: boolean;
  physically_idle?: boolean;
  globally_enabled: boolean;
  scheduler_occupied?: boolean;
  has_processes?: boolean;
  cooldown_until?: string;
  cooldown_remaining_seconds?: number;
  cooldown_reason?: string;
}

interface Profile {
  id: number;
  name: string;
  cwd?: string | null;
  env?: Record<string, string>;
  shell_setup?: string | null;
  notes?: string | null;
}

interface SchedulerSettings {
  poll_interval_seconds: number;
  gpu_idle_required_checks: number;
  effective_wait_seconds: number;
  auto_restore_idle_gpu_seconds: number | null;
  auto_restore_idle_gpu_enabled?: boolean;
  auto_retry_enabled: boolean;
  auto_retry_max_retries: number;
  auto_retry_delay_seconds: number;
  external_kill_gpu_cooldown_seconds: number;
}

type ActivityLogLevel = 'info' | 'success' | 'warning' | 'error';

interface ActivityLogEntry {
  id: number;
  created_at: string;
  level: ActivityLogLevel;
  source: string;
  action: string;
  entity_type?: string | null;
  entity_id?: number | null;
  title: string;
  detail?: string | null;
  metadata?: Record<string, unknown>;
}

type HistorySortKey = 'finished_at' | 'started_at';

interface DiscoveryItem {
  display_name: string;
  suggested_profile?: {
    name?: string;
    cwd?: string | null;
    env?: Record<string, string>;
    shell_setup?: string | null;
    notes?: string | null;
  };
}

type DiscoveryState = {
  conda_envs: DiscoveryItem[];
  venvs: DiscoveryItem[];
};

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail || response.statusText);
  }
  return response.json().catch(() => ({} as T));
}

function formatTime(value?: string | null) {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatBytes(value?: number | null) {
  if (!value || value <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size >= 10 || unitIndex === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unitIndex]}`;
}

function envToText(env?: Record<string, string>) {
  return Object.entries(env || {}).map(([key, value]) => `${key}=${value}`).join('\n');
}

function parseEnv(text: string) {
  const env: Record<string, string> = {};
  text.split('\n').forEach((rawLine) => {
    const line = rawLine.trim();
    if (!line) return;
    const index = line.indexOf('=');
    if (index <= 0) {
      throw new Error(`环境变量格式错误: ${line}`);
    }
    env[line.slice(0, index).trim()] = line.slice(index + 1);
  });
  return env;
}

function toDatetimeLocalValue(value?: string) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const offsetMs = date.getTimezoneOffset() * 60 * 1000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
}

function formatScheduleTime(value?: string) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function taskStatusLabel(status: Task['status']) {
  switch (status) {
    case 'pending': return '排队中';
    case 'running': return '运行中';
    case 'succeeded': return '成功';
    case 'failed': return '失败';
    case 'cancelled': return '取消';
    case 'interrupted': return '中断';
    default: return status;
  }
}

function activityLevelLabel(level: string) {
  switch (level) {
    case 'success': return '成功';
    case 'warning': return '警告';
    case 'error': return '错误';
    default: return '信息';
  }
}

function activityLevelStyle(level: string) {
  switch (level) {
    case 'success': return 'bg-emerald-50 text-emerald-700 border-emerald-100';
    case 'warning': return 'bg-amber-50 text-amber-700 border-amber-100';
    case 'error': return 'bg-rose-50 text-rose-700 border-rose-100';
    default: return 'bg-blue-50 text-blue-700 border-blue-100';
  }
}

function activityEntityLabel(entityType?: string | null) {
  switch (entityType) {
    case 'task': return '任务';
    case 'queue': return '队列';
    case 'gpu': return 'GPU';
    case 'profile': return '环境';
    case 'scheduler': return '调度器';
    default: return '系统';
  }
}

function mapTask(task: BackendTask): Task {
  return {
    id: String(task.id),
    name: task.name || `任务 ${task.id}`,
    status: task.status === 'queued' ? 'pending' : task.status,
    command: task.command,
    workingDir: task.cwd || undefined,
    gpu: task.assigned_gpu ?? task.requested_gpu ?? undefined,
    profile: task.profile_name || undefined,
    startedAt: formatTime(task.started_at),
    endedAt: formatTime(task.finished_at),
    exitCode: task.exit_code ?? undefined,
    isUrgent: task.queue_name === 'urgent',
    attempts: task.attempt_count || undefined,
    notes: task.notes || undefined,
    env: task.env || {},
    requestedGpu: task.requested_gpu ?? null,
    gpuMemoryBudgetMb: task.gpu_memory_budget_mb ?? null,
    profileId: task.profile_id ?? null,
    queueName: task.queue_name || 'normal',
    dependsOn: task.depends_on || [],
    dependencyCount: task.dependency_count ?? (task.depends_on?.length || 0),
    hasDependencies: task.has_dependencies ?? ((task.depends_on?.length || 0) > 0),
    attemptLogs: task.attempt_logs || [],
    raw: task,
  };
}

function mapGpu(gpu: BackendGPU): GPUStatus {
  const isCoolingDown = Boolean(gpu.cooldown_until || (gpu.cooldown_remaining_seconds ?? 0) > 0);
  return {
    id: gpu.index,
    name: gpu.name,
    memoryUsed: gpu.memory_used_mb || 0,
    memoryTotal: gpu.memory_total_mb || 0,
    memoryFree: gpu.memory_free_mb ?? Math.max(0, (gpu.memory_total_mb || 0) - (gpu.memory_used_mb || 0)),
    utilization: gpu.utilization_gpu || 0,
    isBusy: isCoolingDown || !(gpu.physically_idle ?? gpu.is_idle) || Boolean(gpu.scheduler_occupied || gpu.has_processes),
    isCoolingDown,
    cooldownRemainingSeconds: gpu.cooldown_remaining_seconds,
  };
}

type AppTab = 'dashboard' | 'queue' | 'history' | 'activity' | 'nvitop' | 'settings';

export default function App() {
  const [activeTab, setActiveTab] = useState<AppTab>('dashboard');
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [isPaused, setIsPaused] = useState(false);
  const [showNewTask, setShowNewTask] = useState(false);
  const [taskDraft, setTaskDraft] = useState<Task | null>(null);
  const [enabledGpus, setEnabledGpus] = useState<number[]>([]);
  const [appliedEnabledGpus, setAppliedEnabledGpus] = useState<number[]>([]);
  const [gpuSchedule, setGpuSchedule] = useState<Record<string, GpuScheduleEntry>>({});
  const [gpuScheduleDrafts, setGpuScheduleDrafts] = useState<Record<string, string>>({});
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [activityLogs, setActivityLogs] = useState<ActivityLogEntry[]>([]);
  const [activityLevelFilter, setActivityLevelFilter] = useState('all');
  const [activityEntityFilter, setActivityEntityFilter] = useState('all');
  const [activitySearch, setActivitySearch] = useState('');
  const [expandedActivityLogId, setExpandedActivityLogId] = useState<number | null>(null);
  const [historySort, setHistorySort] = useState<HistorySortKey>('finished_at');
  const [isConsoleFullScreen, setIsConsoleFullScreen] = useState(false);
  const [isNvitopFullScreen, setIsNvitopFullScreen] = useState(false);
  const [gpus, setGpus] = useState<GPUStatus[]>([]);
  const [isEditingTask, setIsEditingTask] = useState(false);
  const [dragState, setDragState] = useState<{ id: string, overId: string | null, position: 'before' | 'after' } | null>(null);
  const [isBatchDeleteMode, setIsBatchDeleteMode] = useState(false);
  const [selectedForDelete, setSelectedForDelete] = useState<Set<string>>(new Set());
  const [markedTaskIds, setMarkedTaskIds] = useState<Set<string>>(() => {
    const ids = new Set<string>();
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key?.startsWith('task-mark-') && localStorage.getItem(key) === 'true') {
        ids.add(key.replace('task-mark-', ''));
      }
    }
    return ids;
  });

  const toggleTaskMark = useCallback((taskId: string) => {
    setMarkedTaskIds(prev => {
      const next = new Set(prev);
      if (next.has(taskId)) {
        next.delete(taskId);
        localStorage.removeItem(`task-mark-${taskId}`);
      } else {
        next.add(taskId);
        localStorage.setItem(`task-mark-${taskId}`, 'true');
      }
      return next;
    });
  }, []);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [schedulerSettings, setSchedulerSettings] = useState<SchedulerSettings | null>(null);
  const [schedulerSettingsDraft, setSchedulerSettingsDraft] = useState<Partial<SchedulerSettings>>({});
  const [autoRestoreMinutesDraft, setAutoRestoreMinutesDraft] = useState<string | null>(null);
  const [discovery, setDiscovery] = useState<DiscoveryState>({ conda_envs: [], venvs: [] });
  const [selectedDiscoveryId, setSelectedDiscoveryId] = useState('');
  const [managedProfileId, setManagedProfileId] = useState('');
  const [profileDraft, setProfileDraft] = useState<Profile | null>(null);
  const [serverIp, setServerIp] = useState('读取中...');
  const [message, setMessage] = useState('');
  const followRunningTaskRef = useRef(true);

  const runningTasks = useMemo(() => tasks.filter(t => t.status === 'running'), [tasks]);
  const historyTasks = useMemo(() => tasks.filter(t => t.status !== 'running' && t.status !== 'pending'), [tasks]);
  const urgentQueueTasks = useMemo(() => tasks.filter(t => t.isUrgent && (t.status === 'pending' || t.status === 'running')), [tasks]);
  const standardQueueTasks = useMemo(() => tasks.filter(t => !t.isUrgent && (t.status === 'pending' || t.status === 'running')), [tasks]);
  const selectedTask = useMemo(() => tasks.find(t => t.id === selectedTaskId) || null, [tasks, selectedTaskId]);
  const enabledGpuSet = useMemo(() => new Set(enabledGpus), [enabledGpus]);
  const visibleGpus = useMemo(() => gpus.filter(gpu => enabledGpuSet.has(gpu.id)), [gpus, enabledGpuSet]);
  const queueDepth = urgentQueueTasks.filter(t => t.status === 'pending').length + standardQueueTasks.filter(t => t.status === 'pending').length;
  const hasWaitingUrgentTask = urgentQueueTasks.some(task => task.status === 'pending');
  const failedCount = historyTasks.filter(t => t.status === 'failed' || t.status === 'interrupted').length;
  const failureRate = historyTasks.length ? `${((failedCount / historyTasks.length) * 100).toFixed(1)}%` : '0%';
  const filteredHistoryTasks = useMemo(() => historyTasks.filter(t => {
    if (statusFilter === 'all') return true;
    if (statusFilter === 'marked') return markedTaskIds.has(t.id);
    return t.status === statusFilter;
  }), [historyTasks, markedTaskIds, statusFilter]);
  const canUseBatchDelete = activeTab === 'queue' || activeTab === 'history';
  const dependencyCandidates = useMemo<DependencyCandidate[]>(() => {
    const currentTaskId = taskDraft?.id;
    const selectedIds = new Set((taskDraft?.dependsOn || []).map(String));
    const knownCandidates: DependencyCandidate[] = tasks.filter(task =>
      task.id !== currentTaskId &&
      (task.status === 'pending' || task.status === 'running' || selectedIds.has(task.id))
    );
    const knownIds = new Set(knownCandidates.map(task => task.id));
    const missingCandidates: DependencyCandidate[] = Array.from(selectedIds)
      .filter(id => id !== currentTaskId && !knownIds.has(id))
      .map(id => ({
        id,
        name: '当前列表外任务',
        status: 'pending',
        isMissingDependency: true,
    }));
    return [...knownCandidates, ...missingCandidates];
  }, [taskDraft, tasks]);
  const isMetadataOnlyTaskEdit = Boolean(isEditingTask && taskDraft && taskDraft.status !== 'pending');
  const autoRestoreIdleGpuSeconds = schedulerSettingsDraft.auto_restore_idle_gpu_seconds !== undefined
    ? schedulerSettingsDraft.auto_restore_idle_gpu_seconds
    : schedulerSettings
      ? schedulerSettings.auto_restore_idle_gpu_seconds
      : 300;
  const autoRestoreIdleGpuEnabled = typeof autoRestoreIdleGpuSeconds === 'number' && autoRestoreIdleGpuSeconds > 0;
  const autoRestoreIdleGpuMinutes = autoRestoreMinutesDraft ?? (
    autoRestoreIdleGpuEnabled && typeof autoRestoreIdleGpuSeconds === 'number'
      ? String(Number((autoRestoreIdleGpuSeconds / 60).toFixed(2)))
      : ''
  );
  const autoRetryEnabled = schedulerSettingsDraft.auto_retry_enabled
    ?? schedulerSettings?.auto_retry_enabled
    ?? false;
  const autoRetryMaxRetries = schedulerSettingsDraft.auto_retry_max_retries
    ?? schedulerSettings?.auto_retry_max_retries
    ?? (autoRetryEnabled ? 1 : 0);
  const autoRetryDelaySeconds = schedulerSettingsDraft.auto_retry_delay_seconds
    ?? schedulerSettings?.auto_retry_delay_seconds
    ?? 5;
  const externalKillGpuCooldownSeconds = schedulerSettingsDraft.external_kill_gpu_cooldown_seconds
    ?? schedulerSettings?.external_kill_gpu_cooldown_seconds
    ?? 300;
  const hasSchedulerSettingsDraft = Object.keys(schedulerSettingsDraft).length > 0 || autoRestoreMinutesDraft !== null;

  useEffect(() => {
    setIsBatchDeleteMode(false);
    setSelectedForDelete(new Set());
  }, [activeTab]);

  const refreshAll = useCallback(async () => {
    const taskParams = new URLSearchParams({ history_sort: historySort });
    const [taskPayload, gpuPayload, settingsPayload, profilePayload, serverPayload, schedulerSettingsPayload] = await Promise.all([
      api<{ queued: BackendTask[]; urgent_queued: BackendTask[]; running: BackendTask[]; history: BackendTask[]; queue_paused: boolean }>(`/api/tasks?${taskParams.toString()}`),
      api<{ gpus: BackendGPU[] }>('/api/gpus'),
      api<{ allowed_gpu_ids: number[] | null; gpu_schedule?: Record<string, GpuScheduleEntry> }>('/api/settings'),
      api<{ profiles: Profile[] }>('/api/profiles'),
      api<{ server_ip?: string; server_name?: string }>('/api/server'),
      api<SchedulerSettings>('/api/scheduler/settings').catch(() => null),
    ]);
    const nextGpus = (gpuPayload.gpus || []).map(mapGpu);
    const nextTasks = [
      ...(taskPayload.running || []),
      ...(taskPayload.urgent_queued || []),
      ...(taskPayload.queued || []),
      ...(taskPayload.history || []),
    ].map(mapTask);
    setGpus(nextGpus);
    setTasks(nextTasks);
    setProfiles(profilePayload.profiles || []);
    if (schedulerSettingsPayload) {
      setSchedulerSettings(schedulerSettingsPayload);
    }
    setIsPaused(Boolean(taskPayload.queue_paused));
    const nextEnabledGpus = settingsPayload.allowed_gpu_ids ?? nextGpus.map(gpu => gpu.id);
    setEnabledGpus(nextEnabledGpus);
    setAppliedEnabledGpus(nextEnabledGpus);
    setGpuSchedule(settingsPayload.gpu_schedule || {});
    setServerIp(serverPayload.server_ip || serverPayload.server_name || 'unknown');
    setSelectedTaskId(prev => {
      const runningTask = nextTasks.find(task => task.status === 'running') || null;
      const previousTask = prev ? nextTasks.find(task => task.id === prev) || null : null;
      if (followRunningTaskRef.current) {
        if (previousTask?.status === 'running') return prev;
        return runningTask?.id || previousTask?.id || null;
      }
      if (previousTask) return previousTask.id;
      followRunningTaskRef.current = true;
      return runningTask?.id || null;
    });
  }, [historySort]);

  const loadActivityLogs = useCallback(async () => {
    const params = new URLSearchParams({ limit: '300' });
    if (activityLevelFilter !== 'all') {
      params.set('level', activityLevelFilter);
    }
    if (activityEntityFilter !== 'all') {
      params.set('entity_type', activityEntityFilter);
    }
    const query = activitySearch.trim();
    if (query) {
      params.set('query', query);
    }
    const payload = await api<{ logs: ActivityLogEntry[] }>(`/api/activity/logs?${params.toString()}`);
    setActivityLogs(payload.logs || []);
  }, [activityEntityFilter, activityLevelFilter, activitySearch]);

  useEffect(() => {
    refreshAll().catch(error => setMessage(error.message));
    const source = new EventSource('/api/events');
    source.addEventListener('update', () => {
      refreshAll().catch(error => setMessage(error.message));
    });
    source.onerror = () => {
      source.close();
    };
    const timer = window.setInterval(() => {
      refreshAll().catch(() => {});
    }, 5000);
    return () => {
      source.close();
      window.clearInterval(timer);
    };
  }, [refreshAll]);

  useEffect(() => {
    if (activeTab !== 'activity') return;
    loadActivityLogs().catch(error => setMessage(error.message));
    const timer = window.setInterval(() => {
      loadActivityLogs().catch(() => {});
    }, 5000);
    return () => {
      window.clearInterval(timer);
    };
  }, [activeTab, loadActivityLogs]);

  const toggleQueue = async () => {
    if (isPaused) {
      await api('/api/queue/resume', { method: 'POST' });
      await refreshAll();
      return;
    }
    const stopRunning = runningTasks.length > 0
      ? window.confirm(
        `当前有 ${runningTasks.length} 个运行中任务。\n\n确定：暂停调度，并停止运行中任务后放回队首。\n取消：只暂停新任务调度，运行中任务继续执行。`
      )
      : false;
    await api('/api/queue/pause', {
      method: 'POST',
      body: JSON.stringify({ stop_running: stopRunning }),
    });
    await refreshAll();
  };

  const openNewTaskModal = () => {
    setTaskDraft(null);
    setShowNewTask(true);
  };

  const closeNewTaskModal = () => {
    setShowNewTask(false);
    setTaskDraft(null);
    setIsEditingTask(false);
  };

  const openLogTask = (taskId: string) => {
    followRunningTaskRef.current = false;
    setSelectedTaskId(taskId);
    setActiveTab('dashboard');
  };

  const toggleGpu = (id: number) => {
    setEnabledGpus(prev =>
      prev.includes(id) ? prev.filter(gid => gid !== id) : [...prev, id]
    );
  };

  const saveSchedulerSettings = async () => {
    try {
      const current = schedulerSettings || {
        poll_interval_seconds: 1,
        gpu_idle_required_checks: 1,
        effective_wait_seconds: 1,
        auto_restore_idle_gpu_seconds: 300,
        auto_retry_enabled: false,
        auto_retry_max_retries: 0,
        auto_retry_delay_seconds: 5,
        external_kill_gpu_cooldown_seconds: 300,
      };
      const merged = {
        ...current,
        ...schedulerSettingsDraft
      };
      const retryEnabled = Boolean(merged.auto_retry_enabled);
      let autoRestoreIdleGpuSecondsForSave = merged.auto_restore_idle_gpu_seconds ?? null;
      if (autoRestoreMinutesDraft !== null && autoRestoreIdleGpuSecondsForSave !== null) {
        const minutesText = autoRestoreMinutesDraft.trim();
        const minutes = Number(minutesText);
        if (!minutesText || !Number.isFinite(minutes) || minutes <= 0) {
          setMessage('GPU 自动恢复等待时间必须大于 0，或关闭此功能');
          return;
        }
        autoRestoreIdleGpuSecondsForSave = minutes * 60;
      }
      
      if (merged.poll_interval_seconds <= 0 || !Number.isFinite(merged.poll_interval_seconds)) {
        setMessage('轮询间隔必须大于 0');
        return;
      }
      if (merged.gpu_idle_required_checks < 1 || !Number.isInteger(merged.gpu_idle_required_checks)) {
        setMessage('GPU 空闲确认次数必须是大于等于 1 的整数');
        return;
      }
      if (
        autoRestoreIdleGpuSecondsForSave !== null &&
        autoRestoreIdleGpuSecondsForSave <= 0
      ) {
        setMessage('GPU 自动恢复等待时间必须大于 0，或关闭此功能');
        return;
      }
      if (
        retryEnabled &&
        (merged.auto_retry_max_retries < 1 || !Number.isInteger(merged.auto_retry_max_retries))
      ) {
        setMessage('自动重试开启时，重试次数必须是大于等于 1 的整数');
        return;
      }
      if (
        merged.auto_retry_delay_seconds < 0 ||
        !Number.isInteger(merged.auto_retry_delay_seconds)
      ) {
        setMessage('自动重试延迟必须是大于等于 0 的整数秒');
        return;
      }
      if (
        merged.external_kill_gpu_cooldown_seconds < 0 ||
        !Number.isFinite(merged.external_kill_gpu_cooldown_seconds)
      ) {
        setMessage('外部 kill 后 GPU 冷却时间必须是大于等于 0 的秒数');
        return;
      }
      
      const res = await api<SchedulerSettings>('/api/scheduler/settings', {
        method: 'PUT',
        body: JSON.stringify({
          poll_interval_seconds: merged.poll_interval_seconds,
          gpu_idle_required_checks: merged.gpu_idle_required_checks,
          auto_restore_idle_gpu_seconds: autoRestoreIdleGpuSecondsForSave,
          auto_retry_enabled: retryEnabled,
          auto_retry_max_retries: retryEnabled ? merged.auto_retry_max_retries : 0,
          auto_retry_delay_seconds: merged.auto_retry_delay_seconds,
          external_kill_gpu_cooldown_seconds: merged.external_kill_gpu_cooldown_seconds,
        }),
      });
      setSchedulerSettings(res);
      setSchedulerSettingsDraft({});
      setAutoRestoreMinutesDraft(null);
      setMessage('调控器设置已更新');
    } catch (error) {
      if (error instanceof Error) {
        setMessage(`保存失败: ${error.message}`);
      } else {
        setMessage('保存失败');
      }
    }
  };

  const applyGpuSettings = async () => {
    const allowed_gpu_ids = enabledGpus.length === gpus.length ? null : enabledGpus;
    const disabledGpuIds = appliedEnabledGpus.filter(id => !enabledGpus.includes(id));
    const runningOnDisabled = runningTasks.filter(task => (
      typeof task.gpu === 'number' && disabledGpuIds.includes(task.gpu)
    ));
    let stop_running_gpu_ids: number[] = [];
    if (runningOnDisabled.length > 0) {
      const summary = runningOnDisabled
        .map(task => `#${task.id} ${task.name} / GPU ${task.gpu}`)
        .join('\n');
      const shouldStop = window.confirm(
        `本次关闭的 GPU 上有 ${runningOnDisabled.length} 个运行中任务。\n\n${summary}\n\n确定：停止这些任务并放回原队列队首。\n取消：只关闭后续调度，不停止当前任务。`
      );
      if (shouldStop) {
        stop_running_gpu_ids = Array.from(new Set(
          runningOnDisabled
            .map(task => task.gpu)
            .filter((gpuId): gpuId is number => typeof gpuId === 'number')
        ));
      }
    }
    await api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ allowed_gpu_ids, stop_running_gpu_ids }),
    });
    await refreshAll();
  };

  const scheduleGpuState = async (gpuId: number, action: GpuScheduleAction) => {
    const value = gpuScheduleDrafts[String(gpuId)];
    if (!value) {
      setMessage('请选择定时时间。');
      return;
    }
    await api(`/api/settings/gpu-schedule/${gpuId}`, {
      method: 'POST',
      body: JSON.stringify({ action, run_at: new Date(value).toISOString() }),
    });
    setGpuScheduleDrafts(prev => ({ ...prev, [String(gpuId)]: '' }));
    await refreshAll();
  };

  const clearGpuSchedule = async (gpuId: number) => {
    await api(`/api/settings/gpu-schedule/${gpuId}`, { method: 'DELETE' });
    await refreshAll();
  };

  const submitTask = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    try {
      const name = String(formData.get('name') || '').trim() || null;
      const notes = String(formData.get('notes') || '').trim() || null;
      if (isMetadataOnlyTaskEdit && taskDraft) {
        await api(`/api/tasks/${taskDraft.id}/metadata`, {
          method: 'PATCH',
          body: JSON.stringify({ name, notes }),
        });
        setMessage(`任务 #${taskDraft.id} 记录信息已更新。`);
        closeNewTaskModal();
        await refreshAll();
        return;
      }

      const dependsOnRaw = formData.getAll('depends_on');
      const depends_on = dependsOnRaw.map(Number).filter(n => !isNaN(n));
      const payload = {
        name,
        command: String(formData.get('command') || ''),
        cwd: String(formData.get('cwd') || '').trim() || null,
        notes,
        env: parseEnv(String(formData.get('env') || '')),
        is_urgent: Boolean(formData.get('is_urgent')),
        requested_gpu: formData.get('requested_gpu') ? Number(formData.get('requested_gpu')) : null,
        gpu_memory_budget_mb: formData.get('gpu_memory_budget_gb') ? Math.round(Number(formData.get('gpu_memory_budget_gb')) * 1024) : null,
        profile_id: formData.get('profile_id') ? Number(formData.get('profile_id')) : null,
        depends_on,
      };

      if (isEditingTask && taskDraft) {
        await api(`/api/tasks/${taskDraft.id}`, {
          method: 'PUT',
          body: JSON.stringify(payload),
        });
        setMessage(`任务 #${taskDraft.id} 更新成功。`);
      } else {
        await api('/api/tasks', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        setMessage(taskDraft ? `已根据任务 #${taskDraft.id} 创建新任务。` : '任务已加入队列。');
      }
      closeNewTaskModal();
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : (isEditingTask ? '任务更新失败' : '任务提交失败'));
    }
  };

  const cancelTask = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/cancel`, { method: 'POST' });
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '任务取消失败');
    }
  };

  const preemptTask = async (task: Task) => {
    if (!hasWaitingUrgentTask) {
      setMessage('当前没有等待中的紧急任务，请先把任务加入紧急队列。');
      return;
    }
    if (!window.confirm(`确认抢占任务 #${task.id} 吗？\n\n当前任务会被停止并放回普通队列队首，紧急队列会优先获得空出的 GPU。`)) {
      return;
    }
    try {
      await api(`/api/tasks/${task.id}/preempt`, { method: 'POST' });
      setMessage(`任务 #${task.id} 正在被抢占，紧急队列将优先调度。`);
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '任务抢占失败');
    }
  };

  const deleteTask = async (taskId: string) => {
    if (!window.confirm(`确认删除任务 #${taskId} 吗？历史记录会同时删除对应日志文件。`)) {
      return;
    }
    try {
      await api(`/api/tasks/${taskId}`, { method: 'DELETE' });
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '任务删除失败');
    }
  };

  const handleDragStart = (e: React.DragEvent, taskId: string) => {
    e.dataTransfer.setData('text/plain', taskId);
    e.dataTransfer.effectAllowed = 'move';
    setTimeout(() => {
      setDragState({ id: taskId, overId: null, position: 'before' });
    }, 0);
  };

  const handleDragOver = (e: React.DragEvent, taskId: string, containerId: string) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const rect = e.currentTarget.getBoundingClientRect();
    const isTopHalf = e.clientY < rect.top + rect.height / 2;
    setDragState(prev => prev ? { ...prev, overId: taskId, position: isTopHalf ? 'before' : 'after' } : null);
  };

  const handleDrop = async (e: React.DragEvent, targetQueue: 'urgent' | 'normal', isDropZone = false) => {
    e.preventDefault();
    if (!dragState) return;
    const { id, overId, position } = dragState;
    setDragState(null);

    const task = tasks.find(t => t.id === id);
    if (!task) return;

    try {
      if ((task.isUrgent && targetQueue === 'normal') || (!task.isUrgent && targetQueue === 'urgent')) {
         await api(`/api/tasks/${task.id}`, {
             method: 'PUT',
             body: JSON.stringify({
                 name: task.raw?.name ?? task.name ?? null,
                 command: task.raw?.command ?? task.command,
                 cwd: task.raw?.cwd ?? task.workingDir ?? null,
                 env: task.raw?.env ?? task.env ?? {},
                 notes: task.raw?.notes ?? task.notes ?? null,
                 is_urgent: targetQueue === 'urgent',
                 requested_gpu: task.raw?.requested_gpu ?? task.requestedGpu ?? null,
                 gpu_memory_budget_mb: task.raw?.gpu_memory_budget_mb ?? task.gpuMemoryBudgetMb ?? null,
                 profile_id: task.raw?.profile_id ?? task.profileId ?? null
             })
         });
      }

      const targetPendingTaskIdsStr = tasks
        .filter(t => t.status === 'pending')
        .filter(t => {
           if (t.id === id) return false;
           return targetQueue === 'urgent' ? t.isUrgent : !t.isUrgent;
        })
        .map(t => t.id);

      const newOrderIds = [...targetPendingTaskIdsStr];

      if (!isDropZone && overId && overId !== `empty-${targetQueue}`) {
         const overIndex = targetPendingTaskIdsStr.indexOf(overId);
         if (overIndex !== -1) {
            newOrderIds.splice(position === 'before' ? overIndex : overIndex + 1, 0, id);
         } else {
            newOrderIds.push(id);
         }
      } else {
         newOrderIds.push(id);
      }

      await api('/api/tasks/reorder', {
         method: 'POST',
         body: JSON.stringify({
             task_ids: newOrderIds.map(numId => parseInt(numId)),
             queue_name: targetQueue
         })
      });

      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '拖拽重新排队失败');
      await refreshAll();
    }
  };

  const requeueTask = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/requeue`, { method: 'POST' });
      setMessage(`任务 #${taskId} 已重新入队。`);
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '任务重新入队失败');
    }
  };

  const toggleTaskForBatchDelete = (task: Task) => {
    if (task.status === 'running') {
      setMessage('运行中的任务不能直接删除，请先取消任务。');
      return;
    }
    setSelectedForDelete(prev => {
      const next = new Set(prev);
      if (next.has(task.id)) {
        next.delete(task.id);
      } else {
        next.add(task.id);
      }
      return next;
    });
  };

  const batchDeleteTasks = async () => {
    if (selectedForDelete.size === 0) {
      setMessage('请先选择要删除的任务。');
      return;
    }
    const tasksToDelete = Array.from(selectedForDelete).filter(taskId => {
      const task = tasks.find(item => item.id === taskId);
      return task && task.status !== 'running';
    });
    if (tasksToDelete.length === 0) {
      setMessage('没有可删除的任务。运行中的任务需要先取消。');
      return;
    }
    const taskCount = tasksToDelete.length;
    if (!window.confirm(`确认批量删除已选中的 ${taskCount} 个任务吗？历史记录会同时删除对应日志文件。`)) {
      return;
    }
    try {
      await Promise.all(tasksToDelete.map(taskId =>
        api(`/api/tasks/${taskId}`, { method: 'DELETE' })
      ));
      setSelectedForDelete(new Set());
      setIsBatchDeleteMode(false);
      setMessage(`已成功批量删除 ${taskCount} 个任务。`);
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '批量删除任务失败');
      await refreshAll();
    }
  };

  const duplicateTask = (task: Task) => {
    setTaskDraft(task);
    setIsEditingTask(false);
    setShowNewTask(true);
    setMessage(`正在基于任务 #${task.id} 复用新建。`);
  };

  const editTask = (task: Task) => {
    setTaskDraft(task);
    setIsEditingTask(true);
    setShowNewTask(true);
    setMessage(task.status === 'pending' ? `正在重新编辑任务 #${task.id}。` : `正在编辑任务 #${task.id} 的记录信息。`);
  };

  const copyActivityLog = async (log: ActivityLogEntry) => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(log, null, 2));
      setMessage(`日志 #${log.id} 已复制。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '日志复制失败');
    }
  };

  const saveProfile = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    try {
      const profileId = String(formData.get('profile_id') || '');
      const payload = {
        name: String(formData.get('name') || '').trim(),
        cwd: null,
        env: parseEnv(String(formData.get('env') || '')),
        shell_setup: String(formData.get('shell_setup') || '').trim() || null,
        notes: String(formData.get('notes') || '').trim() || null,
      };
      if (!payload.name) {
        throw new Error('环境模板名称不能为空');
      }
      await api(profileId ? `/api/profiles/${profileId}` : '/api/profiles', {
        method: profileId ? 'PUT' : 'POST',
        body: JSON.stringify(payload),
      });
      setMessage(profileId ? '环境模板已更新。' : '环境模板已创建。');
      setManagedProfileId('');
      setProfileDraft(null);
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '环境模板保存失败');
    }
  };

  const scanProfiles = async () => {
    const payload = await api<DiscoveryState>('/api/profiles/discovery');
    setDiscovery({ conda_envs: payload.conda_envs || [], venvs: payload.venvs || [] });
  };

  const importDiscovery = async () => {
    const allItems = [...discovery.conda_envs, ...discovery.venvs];
    const item = allItems[Number(selectedDiscoveryId)];
    const payload = item?.suggested_profile;
    if (!payload) return;
    await api('/api/profiles/import', {
      method: 'POST',
      body: JSON.stringify({
        name: payload.name,
        cwd: payload.cwd || null,
        env: payload.env || {},
        shell_setup: payload.shell_setup || null,
        notes: payload.notes || null,
      }),
    });
    await refreshAll();
  };

  return (
    <div className="flex h-screen overflow-hidden text-sm bg-slate-50 text-slate-900">
      {/* Sidebar */}
      <aside className="w-56 bg-white border-r border-slate-200 flex flex-col">
        <div className="p-4 border-b border-slate-100 flex items-center gap-3 shrink-0">
          <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center text-white shadow-sm shadow-blue-600/20">
            <Activity className="w-5 h-5" />
          </div>
          <div>
            <h1 className="font-bold text-slate-900 tracking-tight text-sm leading-tight">GPU Flow</h1>
            <p className="text-[9px] text-slate-400 uppercase tracking-widest font-bold">Chronos Engine</p>
          </div>
        </div>

        <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto custom-scrollbar">
          <NavItem
            active={activeTab === 'dashboard'}
            onClick={() => setActiveTab('dashboard')}
            icon={<Monitor className="w-5 h-5" />}
            label="控制台概览"
          />
          <NavItem
            active={activeTab === 'queue'}
            onClick={() => setActiveTab('queue')}
            icon={<Layers className="w-5 h-5" />}
            label="任务队列"
            count={queueDepth}
          />
          <NavItem
            active={activeTab === 'history'}
            onClick={() => setActiveTab('history')}
            icon={<History className="w-5 h-5" />}
            label="历史记录"
          />
          <NavItem
            active={activeTab === 'nvitop'}
            onClick={() => setActiveTab('nvitop')}
            icon={<Terminal className="w-5 h-5" />}
            label="GPU 监控"
          />
          <NavItem
            active={activeTab === 'settings'}
            onClick={() => setActiveTab('settings')}
            icon={<Settings className="w-5 h-5" />}
            label="资源与环境"
          />
          <NavItem
            active={activeTab === 'activity'}
            onClick={() => setActiveTab('activity')}
            icon={<FileText className="w-5 h-5" />}
            label="系统日志"
          />

          <div className="mt-8 pt-6 border-t border-slate-100">
            <p className="px-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-3">活跃流水线</p>
            <div className="space-y-2">
              <div className="flex items-center gap-3 px-3 py-1.5 text-sm text-slate-600">
                <div className="w-2 h-2 rounded-full bg-emerald-500 shadow-sm shadow-emerald-500/20" />
                <span className="font-medium">数据主同步</span>
              </div>
              <div className="flex items-center gap-3 px-3 py-1.5 text-sm text-slate-600">
                <div className="w-2 h-2 rounded-full bg-blue-500 shadow-sm shadow-blue-500/20" />
                <span className="font-medium">日志清理</span>
              </div>
            </div>
          </div>
        </nav>

        <div className="p-4 bg-slate-50/50 border-t border-slate-100">
          <div className="bg-white p-4 rounded-xl border border-slate-200 shadow-sm space-y-3">
            <div className="flex items-center gap-2 text-slate-500">
              <Server className="w-4 h-4" />
              <span className="text-xs font-bold truncate tracking-tight uppercase">高性能计算服务器</span>
            </div>
            <div className="space-y-1.5 pt-1 border-t border-slate-50">
              <div className="flex justify-between text-[10px] font-bold text-slate-400 uppercase tracking-tighter">
                <span>核心利用率</span>
                <span>42%</span>
              </div>
              <div className="w-full bg-slate-100 h-1.5 rounded-full overflow-hidden">
                <div className="bg-blue-500 h-full w-[42%] transition-all"></div>
              </div>
            </div>
            <div className="flex items-center justify-between pt-1">
              <div className="flex items-center gap-1.5">
                <div className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
                <span className="text-[10px] text-slate-500 font-medium tracking-tight">{serverIp}</span>
              </div>
              <button className="text-slate-400 hover:text-slate-600 transition-colors">
                <RefreshCcw className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 flex flex-col overflow-hidden relative">
        {/* Top Header */}
        <header className="h-16 bg-white border-b border-slate-200 px-6 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-slate-50 text-slate-400 rounded-lg border border-slate-100">
	              {activeTab === 'dashboard' && <Monitor className="w-4 h-4" />}
	              {activeTab === 'queue' && <Layers className="w-4 h-4" />}
	              {activeTab === 'history' && <History className="w-4 h-4" />}
	              {activeTab === 'activity' && <FileText className="w-4 h-4" />}
	              {activeTab === 'nvitop' && <Terminal className="w-4 h-4" />}
	              {activeTab === 'settings' && <Settings className="w-4 h-4" />}
            </div>
            <h2 className="text-lg font-bold text-slate-900 tracking-tight">
	              {activeTab === 'dashboard' && '控制台概览'}
	              {activeTab === 'queue' && '任务队列'}
	              {activeTab === 'history' && '历史记录'}
	              {activeTab === 'activity' && '系统日志'}
	              {activeTab === 'nvitop' && 'GPU 监控'}
	              {activeTab === 'settings' && '资源与环境'}
            </h2>
          </div>

          <div className="flex items-center gap-2.5">
            {isBatchDeleteMode && canUseBatchDelete ? (
              <>
                <button
                  onClick={() => { setIsBatchDeleteMode(false); setSelectedForDelete(new Set()); }}
                  className="flex items-center gap-2 px-3 py-2 bg-slate-100 text-slate-600 border border-slate-200 hover:bg-slate-200 rounded-lg text-xs font-bold transition-all"
                >
                  取消
                </button>
                <button
                  onClick={batchDeleteTasks}
                  disabled={selectedForDelete.size === 0}
                  className={`flex items-center gap-2 px-3 py-2 border rounded-lg text-xs font-bold transition-all shadow-sm ${
                    selectedForDelete.size === 0
                      ? 'bg-rose-100 text-rose-300 border-rose-100 cursor-not-allowed'
                      : 'bg-rose-500 text-white border-rose-600 hover:bg-rose-600 shadow-md'
                  }`}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  确认删除 ({selectedForDelete.size})
                </button>
              </>
            ) : (
              <>
                {canUseBatchDelete && (
                  <button
                    onClick={() => { setIsBatchDeleteMode(true); setSelectedForDelete(new Set()); }}
                    className="flex items-center gap-2 px-3 py-2 bg-rose-50 text-rose-600 border border-rose-200 hover:bg-rose-100 hover:border-rose-300 rounded-lg text-xs font-bold transition-all shadow-sm"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                    批量删除
                  </button>
                )}
                <button
                  onClick={toggleQueue}
                  className={`flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-semibold transition-all ${
                    isPaused
                    ? 'bg-slate-900 text-white hover:bg-slate-800 shadow-md'
                    : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border border-slate-200'
                  }`}
                >
                  {isPaused ? <Play className="w-3.5 h-3.5 fill-current" /> : <Pause className="w-3.5 h-3.5 fill-current" />}
                  {isPaused ? '恢复调度' : '暂停调度'}
                </button>
                <button
                  onClick={openNewTaskModal}
                  className="flex items-center gap-2 px-5 py-2 bg-blue-600 text-white rounded-lg text-xs font-bold shadow-md shadow-blue-600/20 hover:bg-blue-700 active:scale-95 transition-all"
                >
                  <Plus className="w-3.5 h-3.5" />
                  新建任务
                </button>
              </>
            )}
          </div>
        </header>

        {/* Dynamic Content */}
        <div className="flex-1 overflow-y-auto p-6 custom-scrollbar space-y-6">
          {message && (
            <div className="bg-white border border-slate-200 rounded-xl px-4 py-3 text-xs text-slate-600 font-medium shadow-sm">
              {message}
            </div>
          )}
          <AnimatePresence mode="wait">
            {activeTab === 'dashboard' && (
              <motion.div
                key="dashboard"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="space-y-6"
              >
                {/* Stats Cards Row (derived from design) */}
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                  <StatCard label="总运行次数" value={String(tasks.length)} type="neutral" />
                  <StatCard label="活跃任务" value={String(runningTasks.length)} type="blue" />
                  <StatCard label="队列深度" value={String(queueDepth)} type="amber" />
                  <StatCard label="失败率" value={failureRate} type="rose" />
                </div>

                {/* GPU Nodes Section */}
                <div className="space-y-3">
                  <div className="flex items-center justify-between px-1">
                    <h3 className="text-xs font-bold uppercase tracking-widest text-slate-500 flex items-center gap-2">
                       <Server className="w-4 h-4" />
                       GPU 节点状态
                    </h3>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {visibleGpus.map(gpu => (
                      <GPUCard key={gpu.id} gpu={gpu} />
                    ))}
                  </div>
                </div>

                {/* Main Split: Workloads & Console */}
                <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
                  <div className="lg:col-span-12 space-y-3">
                    <div className="bg-white rounded-xl border border-slate-200 shadow-sm flex flex-col h-full overflow-hidden">
                      <div className="p-4 border-b border-slate-100 flex items-center justify-between bg-slate-50/50">
                        <div className="flex items-center gap-2">
                           <Activity className="w-4 h-4 text-blue-600" />
                           <span className="font-bold text-slate-800 text-sm">正在运行的工作负载</span>
                        </div>
                        <div className="flex gap-2 text-[10px] font-bold uppercase tracking-widest">
                          <span className="px-2 py-1 bg-white border border-slate-200 rounded text-slate-400">实时</span>
                        </div>
                      </div>
                      <div className="flex flex-col gap-2.5 p-4">
                        {runningTasks.map(task => (
                          <TaskCardInner
                            key={task.id}
                            task={task}
                            isSelected={selectedTaskId === task.id}
                            onSelect={() => {
                              followRunningTaskRef.current = true;
                              setSelectedTaskId(task.id);
                            }}
                            onCancel={() => cancelTask(task.id)}
                            onPreempt={() => preemptTask(task)}
                            canPreempt={hasWaitingUrgentTask}
                            onDuplicate={() => duplicateTask(task)}
                            onEdit={() => editTask(task)}
                            isMarked={markedTaskIds.has(task.id)}
                            toggleMark={(e) => { e.stopPropagation(); toggleTaskMark(task.id); }}
                          />
                        ))}
                      </div>
                    </div>
                  </div>

                  <section className={isConsoleFullScreen ? "fixed inset-0 z-50 bg-slate-100 p-6 flex flex-col space-y-4" : "lg:col-span-12 space-y-3"}>
                    <div className="flex items-center justify-between px-1 shrink-0">
                      <h3 className="text-[10px] font-bold uppercase tracking-widest text-slate-400 flex items-center gap-2">
                        <Terminal className="w-3.5 h-3.5" />
                        集成控制台输出
                      </h3>
                      <div className="flex gap-2">
                        <button
                          onClick={() => setIsConsoleFullScreen(!isConsoleFullScreen)}
                          className="p-1.5 bg-white border border-slate-200 rounded text-slate-500 hover:text-blue-600 hover:bg-blue-50 transition-colors"
                          title={isConsoleFullScreen ? "退出全屏" : "全屏显示"}
                        >
                          {isConsoleFullScreen ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
                        </button>
                      </div>
                    </div>
                    <div className={`bg-slate-900 rounded-2xl p-5 font-mono text-xs leading-relaxed shadow-xl overflow-hidden ${isConsoleFullScreen ? 'flex-1' : 'h-[600px]'}`}>
                      <ConsoleTerminal
                        task={selectedTask}
                        isFullScreen={isConsoleFullScreen}
                        onMessage={setMessage}
                      />
                    </div>
                  </section>
                </div>
              </motion.div>
            )}

            {activeTab === 'nvitop' && (
              <motion.div
                key="nvitop"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className={isNvitopFullScreen ? "fixed inset-0 z-50 bg-slate-100 p-6 flex flex-col space-y-4" : "space-y-4"}
              >
                <div className="flex items-center justify-between px-1 shrink-0">
                  <h3 className="text-[10px] font-bold uppercase tracking-widest text-slate-400 flex items-center gap-2">
                    <Terminal className="w-3.5 h-3.5" />
                    nvitop GPU 终端
                  </h3>
                  <button
                    onClick={() => setIsNvitopFullScreen(!isNvitopFullScreen)}
                    className="p-1.5 bg-white border border-slate-200 rounded text-slate-500 hover:text-blue-600 hover:bg-blue-50 transition-colors"
                    title={isNvitopFullScreen ? "退出全屏" : "全屏显示"}
                  >
                    {isNvitopFullScreen ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
                  </button>
                </div>
                <div className={`bg-slate-900 rounded-2xl p-5 font-mono text-xs leading-relaxed shadow-xl overflow-hidden ${isNvitopFullScreen ? 'flex-1' : 'h-[calc(100vh-11rem)] min-h-[600px]'}`}>
                  <NvitopTerminal />
                </div>
              </motion.div>
            )}

            {activeTab === 'history' && (
              <motion.div
                key="history"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden flex flex-col"
              >
                <div className="p-4 border-b border-slate-100 flex items-center justify-between bg-slate-50/50 flex-wrap gap-4">
                  <div className="flex items-center gap-3">
                    <div className="p-2 bg-blue-600 rounded-lg text-white">
                      <History className="w-5 h-5" />
                    </div>
                    <span className="font-bold text-slate-800 text-base">系统审计日志</span>
                  </div>

                  <div className="flex items-center gap-2 flex-wrap justify-end">
                    <div className="flex items-center gap-1 bg-slate-100 p-1 rounded-lg border border-slate-200">
                      {(['finished_at', 'started_at'] as HistorySortKey[]).map((sortKey) => (
                        <button
                          key={sortKey}
                          onClick={() => setHistorySort(sortKey)}
                          className={`px-3 py-1 text-[10px] font-bold uppercase tracking-tighter rounded-md transition-all ${
                            historySort === sortKey
                              ? 'bg-white text-blue-600 shadow-sm'
                              : 'text-slate-400 hover:text-slate-600'
                          }`}
                          title={sortKey === 'finished_at' ? '按完成时间排序' : '按开始时间排序'}
                        >
                          {sortKey === 'finished_at' ? '完成时间' : '开始时间'}
                        </button>
                      ))}
                    </div>

                    <div className="flex items-center gap-1 bg-slate-100 p-1 rounded-lg border border-slate-200">
                      {['all', 'marked', 'succeeded', 'failed', 'interrupted', 'cancelled'].map((filter) => (
                        <button
                          key={filter}
                          onClick={() => setStatusFilter(filter)}
                          className={`px-3 py-1 text-[10px] font-bold uppercase tracking-tighter rounded-md transition-all ${
                            statusFilter === filter
                            ? 'bg-white text-blue-600 shadow-sm'
                            : 'text-slate-400 hover:text-slate-600'
                          }`}
                        >
                          {filter === 'all' && '全部'}
                          {filter === 'marked' && '已标记'}
                          {filter === 'succeeded' && '成功'}
                          {filter === 'failed' && '失败'}
                          {filter === 'interrupted' && '中断'}
                          {filter === 'cancelled' && '取消'}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
                <div className="p-4 space-y-4 bg-slate-50/50">
                  {filteredHistoryTasks.map(task => (
                    <HistoryRowInner
                      key={task.id}
                      task={task}
                      onDuplicate={() => duplicateTask(task)}
                      onEdit={() => editTask(task)}
                      onDelete={() => deleteTask(task.id)}
                      onRequeue={() => requeueTask(task.id)}
                      onSelectLog={() => openLogTask(task.id)}
                      isMarked={markedTaskIds.has(task.id)}
                      toggleMark={(e) => { e.stopPropagation(); toggleTaskMark(task.id); }}
                      isBatchDeleteMode={isBatchDeleteMode}
                      isSelectedForDelete={selectedForDelete.has(task.id)}
                      onToggleSelectForDelete={() => toggleTaskForBatchDelete(task)}
                    />
                  ))}
                  {filteredHistoryTasks.length === 0 && (
                    <div className="p-12 text-center text-slate-400 space-y-2">
                       <History className="w-8 h-8 mx-auto opacity-20" />
                       <p className="text-sm">暂无符合条件的审计记录</p>
                    </div>
                  )}
                </div>
	              </motion.div>
	            )}

	            {activeTab === 'activity' && (
	              <motion.div
	                key="activity"
	                initial={{ opacity: 0, y: 10 }}
	                animate={{ opacity: 1, y: 0 }}
	                exit={{ opacity: 0, y: -10 }}
	                className="space-y-4"
	              >
	                <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
	                  <div className="p-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between gap-4 flex-wrap">
	                    <div className="flex items-center gap-3">
	                      <div className="p-2 bg-blue-600 rounded-lg text-white">
	                        <FileText className="w-5 h-5" />
	                      </div>
	                      <div>
	                        <h3 className="font-bold text-slate-800 text-base">系统日志</h3>
	                        <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">Activity Log</p>
	                      </div>
	                    </div>
	                    <div className="flex items-center gap-2">
	                      <button
	                        onClick={() => loadActivityLogs().catch(error => setMessage(error.message))}
	                        className="flex items-center gap-2 px-3 py-2 bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 rounded-lg text-xs font-bold transition-colors shadow-sm"
	                      >
	                        <RefreshCcw className="w-3.5 h-3.5" />
	                        刷新
	                      </button>
	                    </div>
	                  </div>

	                  <div className="p-4 border-b border-slate-100 bg-white grid grid-cols-1 lg:grid-cols-12 gap-3">
	                    <div className="lg:col-span-6 relative">
	                      <Search className="w-3.5 h-3.5 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
	                      <input
	                        value={activitySearch}
	                        onChange={(event) => setActivitySearch(event.target.value)}
	                        placeholder="搜索任务 ID、任务名、命令、环境变量、备注..."
	                        className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-700 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors"
	                      />
	                    </div>
	                    <select
	                      value={activityLevelFilter}
	                      onChange={(event) => setActivityLevelFilter(event.target.value)}
	                      className="lg:col-span-3 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-700 outline-none focus:border-blue-500 transition-colors cursor-pointer"
	                    >
	                      <option value="all">全部级别</option>
	                      <option value="success">成功</option>
	                      <option value="info">信息</option>
	                      <option value="warning">警告</option>
	                      <option value="error">错误</option>
	                    </select>
	                    <select
	                      value={activityEntityFilter}
	                      onChange={(event) => setActivityEntityFilter(event.target.value)}
	                      className="lg:col-span-3 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-700 outline-none focus:border-blue-500 transition-colors cursor-pointer"
	                    >
	                      <option value="all">全部对象</option>
	                      <option value="task">任务</option>
	                      <option value="queue">队列</option>
	                      <option value="gpu">GPU</option>
	                      <option value="profile">环境模板</option>
	                      <option value="scheduler">调度器</option>
	                    </select>
	                  </div>

	                  <div className="p-4 bg-slate-50/50 space-y-3">
	                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
	                      <StatCard label="当前列表" value={String(activityLogs.length)} type="neutral" />
	                      <StatCard label="错误" value={String(activityLogs.filter(log => log.level === 'error').length)} type="rose" />
	                      <StatCard label="警告" value={String(activityLogs.filter(log => log.level === 'warning').length)} type="amber" />
	                      <StatCard label="成功" value={String(activityLogs.filter(log => log.level === 'success').length)} type="blue" />
	                    </div>

	                    {activityLogs.map(log => {
	                      const expanded = expandedActivityLogId === log.id;
	                      const metadataText = JSON.stringify(log.metadata || {}, null, 2);
	                      return (
	                        <div key={log.id} className="bg-white border border-slate-200 rounded-xl shadow-sm hover:border-blue-200 transition-colors overflow-hidden">
	                          <button
	                            type="button"
	                            onClick={() => setExpandedActivityLogId(expanded ? null : log.id)}
	                            className="w-full text-left p-4 flex items-start justify-between gap-4"
	                          >
	                            <div className="min-w-0 flex-1 space-y-2">
	                              <div className="flex items-center gap-2 min-w-0 flex-wrap">
	                                <span className={`px-2 py-0.5 rounded border text-[9px] font-bold ${activityLevelStyle(log.level)}`}>
	                                  {activityLevelLabel(log.level)}
	                                </span>
	                                <span className="text-[10px] text-slate-400 font-mono">#{log.id}</span>
	                                <span className="text-[10px] text-slate-400 font-mono">{formatTime(log.created_at)}</span>
	                                <span className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">{log.source}</span>
	                                <span className="text-[10px] text-slate-400 font-mono truncate">{log.action}</span>
	                              </div>
	                              <div className="flex items-center gap-2 min-w-0">
	                                <h4 className="font-bold text-slate-900 text-[13px] truncate">{log.title}</h4>
	                                <span className="shrink-0 px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100 text-[9px] text-slate-500 font-bold">
	                                  {activityEntityLabel(log.entity_type)}
	                                  {log.entity_id !== null && log.entity_id !== undefined ? ` #${log.entity_id}` : ''}
	                                </span>
	                              </div>
	                              {log.detail && (
	                                <p className="text-[11px] text-slate-500 truncate">{log.detail}</p>
	                              )}
	                            </div>
	                            <ChevronRight className={`w-4 h-4 text-slate-300 shrink-0 mt-1 transition-transform ${expanded ? 'rotate-90' : ''}`} />
	                          </button>
	                          {expanded && (
	                            <div className="px-4 pb-4 space-y-3 border-t border-slate-100 bg-slate-50/40">
	                              <div className="pt-3 flex items-center justify-between">
	                                <span className="text-[10px] font-bold uppercase tracking-widest text-slate-400">完整详情</span>
	                                <button
	                                  onClick={() => copyActivityLog(log)}
	                                  className="flex items-center gap-1.5 px-2.5 py-1 bg-white border border-slate-200 hover:bg-slate-50 text-slate-500 rounded text-[10px] font-bold transition-colors"
	                                >
	                                  <Copy className="w-3 h-3" />
	                                  复制 JSON
	                                </button>
	                              </div>
	                              {log.detail && (
	                                <div className="bg-white border border-slate-200 rounded-lg px-3 py-2 text-xs text-slate-600 whitespace-pre-wrap">
	                                  {log.detail}
	                                </div>
	                              )}
	                              <pre className="bg-slate-950 text-slate-200 rounded-lg p-4 text-[11px] leading-relaxed overflow-auto max-h-[520px] custom-scrollbar">
	                                {metadataText}
	                              </pre>
	                            </div>
	                          )}
	                        </div>
	                      );
	                    })}

	                    {activityLogs.length === 0 && (
	                      <div className="p-12 text-center text-slate-400 space-y-2 bg-white rounded-xl border border-slate-200">
	                        <FileText className="w-8 h-8 mx-auto opacity-20" />
	                        <p className="text-sm">暂无符合条件的系统日志</p>
	                      </div>
	                    )}
	                  </div>
	                </div>
	              </motion.div>
	            )}

	            {activeTab === 'queue' && (
	              <motion.div
	                key="queue"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="space-y-8"
              >
                {/* Emergency Queue */}
                <div className="space-y-4 bg-rose-50/30 p-5 rounded-2xl border border-rose-100">
                  <div className="flex items-center justify-between px-1">
                    <div className="flex items-center gap-2">
                       <div className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-ping" />
                       <h3 className="text-sm font-bold text-slate-800">紧急队列 (Priority)</h3>
                    </div>
                    <span className="text-[10px] font-bold text-rose-500 bg-rose-50 px-2 py-0.5 rounded border border-rose-100 uppercase">优先执行</span>
                  </div>
                  <div className="min-h-[120px] flex flex-col">
                    {urgentQueueTasks.length > 0 ? (
                       <div
                         className={`space-y-4 min-h-[120px] pb-4 transition-colors ${dragState && dragState.overId === 'empty-urgent' ? 'bg-blue-50/20' : ''}`}
                         onDragOver={(e) => { e.preventDefault(); if(e.target === e.currentTarget) setDragState(prev => prev ? { ...prev, overId: 'empty-urgent', position: 'after' } : null); }}
                         onDrop={(e) => {
                           if(dragState?.overId === 'empty-urgent') { handleDrop(e, 'urgent', true); }
                         }}
                       >
                         {urgentQueueTasks.map(task => (
                           <HistoryRowInner
                             key={task.id}
                             task={task}
                             isQueueView={true}
                             onDuplicate={() => duplicateTask(task)}
                             onEdit={() => editTask(task)}
                             onDelete={() => task.status === 'running' ? cancelTask(task.id) : deleteTask(task.id)}
                             onSelectLog={() => openLogTask(task.id)}
                             isMarked={markedTaskIds.has(task.id)}
                             toggleMark={(e) => { e.stopPropagation(); toggleTaskMark(task.id); }}
                             isBatchDeleteMode={isBatchDeleteMode}
                             isSelectedForDelete={selectedForDelete.has(task.id)}
                             onToggleSelectForDelete={() => toggleTaskForBatchDelete(task)}
                             isDragging={dragState?.id === task.id}
                             dragOverPosition={dragState?.overId === task.id ? dragState.position : null}
                             onDragStart={(e) => handleDragStart(e, task.id)}
                             onDragOver={(e) => handleDragOver(e, task.id, 'urgent')}
                             onDrop={(e) => handleDrop(e, 'urgent')}
                             onDragEnd={() => setDragState(null)}
                           />
                         ))}
                       </div>
                    ) : (
                      <div
                        onDragOver={(e) => { e.preventDefault(); setDragState(prev => prev ? { ...prev, overId: 'empty-urgent', position: 'after' } : null); }}
                        onDrop={(e) => handleDrop(e, 'urgent', true)}
                        className={`bg-white/50 border border-dashed rounded-xl flex-1 flex flex-col items-center justify-center p-8 transition-colors ${dragState && dragState.overId === 'empty-urgent' ? 'border-blue-400 bg-blue-50 text-blue-500' : 'border-slate-200 text-slate-300'}`}
                      >
                        <Activity className="w-10 h-10 mb-2 opacity-50" />
                        <p className="text-sm font-medium">暂无紧急任务，支持拖拽移入</p>
                      </div>
                    )}
                  </div>
                </div>

                {/* Execution Queue */}
                <div className="space-y-4 bg-slate-50/50 p-5 rounded-2xl border border-slate-200">
                  <div className="flex items-center justify-between px-1">
                    <div className="flex items-center gap-2">
                       <h3 className="text-sm font-bold text-slate-800">执行队列 (Standard)</h3>
                    </div>
                    <span className="text-[10px] font-bold text-slate-400 bg-slate-50 px-2 py-0.5 rounded border border-slate-100 uppercase">普通任务</span>
                  </div>
                  <div className="min-h-[120px] flex flex-col">
                    {standardQueueTasks.length > 0 ? (
                       <div
                         className={`space-y-4 min-h-[120px] pb-4 transition-colors ${dragState && dragState.overId === 'empty-normal' ? 'bg-slate-50/50' : ''}`}
                         onDragOver={(e) => { e.preventDefault(); if(e.target === e.currentTarget) setDragState(prev => prev ? { ...prev, overId: 'empty-normal', position: 'after' } : null); }}
                         onDrop={(e) => {
                           if(dragState?.overId === 'empty-normal') { handleDrop(e, 'normal', true); }
                         }}
                       >
                         {standardQueueTasks.map(task => (
                           <HistoryRowInner
                             key={task.id}
                             task={task}
                             isQueueView={true}
                             onDuplicate={() => duplicateTask(task)}
                             onEdit={() => editTask(task)}
                             onDelete={() => task.status === 'running' ? cancelTask(task.id) : deleteTask(task.id)}
                             onSelectLog={() => openLogTask(task.id)}
                             isMarked={markedTaskIds.has(task.id)}
                             toggleMark={(e) => { e.stopPropagation(); toggleTaskMark(task.id); }}
                             isBatchDeleteMode={isBatchDeleteMode}
                             isSelectedForDelete={selectedForDelete.has(task.id)}
                             onToggleSelectForDelete={() => toggleTaskForBatchDelete(task)}
                             isDragging={dragState?.id === task.id}
                             dragOverPosition={dragState?.overId === task.id ? dragState.position : null}
                             onDragStart={(e) => handleDragStart(e, task.id)}
                             onDragOver={(e) => handleDragOver(e, task.id, 'normal')}
                             onDrop={(e) => handleDrop(e, 'normal')}
                             onDragEnd={() => setDragState(null)}
                           />
                         ))}
                       </div>
                    ) : (
                      <div
                        onDragOver={(e) => { e.preventDefault(); setDragState(prev => prev ? { ...prev, overId: 'empty-normal', position: 'after' } : null); }}
                        onDrop={(e) => handleDrop(e, 'normal', true)}
                        className={`bg-white/50 border border-dashed rounded-xl flex-1 flex flex-col items-center justify-center p-8 transition-colors ${dragState && dragState.overId === 'empty-normal' ? 'border-slate-400 bg-slate-50 text-slate-500' : 'border-slate-200 text-slate-300'}`}
                      >
                        <Layers className="w-10 h-10 mb-2 opacity-50" />
                        <p className="text-sm font-medium">暂无普通排队任务，支持拖拽移入</p>
                      </div>
                    )}
                  </div>
                </div>
              </motion.div>
            )}

            {activeTab === 'settings' && (
              <motion.div
                key="settings"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="space-y-8"
              >
                {/* GPU Pool Controls */}
                <div className="bg-white rounded-2xl p-8 border border-slate-200 shadow-sm space-y-6 relative overflow-hidden">
                  <div className="absolute top-0 right-0 p-8 opacity-[0.03]">
                     <Cpu className="w-32 h-32 text-slate-900" />
                  </div>
                  <div className="relative">
                    <div className="flex items-center gap-3 mb-6">
                      <div className="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center border border-blue-100">
                        <Server className="w-6 h-6 text-blue-600" />
                      </div>
                      <div>
                        <h3 className="text-lg font-bold text-slate-900">GPU 实时资源池</h3>
                        <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">Hardware Scheduling Pool</p>
                      </div>
                    </div>

                    <div className="space-y-6">
                      <div className="space-y-3">
                        <p className="text-xs font-bold text-slate-500 uppercase tracking-widest">全局可用 GPU (Global Select)</p>
                        <div className="flex flex-wrap items-start gap-4">
                          {gpus.map(gpu => {
                            const schedule = gpuSchedule[String(gpu.id)];
                            return (
                            <div key={gpu.id} className={`space-y-2 rounded-xl border p-3 transition-all ${
                              enabledGpus.includes(gpu.id)
                              ? 'bg-blue-50 border-blue-200 text-blue-700'
                              : 'bg-slate-50 border-slate-200 text-slate-500'
                            }`}>
                              <label className="flex items-center gap-3 cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={enabledGpus.includes(gpu.id)}
                                  onChange={() => toggleGpu(gpu.id)}
                                  className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                                />
                                <span className="text-sm font-bold">GPU {gpu.id}</span>
                              </label>
                              <div className="space-y-2 border-t border-white/70 pt-2">
                                {schedule && (
                                  <div className="flex items-center justify-between gap-2 text-[10px] font-bold text-slate-500">
                                    <span>{schedule.action === 'enable' ? '定时开启' : '定时关闭'} {formatScheduleTime(schedule.run_at)}</span>
                                    <button
                                      type="button"
                                      onClick={() => clearGpuSchedule(gpu.id)}
                                      className="text-rose-500 hover:text-rose-600"
                                    >
                                      取消
                                    </button>
                                  </div>
                                )}
                                <input
                                  type="datetime-local"
                                  value={gpuScheduleDrafts[String(gpu.id)] ?? toDatetimeLocalValue(schedule?.run_at)}
                                  onChange={(event) => setGpuScheduleDrafts(prev => ({ ...prev, [String(gpu.id)]: event.target.value }))}
                                  className="w-full rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-[11px] font-medium text-slate-700 outline-none focus:border-blue-500"
                                />
                                <button
                                  type="button"
                                  onClick={() => scheduleGpuState(gpu.id, enabledGpus.includes(gpu.id) ? 'disable' : 'enable')}
                                  className="w-full rounded-lg bg-slate-900 px-2 py-1.5 text-[11px] font-bold text-white hover:bg-slate-800 transition-colors"
                                >
                                  {enabledGpus.includes(gpu.id) ? '定时关闭' : '定时开启'}
                                </button>
                              </div>
                            </div>
                          );
                          })}
                          <div className="flex w-[210px] max-w-full self-start flex-col gap-3 rounded-xl border border-emerald-100 bg-emerald-50/60 p-3">
                            <label className="flex items-start gap-2 cursor-pointer">
                              <input
                                type="checkbox"
                                checked={autoRestoreIdleGpuEnabled}
                                onChange={(event) => {
                                  setAutoRestoreMinutesDraft(null);
                                  setSchedulerSettingsDraft(prev => ({
                                    ...prev,
                                    auto_restore_idle_gpu_seconds: event.target.checked
                                      ? (schedulerSettings?.auto_restore_idle_gpu_seconds || 300)
                                      : null,
                                  }));
                                }}
                                className="mt-0.5 w-4 h-4 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500"
                              />
                              <span>
                                <span className="block text-sm font-bold text-slate-900">空闲自动恢复可用</span>
                                <span className="block text-[10px] font-bold uppercase tracking-widest text-emerald-700">Idle Auto Restore</span>
                              </span>
                            </label>
                            <div className="space-y-1">
                              <span className="block text-xs font-bold text-slate-700">等待时间 (分钟)</span>
                              <div className="flex items-center gap-2">
                                <input
                                  type="number"
                                  min="0.1"
                                  step="1"
                                  disabled={!autoRestoreIdleGpuEnabled}
                                  value={autoRestoreIdleGpuMinutes}
                                  onChange={(event) => {
                                    setAutoRestoreMinutesDraft(event.target.value);
                                    setSchedulerSettingsDraft(prev => ({
                                      ...prev,
                                      auto_restore_idle_gpu_seconds: autoRestoreIdleGpuSeconds || 300,
                                    }));
                                  }}
                                  className="w-[112px] rounded-lg border border-emerald-100 bg-white px-3 py-2 text-sm font-bold text-slate-900 outline-none transition-colors focus:border-emerald-500 disabled:bg-slate-100 disabled:text-slate-400"
                                />
                                <button
                                  type="button"
                                  onClick={saveSchedulerSettings}
                                  disabled={!hasSchedulerSettingsDraft}
                                  className={`px-4 py-2.5 rounded-lg text-sm font-bold transition-all ${
                                    !hasSchedulerSettingsDraft
                                      ? 'bg-white/70 text-slate-400 border border-emerald-100 cursor-not-allowed'
                                      : 'bg-emerald-600 text-white shadow-sm hover:bg-emerald-700 active:scale-95'
                                  }`}
                                >
                                  保存
                                </button>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>

                      <div className="flex gap-3 pt-4">
                        <button
                          onClick={() => setEnabledGpus(gpus.map(g => g.id))}
                          className="px-6 py-2.5 rounded-lg bg-slate-50 border border-slate-200 text-slate-600 text-sm font-bold hover:bg-slate-100 transition-all"
                        >
                          恢复全部可用
                        </button>
                        <button onClick={applyGpuSettings} className="px-8 py-2.5 rounded-lg bg-slate-900 text-white text-sm font-bold shadow-md hover:bg-slate-800 active:scale-95 transition-all">
                          应用 GPU 设置
                        </button>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                  {gpus.map(gpu => (
                    <GPUCard key={gpu.id} gpu={gpu} />
                  ))}
                </div>

                {/* Scheduler Strategy Controls */}
                <div className="bg-white border border-slate-200 rounded-2xl p-6 text-slate-700 shadow-sm space-y-5 relative overflow-hidden">
                  <div className="absolute top-0 right-0 p-6 opacity-[0.02]">
                    <Clock className="w-32 h-32 text-slate-900" />
                  </div>
                  <div className="relative">
                    <div className="flex items-center justify-between pointer-events-none">
                      <div className="flex items-center gap-3">
                        <div className="w-10 h-10 bg-emerald-50 rounded-xl flex items-center justify-center border border-emerald-100">
                          <Settings className="w-6 h-6 text-emerald-600" />
                        </div>
                        <div>
                          <h3 className="text-xl font-black text-slate-900 tracking-tight">调度器策略 (调控器)</h3>
                          <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">Scheduler Controls</p>
                        </div>
                      </div>
                    </div>

                    <div className="mt-6 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                      <div className="space-y-2">
                        <label className="block text-xs font-bold text-slate-700">轮询间隔 (秒)</label>
                        <input
                          type="number"
                          step="0.1"
                          min="0.1"
                          value={schedulerSettingsDraft.poll_interval_seconds ?? schedulerSettings?.poll_interval_seconds ?? ''}
                          onChange={(e) => setSchedulerSettingsDraft(prev => ({ ...prev, poll_interval_seconds: parseFloat(e.target.value) }))}
                          className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm font-bold text-slate-900 focus:bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 transition-colors"
                        />
                        <p className="text-[10px] text-slate-500 font-medium">调度器每次检查队首任务的间隔时间</p>
                      </div>

                      <div className="space-y-2">
                        <label className="block text-xs font-bold text-slate-700">GPU 空闲确认次数</label>
                        <input
                          type="number"
                          step="1"
                          min="1"
                          value={schedulerSettingsDraft.gpu_idle_required_checks ?? schedulerSettings?.gpu_idle_required_checks ?? ''}
                          onChange={(e) => setSchedulerSettingsDraft(prev => ({ ...prev, gpu_idle_required_checks: parseInt(e.target.value, 10) }))}
                          className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm font-bold text-slate-900 focus:bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 transition-colors"
                        />
                        <p className="text-[10px] text-slate-500 font-medium">连续 N 次轮询确认空闲后，才可分配任务</p>
                      </div>

                      <div className="space-y-2">
                        <label className="block text-xs font-bold text-slate-700">外部空闲等待时间 (只读)</label>
                        <div className="w-full bg-slate-100 border border-slate-200 rounded-lg px-3 py-2 text-sm font-bold text-slate-600 flex items-center justify-between">
                          <span>{schedulerSettings?.effective_wait_seconds ?? '-'} 秒</span>
                          <Clock className="w-4 h-4 text-slate-400" />
                        </div>
                        <p className="text-[10px] text-slate-500 font-medium">外部释放或未知占用后使用；调度器任务结束后快速接续</p>
                      </div>

                      <div className="space-y-2">
                        <label className="block text-xs font-bold text-slate-700">外部 kill 后冷却 (秒)</label>
                        <input
                          type="number"
                          step="1"
                          min="0"
                          value={externalKillGpuCooldownSeconds}
                          onChange={(event) => {
                            const value = parseFloat(event.target.value);
                            setSchedulerSettingsDraft(prev => ({
                              ...prev,
                              external_kill_gpu_cooldown_seconds: Number.isFinite(value) ? value : 0,
                            }));
                          }}
                          className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm font-bold text-slate-900 focus:bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 transition-colors"
                        />
                        <p className="text-[10px] text-slate-500 font-medium">0 表示外部 kill 后不进入 GPU 冷却</p>
                      </div>
                    </div>

                    <div className="mt-6 border-t border-slate-100 pt-5 space-y-4">
                      <label className="inline-flex items-center gap-3 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={autoRetryEnabled}
                          onChange={(event) => {
                            const enabled = event.target.checked;
                            setSchedulerSettingsDraft(prev => ({
                              ...prev,
                              auto_retry_enabled: enabled,
                              auto_retry_max_retries: enabled
                                ? Math.max(1, schedulerSettings?.auto_retry_max_retries ?? 1)
                                : 0,
                            }));
                          }}
                          className="w-4 h-4 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500"
                        />
                        <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600 border border-emerald-100">
                          <RefreshCcw className="h-4 w-4" />
                        </span>
                        <span>
                          <span className="block text-sm font-bold text-slate-900">OOM / CUDA 资源错误自动重试</span>
                          <span className="block text-[10px] font-bold uppercase tracking-widest text-slate-400">Auto Retry</span>
                        </span>
                      </label>

                      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                        <div className="space-y-2">
                          <label className="block text-xs font-bold text-slate-700">额外重试次数</label>
                          <input
                            type="number"
                            step="1"
                            min="1"
                            disabled={!autoRetryEnabled}
                            value={autoRetryEnabled ? autoRetryMaxRetries : 0}
                            onChange={(event) => {
                              const value = parseInt(event.target.value, 10);
                              setSchedulerSettingsDraft(prev => ({
                                ...prev,
                                auto_retry_enabled: true,
                                auto_retry_max_retries: Number.isFinite(value) ? value : 1,
                              }));
                            }}
                            className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm font-bold text-slate-900 focus:bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 disabled:bg-slate-100 disabled:text-slate-400 transition-colors"
                          />
                          <p className="text-[10px] text-slate-500 font-medium">1 表示失败后最多再跑一次</p>
                        </div>

                        <div className="space-y-2">
                          <label className="block text-xs font-bold text-slate-700">重试延迟 (秒)</label>
                          <input
                            type="number"
                            step="1"
                            min="0"
                            disabled={!autoRetryEnabled}
                            value={autoRetryDelaySeconds}
                            onChange={(event) => {
                              const value = parseInt(event.target.value, 10);
                              setSchedulerSettingsDraft(prev => ({
                                ...prev,
                                auto_retry_delay_seconds: Number.isFinite(value) ? value : 0,
                              }));
                            }}
                            className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm font-bold text-slate-900 focus:bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 disabled:bg-slate-100 disabled:text-slate-400 transition-colors"
                          />
                          <p className="text-[10px] text-slate-500 font-medium">重新回队列前等待的时间</p>
                        </div>

                        <div className="space-y-2">
                          <label className="block text-xs font-bold text-slate-700">当前状态</label>
                          <div className="w-full bg-slate-100 border border-slate-200 rounded-lg px-3 py-2 text-sm font-bold text-slate-600 flex items-center justify-between">
                            <span>{autoRetryEnabled ? `开启，最多 ${autoRetryMaxRetries + 1} 次尝试` : '关闭'}</span>
                            <AlertCircle className="w-4 h-4 text-slate-400" />
                          </div>
                          <p className="text-[10px] text-slate-500 font-medium">普通业务失败仍然不会自动重试</p>
                        </div>
                      </div>
                    </div>

                    <div className="flex justify-end pt-4 border-t border-slate-100 mt-6">
                      <button
                        onClick={saveSchedulerSettings}
                        disabled={!hasSchedulerSettingsDraft}
                        className={`px-8 py-2.5 rounded-lg text-sm font-bold shadow-sm transition-all focus:ring-2 focus:ring-emerald-500/20 focus:ring-offset-2 ${
                          !hasSchedulerSettingsDraft
                            ? 'bg-slate-100 text-slate-400 border-transparent cursor-not-allowed'
                            : 'bg-emerald-600 hover:bg-emerald-700 text-white border-transparent active:scale-95'
                        }`}
                      >
                        保存调控器设置
                      </button>
                    </div>
                  </div>
                </div>

                {/* Environment Template Configuration */}
                <div className="bg-white border border-slate-200 rounded-2xl p-6 text-slate-700 shadow-sm space-y-5">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <h3 className="text-xl font-black text-slate-900 tracking-tight">环境模板配置</h3>
                      <span className="px-2.5 py-0.5 rounded-full bg-slate-100 text-[10px] font-bold text-slate-500 tracking-widest uppercase border border-slate-200">Profiles</span>
                    </div>
                      <button type="submit" form="profile-form" className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white font-bold rounded-lg text-sm transition-colors shadow-sm flex items-center gap-2">
                        <Plus className="w-4 h-4" />
                        保存环境配置
                      </button>
                    </div>

                    <form id="profile-form" key={profileDraft?.id || 'new'} onSubmit={saveProfile} className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                        <input type="hidden" name="profile_id" value={profileDraft?.id || ''} readOnly />
                      {/* Left Column */}
                      <div className="space-y-4">
                        <div className="space-y-2">
                          <label className="block text-xs font-bold text-slate-600">自动发现本机环境</label>
                          <div className="flex gap-2">
                            <button type="button" onClick={scanProfiles} className="px-4 py-2 bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold rounded-lg transition-colors text-sm shrink-0 border border-slate-200">
                              扫描
                            </button>
                            <select value={selectedDiscoveryId} onChange={(event) => setSelectedDiscoveryId(event.target.value)} className="flex-1 bg-white border border-slate-200 rounded-lg px-3 py-2 text-sm font-medium text-slate-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 appearance-none cursor-pointer">
                              <option value="">(请先点击扫描)</option>
                              {[...discovery.conda_envs, ...discovery.venvs].map((item, index) => (
                                <option key={`${item.display_name}-${index}`} value={index}>{item.display_name}</option>
                              ))}
                            </select>
                              {selectedDiscoveryId !== '' && (
                                <button type="button" onClick={importDiscovery} className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white font-bold rounded-lg transition-colors text-sm shrink-0 border border-blue-600">
                                  导入
                                </button>
                              )}
                          </div>
                        </div>

                        <div className="space-y-2">
                          <label className="block text-xs font-bold text-slate-600">管理已用配置</label>
                          <select
                            value={managedProfileId}
                            onChange={(event) => {
                              setManagedProfileId(event.target.value);
                              setProfileDraft(profiles.find(profile => String(profile.id) === event.target.value) || null);
                            }}
                            className="w-full bg-white border border-slate-200 rounded-lg px-3 py-2 text-sm font-medium text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 appearance-none cursor-pointer"
                          >
                            <option value="">+ 新建模板 (保持下方表单为空新增)</option>
                              {profiles.map(profile => (
                                <option key={profile.id} value={profile.id}>{profile.name}</option>
                              ))}
                          </select>
                        </div>

                      <div className="space-y-2 pt-2">
                        <label className="block text-xs font-bold text-slate-700">模板别名</label>
                          <input
                            type="text"
                              name="name"
                              defaultValue={profileDraft?.name || ''}
                            placeholder="例如：torch-cu12"
                            className="w-full bg-white border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-shadow transition-colors"
                          />
                      </div>
                    </div>

                    {/* Right Column */}
                    <div className="space-y-4">
                      <div className="space-y-2">
                        <label className="block text-xs font-bold text-slate-700">激活脚本 (Shell)</label>
                          <textarea
                            rows={2}
                              name="shell_setup"
                              defaultValue={profileDraft?.shell_setup || ''}
                            placeholder="source /venv/bin/activate"
                            className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm font-mono text-slate-600 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 resize-none transition-shadow transition-colors"
                          />
                      </div>

                        <div className="space-y-2">
                          <label className="block text-xs font-bold text-slate-700">默认环境变量 & 备注</label>
                          <div className="flex flex-col gap-2">
                          <textarea
                            rows={3}
                            name="env"
                            defaultValue={envToText(profileDraft?.env)}
                            placeholder="CUDA_VISIBLE_DEVICES..."
                            className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm font-mono text-slate-600 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 resize-none transition-shadow transition-colors"
                          />
                          <input
                            type="text"
                            name="notes"
                            defaultValue={profileDraft?.notes || ''}
                            placeholder="可选备注说明"
                            className="w-full bg-white border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-600 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-shadow transition-colors"
                          />
                        </div>
                      </div>
                    </div>
                  </form>
                  </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </main>

      {/* New Task Modal */}
      <AnimatePresence>
        {showNewTask && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-slate-900/40 backdrop-blur-sm">
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 20 }}
              className="relative w-full max-w-4xl bg-white border border-slate-200 rounded-2xl shadow-2xl overflow-hidden flex flex-col"
            >
              <div className="px-8 py-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between">
                <div>
                  <h2 className="text-lg font-bold text-slate-900 tracking-tight">{isMetadataOnlyTaskEdit ? '编辑记录信息' : taskDraft ? (isEditingTask ? '编辑任务' : '复用任务创建新任务') : '创建新运行时任务'}</h2>
                  <p className="text-xs text-slate-500 font-medium">{isMetadataOnlyTaskEdit ? `任务 #${taskDraft?.id} 的名称和备注` : taskDraft ? (isEditingTask ? `配置任务 #${taskDraft.id} 并提交更新` : `已填入任务 #${taskDraft.id} 的参数，可修改后提交`) : '配置参数并提交给调度系统'}</p>
                </div>
                <button onClick={closeNewTaskModal} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-900 transition-colors">
                  <Plus className="w-5 h-5 rotate-45" />
                </button>
              </div>

              <form key={taskDraft?.id || 'new'} onSubmit={submitTask} className="px-8 py-5 space-y-4 overflow-y-auto max-h-[80vh] custom-scrollbar">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
                  {/* Task Name */}
	                  <div className="space-y-1.5 md:col-span-2">
	                    <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">任务名称</label>
	                      <input
                        type="text"
                          name="name"
                          defaultValue={taskDraft?.name || ''}
                        placeholder="例如: llama-sft"
	                        className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors"
	                      />
	                  </div>

	                  {isMetadataOnlyTaskEdit && (
	                    <div className="space-y-1.5 md:col-span-2">
	                      <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">备注说明</label>
	                      <input
	                        type="text"
	                        name="notes"
	                        defaultValue={taskDraft?.notes || ''}
	                        placeholder="可选备注"
	                        className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors"
	                      />
	                    </div>
	                  )}

	                  {!isMetadataOnlyTaskEdit && (
	                    <>
	                      {/* Environment Template */}
	                      <div className="space-y-1.5">
	                        <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">环境模板</label>
	                          <select name="profile_id" defaultValue={taskDraft?.profileId ?? ''} className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-sm text-slate-700 outline-none focus:border-blue-500 transition-colors cursor-pointer">
	                            <option value="">不使用环境模板</option>
	                              {profiles.map(profile => (
	                                <option key={profile.id} value={profile.id}>{profile.name}</option>
	                              ))}
	                          </select>
	                      </div>

	                      {/* GPU Selection */}
	                      <div className="space-y-1.5">
	                        <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">指定 GPU</label>
	                          <select name="requested_gpu" defaultValue={taskDraft?.requestedGpu ?? ''} className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-sm text-slate-700 outline-none focus:border-blue-500 transition-colors cursor-pointer">
	                            <option value="">自动分配</option>
	                              {gpus.map(gpu => (
	                                <option key={gpu.id} value={gpu.id}>GPU {gpu.id} ({gpu.name})</option>
	                              ))}
	                          </select>
	                      </div>

	                      {/* Queue Type */}
	                      <div className="space-y-1.5">
	                        <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">队列策略</label>
	                        <div className="flex items-center gap-3 bg-slate-50 border border-slate-200 rounded-lg px-4 py-[7px] cursor-pointer hover:bg-slate-100 transition-all">
	                            <input type="checkbox" name="is_urgent" defaultChecked={Boolean(taskDraft?.isUrgent)} className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500" />
	                          <span className="text-sm font-semibold text-slate-600">加入紧急队列 (Priority)</span>
	                        </div>
	                      </div>

	                      {/* GPU Memory Budget */}
	                      <div className="space-y-1.5">
	                        <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">显存预算 (GB)</label>
	                        <input
	                          type="number"
	                          min="0"
	                          step="0.1"
	                          name="gpu_memory_budget_gb"
	                          defaultValue={taskDraft?.gpuMemoryBudgetMb ? taskDraft.gpuMemoryBudgetMb / 1024 : ''}
	                          placeholder="不填写则使用默认空闲阈值"
	                          className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors"
	                        />
	                      </div>
	                    </>
	                  )}
	                </div>

	                {/* Dependencies */}
		                {!isMetadataOnlyTaskEdit && (
		                  <div className="space-y-1.5">
		                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
		                      <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">依赖任务 (Dependencies)</label>
		                      <span className="text-[10px] text-slate-400">选择前置任务，全部成功完成后才会调度当前任务</span>
		                      <span className="text-[10px] text-slate-400">按住 Ctrl/Cmd 多选，不选则无依赖</span>
		                    </div>
		                    <select
		                      name="depends_on"
		                      multiple
		                      size={Math.min(4, Math.max(2, dependencyCandidates.length))}
		                      disabled={dependencyCandidates.length === 0}
		                      defaultValue={taskDraft?.dependsOn?.map(String) || []}
		                      className={`w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-blue-500 transition-colors ${dependencyCandidates.length === 0 ? 'text-slate-400 cursor-not-allowed' : 'text-slate-700'}`}
		                    >
		                      {dependencyCandidates.length === 0 ? (
		                        <option value="">暂无可选依赖任务</option>
		                      ) : (
		                        dependencyCandidates.map(candidate => (
		                          <option key={candidate.id} value={candidate.id}>
		                            #{candidate.id} {candidate.name} ({candidate.isMissingDependency ? '当前列表外' : taskStatusLabel(candidate.status)})
		                          </option>
		                        ))
		                      )}
		                    </select>
		                  </div>
		                )}

	                {!isMetadataOnlyTaskEdit && (
	                  <>
	                    {/* Command */}
	                    <div className="space-y-1.5">
	                      <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">启动命令</label>
		                        <textarea
		                          rows={4}
		                          name="command"
		                          defaultValue={taskDraft?.command || ''}
		                          placeholder="python main.py --model llama --dataset sft..."
		                          required
		                          className="w-full min-h-[96px] bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-slate-900 font-mono text-[11px] placeholder-slate-400 outline-none focus:border-blue-500 transition-colors resize-none"
		                        />
	                    </div>

	                    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
	                      {/* Working Directory */}
	                      <div className="space-y-1.5">
	                        <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">工作目录</label>
	                          <input
	                            type="text"
	                              name="cwd"
	                              defaultValue={taskDraft?.workingDir || ''}
	                            placeholder="/path/to/project (可选)"
	                            className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors"
	                          />
	                      </div>

	                      {/* Remarks */}
	                      <div className="space-y-1.5">
	                        <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">备注说明</label>
	                          <input
	                            type="text"
	                              name="notes"
	                              defaultValue={taskDraft?.notes || ''}
	                            placeholder="可选备注"
	                            className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors"
	                          />
	                      </div>
	                    </div>

	                    {/* Env Vars */}
	                    <div className="space-y-1.5">
	                      <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">独立环境变量</label>
	                        <textarea
	                          rows={1}
	                            name="env"
	                            defaultValue={envToText(taskDraft?.env)}
	                          placeholder="WANDB_MODE=offline"
	                          className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-slate-900 font-mono text-[11px] placeholder-slate-400 outline-none focus:border-blue-500 transition-colors resize-none"
	                        />
	                    </div>
	                  </>
	                )}

                <div className="flex justify-end gap-3 pt-4 border-t border-slate-100">
                    <button
                        type="button"
                      onClick={closeNewTaskModal}
                      className="px-6 py-2 rounded-lg text-slate-500 font-bold hover:bg-slate-50 transition-colors text-sm"
                    >
                    取消
                  </button>
                    <button
                        type="submit"
                      className="px-10 py-2 bg-blue-600 text-white rounded-lg font-bold shadow-md shadow-blue-600/20 hover:bg-blue-700 active:scale-95 transition-all text-sm"
	                    >
	                      {isMetadataOnlyTaskEdit
	                      ? '保存记录信息'
	                      : taskDraft
	                      ? (isEditingTask ? '更新任务并加入队列' : '创建复用任务')
	                      : '部署任务至调度器'}
	                    </button>
                </div>
              </form>
              </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  );
}

function NavItem({ icon, label, active, count, onClick }: { icon: ReactNode, label: string, active?: boolean, count?: number, onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center justify-between px-3 py-2 rounded-lg transition-all duration-200 group ${
        active
        ? 'bg-blue-50 text-blue-700 font-bold shadow-sm ring-1 ring-blue-100'
        : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
      }`}
    >
      <div className="flex items-center gap-3">
        <span className={active ? 'text-blue-600' : 'text-slate-400 group-hover:text-slate-600'}>{icon}</span>
        <span className="text-sm tracking-tight">{label}</span>
      </div>
      {count !== undefined && (
        <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${active ? 'bg-blue-200 text-blue-800' : 'bg-slate-100 text-slate-500'}`}>
          {count}
        </span>
      )}
    </button>
  );
}

function StatCard({ label, value, trend, type }: { label: string, value: string, trend?: string, type: 'blue' | 'amber' | 'rose' | 'neutral' }) {
  const typeColors = {
    blue: 'text-blue-600',
    amber: 'text-amber-600',
    rose: 'text-rose-500',
    neutral: 'text-slate-900'
  };

  return (
    <div className="bg-white p-3 rounded-xl border border-slate-200 shadow-sm transition-all hover:shadow-md">
      <p className="text-[9px] font-bold text-slate-400 uppercase tracking-widest mb-1">{label}</p>
      <p className={`text-xl font-semibold tracking-tight ${typeColors[type]}`}>
        {value}
        {trend && <span className="text-[9px] font-bold text-emerald-500 ml-1.5 tracking-normal inline-block align-middle">{trend}</span>}
      </p>
    </div>
  );
}

function GPUCard({ gpu }: { gpu: GPUStatus; key?: React.Key }) {
  const memPercent = gpu.memoryTotal ? (gpu.memoryUsed / gpu.memoryTotal) * 100 : 0;
  const statusText = gpu.isCoolingDown
    ? `冷却 ${gpu.cooldownRemainingSeconds ?? 0}s`
    : gpu.isBusy ? '活跃' : '空闲';
  const statusClass = gpu.isCoolingDown
    ? 'bg-amber-50 text-amber-700 border border-amber-100'
    : gpu.isBusy
      ? 'bg-blue-50 text-blue-600 border border-blue-100'
      : 'bg-slate-50 text-slate-500 border border-slate-100';

  return (
    <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-sm transition-all hover:shadow-md hover:border-blue-200 group flex items-center gap-4">
      <div className="flex items-center gap-3 shrink-0">
        <div className={`p-1.5 rounded-lg ${gpu.isCoolingDown ? 'bg-amber-50 text-amber-600' : gpu.isBusy ? 'bg-blue-50 text-blue-600' : 'bg-slate-50 text-slate-400'}`}>
          <Cpu className="w-4 h-4" />
        </div>
        <div>
          <h4 className="font-bold text-slate-800 leading-none mb-1 text-[13px]">节点 {gpu.id}</h4>
          <p className="text-[8px] text-slate-400 font-bold uppercase tracking-tight">{gpu.name}</p>
        </div>
      </div>

      <div className="flex-1 grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <div className="flex justify-between text-[8px] font-bold uppercase tracking-widest text-slate-400">
            <span>显存负载</span>
            <span className="text-slate-700 font-mono tracking-normal text-[9px]">{Math.round(gpu.memoryUsed / 1024)}G / {Math.round(gpu.memoryTotal / 1024)}G</span>
          </div>
          <div className="h-1 w-full bg-slate-100 rounded-full overflow-hidden">
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${memPercent}%` }}
              className={`h-full transition-all ${memPercent > 80 ? 'bg-amber-500' : 'bg-blue-500'}`}
            />
          </div>
        </div>
        <div className="flex flex-col justify-center">
           <div className="flex justify-between items-center bg-slate-50 px-1.5 py-0.5 rounded">
             <span className="text-[8px] font-bold text-slate-400 uppercase tracking-widest">余量</span>
             <span className="text-[11px] font-bold text-slate-900 font-mono">{Math.round(gpu.memoryFree / 1024)}G</span>
           </div>
        </div>
      </div>

      <div className="shrink-0">
        <div className={`px-2 py-0.5 rounded text-[9px] font-bold uppercase tracking-tighter ${statusClass}`}>
          {statusText}
        </div>
      </div>
    </div>
  );
};

function TaskNotesPill({ notes, className = '' }: { notes?: string; className?: string }) {
  if (!notes) return null;
  return (
    <div
      title={notes}
      className={`min-w-0 flex items-center gap-1.5 px-2 py-0.5 bg-amber-50/70 border border-amber-100 rounded text-[9px] text-amber-700 shadow-sm ${className}`}
    >
      <span className="text-amber-500 font-bold uppercase tracking-tight text-[8px] whitespace-nowrap">备注</span>
      <span className="truncate">{notes}</span>
    </div>
  );
}

  const TaskCardInner = ({ task, isSelected, onSelect, onCancel, onPreempt, canPreempt, onDuplicate, onEdit, isMarked, toggleMark }: { task: Task; isSelected?: boolean; onSelect?: () => void; onCancel?: () => void; onPreempt?: () => void; canPreempt?: boolean; onDuplicate?: () => void; onEdit?: () => void; isMarked?: boolean; toggleMark?: (e: React.MouseEvent) => void; key?: React.Key }) => {
  const canEdit = Boolean(onEdit);
  const editTitle = task.status === 'pending' ? '编辑任务' : '编辑记录信息';

  return (
    <div
      onClick={onSelect}
      className={`border rounded-xl p-3.5 transition-all group flex flex-col gap-2.5 cursor-pointer ${
        isSelected
        ? 'bg-white border-blue-500 ring-1 ring-blue-500 shadow-md transform scale-[1.005]'
        : isMarked
          ? 'bg-amber-50/40 border-amber-300 shadow-sm hover:border-amber-400 hover:shadow-md'
          : 'bg-white border-slate-200 shadow-sm hover:border-slate-300 hover:shadow-md'
      }`}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4 shrink-0">
          <div className={`p-2 rounded-xl border shadow-inner transition-colors ${isSelected ? 'bg-blue-600 text-white border-blue-500' : 'bg-blue-50 text-blue-600 border-blue-100'}`}>
            <Play className="w-4 h-4 fill-current" />
          </div>
          <div className="min-w-0">
            <h4 className="font-bold text-slate-900 leading-tight mb-0.5 truncate flex items-center gap-2 text-[13px]">
              {task.name}
              {isSelected && <span className="w-1.5 h-1.5 rounded-full bg-blue-600 animate-pulse" />}
            </h4>
            <div className="flex items-center gap-2 text-[9px] font-bold text-slate-400 uppercase tracking-tighter">
              <span className={`px-1.5 py-0.5 rounded transition-colors ${isSelected ? 'bg-blue-100 text-blue-700' : 'bg-blue-50 text-blue-600'}`}>{task.profile}</span>
              <span>•</span>
              <span>节点 {task.gpu}</span>
              {task.gpuMemoryBudgetMb && (
                <>
                  <span>•</span>
                  <span>{(task.gpuMemoryBudgetMb / 1024).toFixed(1)}G</span>
                </>
              )}
              {task.workingDir && (
                <>
                  <span>•</span>
                  <span className="normal-case font-mono">{task.workingDir}</span>
                </>
              )}
              {task.hasDependencies && task.status === 'pending' && (
                <>
                  <span>•</span>
                  <span className="inline-flex items-center gap-0.5 text-amber-600">
                    <Link2 className="w-3 h-3" />
                    等待 {task.dependencyCount}
                  </span>
                </>
              )}
            </div>
          </div>
        </div>

        <div className="flex flex-col items-end shrink-0 gap-1">
          <div className="flex items-center gap-0.5">
              <button
                title="复用新建"
                onClick={(e) => {
                  e.stopPropagation();
                    onDuplicate?.();
                }}
              className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90"
            >
              <Copy className="w-3.5 h-3.5" />
            </button>
            {canEdit && (
              <button
                title={editTitle}
                onClick={(e) => {
                  e.stopPropagation();
                  onEdit?.();
                }}
                className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90"
              >
                <Edit2 className="w-3.5 h-3.5" />
              </button>
            )}
              <button
                title={canPreempt ? "抢占给紧急队列" : "当前没有等待中的紧急任务"}
                disabled={!canPreempt}
                onClick={(e) => {
                    e.stopPropagation();
                    onPreempt?.();
                  }}
                className={`p-1.5 rounded-lg transition-all active:scale-90 ${
                  canPreempt
                    ? 'text-slate-400 hover:text-amber-600 hover:bg-amber-50'
                    : 'text-slate-300 cursor-not-allowed'
                }`}
              >
                <AlertCircle className="w-3.5 h-3.5" />
              </button>
              <button
                title={isMarked ? "取消标记" : "标记任务"}
                onClick={toggleMark}
                className={`p-1.5 rounded-lg transition-all active:scale-90 ${
                  isMarked
                    ? 'text-amber-500 bg-amber-50 hover:bg-amber-100 hover:text-amber-600'
                    : 'text-slate-400 hover:text-amber-500 hover:bg-amber-50'
                }`}
              >
                <Bookmark className={`w-3.5 h-3.5 ${isMarked ? 'fill-current' : ''}`} />
              </button>
              <button
                title="中断/取消任务"
                onClick={(e) => {
                    e.stopPropagation();
                    onCancel?.();
                  }}
              className="p-1.5 text-slate-400 hover:text-rose-500 hover:bg-rose-50 rounded-lg transition-all active:scale-90"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
          <div className="flex items-center gap-2 text-[9px] font-medium text-slate-400 pr-1.5">
             <span className="uppercase tracking-widest text-[8px] font-bold text-slate-300">开始</span>
             <span className="tabular-nums font-mono">{task.startedAt?.split(' ')[1] || '-'}</span>
          </div>
        </div>
      </div>

      <div className={`rounded-lg px-3 py-2 border transition-colors ${isSelected ? 'bg-slate-900/5 border-slate-200' : 'bg-slate-50/50 border-slate-100'}`}>
	        <code className={`text-[10px] font-mono block truncate ${isSelected ? 'text-slate-800' : 'text-slate-600'}`}>
	          {task.command}
	        </code>
	      </div>
	      <TaskNotesPill notes={task.notes} className="w-full" />
	    </div>
	  );
	}

  const HistoryRowInner = ({
  task,
  isQueueView = false,
  onDuplicate,
  onDelete,
  onRequeue,
  onSelectLog,
  onEdit,
  isMarked,
  toggleMark,
  isDragging,
  dragOverPosition,
  onDragStart,
  onDragOver,
  onDragLeave,
  onDrop,
  onDragEnd,
  isBatchDeleteMode,
  isSelectedForDelete,
  onToggleSelectForDelete,
}: {
  task: Task;
  key?: React.Key;
  isQueueView?: boolean;
  onDuplicate?: () => void;
  onDelete?: () => void;
  onRequeue?: () => void;
  onSelectLog?: () => void;
  onEdit?: () => void;
  isMarked?: boolean;
  toggleMark?: (e: React.MouseEvent) => void;
  isDragging?: boolean;
  dragOverPosition?: 'before' | 'after' | null;
  onDragStart?: (e: React.DragEvent) => void;
  onDragOver?: (e: React.DragEvent) => void;
  onDragLeave?: (e: React.DragEvent) => void;
  onDrop?: (e: React.DragEvent) => void;
  onDragEnd?: (e: React.DragEvent) => void;
  isBatchDeleteMode?: boolean;
  isSelectedForDelete?: boolean;
  onToggleSelectForDelete?: () => void;
}) => {
  const canRequeue = task.status === 'failed' || task.status === 'cancelled' || task.status === 'interrupted';
	  const canEdit = Boolean(onEdit);
	  const editTitle = task.status === 'pending' ? '编辑任务' : '编辑记录信息';
  const canBatchDelete = task.status !== 'running';
  const attemptOptions = useMemo(() => task.attemptLogs || [], [task.attemptLogs]);
  const latestAttempt = attemptOptions.length > 0
    ? attemptOptions[attemptOptions.length - 1].attempt
    : (task.attempts || 1);

  const [isLogExpanded, setIsLogExpanded] = useState(false);
  const [isLogFullScreen, setIsLogFullScreen] = useState(false);
  const [selectedAttempt, setSelectedAttempt] = useState<number | null>(latestAttempt);
  const effectiveSelectedAttempt = selectedAttempt ?? latestAttempt;
  const selectedAttemptLog = attemptOptions.find(log => log.attempt === effectiveSelectedAttempt) || null;
  const selectedStartedAt = formatTime(selectedAttemptLog?.started_at) || (
    effectiveSelectedAttempt === latestAttempt ? task.startedAt : undefined
  );
  const selectedFinishedAt = formatTime(selectedAttemptLog?.finished_at) || (
    effectiveSelectedAttempt === latestAttempt ? task.endedAt : undefined
  );
  const controlledLogAttempt = !isQueueView && attemptOptions.length > 0
    ? effectiveSelectedAttempt
    : undefined;

  useEffect(() => {
    if (attemptOptions.length === 0) return;
    setSelectedAttempt(prev => {
      if (prev !== null && attemptOptions.some(log => log.attempt === prev)) {
        return prev;
      }
      return latestAttempt;
    });
  }, [attemptOptions, latestAttempt]);

  const handleSelectAttempt = (attempt: number | null) => {
    setSelectedAttempt(attempt);
  };

  const getAttemptStatusLabel = (status?: TaskLogEntry['status']) => {
    switch (status) {
      case 'succeeded': return '成功';
      case 'failed': return '失败';
      case 'cancelled': return '取消';
      case 'interrupted': return '中断';
      case 'retry_scheduled': return '已重试';
      case 'preempted': return '已抢占';
      case 'interrupted_requeued': return '已回队';
      case 'running': return '运行中';
      default: return '';
    }
  };

  const handleToggleLog = () => {
    if (!isLogExpanded) {
      setIsLogExpanded(true);
    } else {
      setIsLogExpanded(false);
      setIsLogFullScreen(false);
    }
  };

  const getStatusStyle = () => {
    switch(task.status) {
      case 'succeeded': return 'bg-emerald-50 text-emerald-700 border-emerald-100';
      case 'failed': return 'bg-rose-50 text-rose-700 border-rose-100';
      case 'interrupted': return 'bg-amber-50 text-amber-700 border-amber-100';
      case 'cancelled': return 'bg-slate-100 text-slate-500 border-slate-200';
      default: return 'bg-slate-50 text-slate-400 border-slate-100';
    }
  };

  const getStatusLabel = () => {
    switch(task.status) {
      case 'succeeded': return '成功';
      case 'failed': return '失败';
      case 'interrupted': return '中断';
      case 'cancelled': return '取消';
      case 'running': return '运行中';
      case 'pending': return '待排队';
      default: return task.status;
    }
  };

  return (
    <div
      onClick={isBatchDeleteMode && canBatchDelete ? onToggleSelectForDelete : undefined}
      onDoubleClick={!isBatchDeleteMode && !isQueueView ? handleToggleLog : undefined}
      draggable={!isBatchDeleteMode && isQueueView && task.status === 'pending'}
      onDragStart={!isBatchDeleteMode ? onDragStart : undefined}
      onDragOver={!isBatchDeleteMode ? onDragOver : undefined}
      onDragLeave={!isBatchDeleteMode ? onDragLeave : undefined}
      onDrop={!isBatchDeleteMode ? onDrop : undefined}
      onDragEnd={!isBatchDeleteMode ? onDragEnd : undefined}
      style={isDragging ? { opacity: 0.4 } : undefined}
      className={`px-5 py-4 rounded-xl border transition-all group shadow-sm hover:shadow-md ${isBatchDeleteMode && canBatchDelete ? 'cursor-pointer' : ''} ${isBatchDeleteMode && !canBatchDelete ? 'cursor-not-allowed opacity-60' : ''} ${!isBatchDeleteMode && isQueueView && task.status === 'pending' ? 'cursor-grab active:cursor-grabbing' : ''} ${
        isBatchDeleteMode
          ? (isSelectedForDelete ? 'bg-rose-50 !border-rose-400 ring-1 ring-rose-300 shadow-rose-500/20' : canBatchDelete ? 'bg-white border-slate-200 hover:border-rose-300 hover:bg-rose-50/30' : 'bg-slate-50 border-slate-200')
          : (isMarked ? 'bg-amber-50/40 border-amber-300 hover:border-amber-400' : 'bg-white border-slate-200 hover:border-blue-200')
      } ${dragOverPosition === 'before' ? '!border-t-4 !border-t-blue-500 !border-slate-200 !rounded-t-sm shadow-blue-500/20' : ''} ${
      dragOverPosition === 'after' ? '!border-b-4 !border-b-blue-500 !border-slate-200 !rounded-b-sm shadow-blue-500/20' : ''}`}
    >
      <div className="flex flex-col gap-3.5">
        {/* Top: Status, Name, ID & Actions */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            {!isQueueView && (
              <div className={`px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-widest border ${getStatusStyle()}`}>
                {getStatusLabel()}
              </div>
            )}
            {isQueueView && (
              task.status === 'running'
                ? <Loader2 className="w-4 h-4 text-emerald-500 animate-spin" />
                : <Clock className="w-4 h-4 text-slate-300" />
            )}
            <h5 className="font-bold text-slate-900 text-[13px] tracking-tight">{task.name}</h5>
            <span className="text-[10px] text-slate-400 font-mono">ID:{task.id}</span>
            {task.hasDependencies && task.status === 'pending' && (
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-bold bg-amber-50 text-amber-600 border border-amber-200" title={`依赖 ${task.dependencyCount} 个前置任务`}>
                <Link2 className="w-3 h-3" />
                {task.dependencyCount}
              </span>
            )}
          </div>

          {isBatchDeleteMode ? (
            <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-[10px] font-bold ${
              isSelectedForDelete
                ? 'bg-rose-500 text-white border-rose-500'
                : canBatchDelete
                  ? 'bg-white text-rose-500 border-rose-200'
                  : 'bg-slate-100 text-slate-400 border-slate-200'
            }`}>
              {isSelectedForDelete ? <CheckCircle2 className="w-3.5 h-3.5" /> : <Trash2 className="w-3.5 h-3.5" />}
              {isSelectedForDelete ? '已选择' : canBatchDelete ? '点击选择' : '运行中不可删'}
            </div>
          ) : (
            <div className="flex items-center gap-0.5">
              {!isQueueView && (
                 <>
                     {canRequeue && (
                       <button title="重新入队" onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onRequeue?.(); }} className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90">
                         <RotateCw className="w-3.5 h-3.5" />
                       </button>
                     )}
                     <button title="查看日志" onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); handleToggleLog(); }} className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90">
                       <FileText className="w-3.5 h-3.5" />
                     </button>
                 </>
              )}
                <button title="复用新建" onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onDuplicate?.(); }} className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90">
                  <Copy className="w-3.5 h-3.5" />
                </button>
	                {canEdit && (
	                  <button title={editTitle} onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onEdit?.(); }} className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90">
                    <Edit2 className="w-3.5 h-3.5" />
                  </button>
                )}
                <button
                  title={isMarked ? "取消标记" : "标记任务"}
                  onDoubleClick={(e) => e.stopPropagation()}
                  onClick={toggleMark}
                  className={`p-1.5 rounded-lg transition-all active:scale-90 ${
                    isMarked
                      ? 'text-amber-500 bg-amber-50 hover:bg-amber-100 hover:text-amber-600'
                      : 'text-slate-400 hover:text-amber-500 hover:bg-amber-50'
                  }`}
                >
                  <Bookmark className={`w-3.5 h-3.5 ${isMarked ? 'fill-current' : ''}`} />
                </button>
                <button title="删除记录" onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onDelete?.(); }} className="p-1.5 text-slate-400 hover:text-rose-500 hover:bg-rose-50 rounded-lg transition-all active:scale-90">
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
            </div>
          )}
        </div>

        {/* Middle: Command Box */}
        <div className="px-3 py-2 bg-slate-50/80 rounded-lg border border-slate-100">
           <code className="text-[10px] font-mono text-slate-600 truncate block">{task.command}</code>
        </div>

        {/* Bottom: Metadata Labels and Timestamps */}
	        <div className="flex items-center justify-between gap-4">
	          <div className="flex items-center gap-2 min-w-0 flex-1">
	             <div className="flex items-center gap-1.5 px-2 py-0.5 bg-white border border-slate-100/80 rounded text-[9px] text-slate-500 shadow-sm whitespace-nowrap">
	               <span className="text-slate-400 font-bold uppercase tracking-tight text-[8px]">环境</span>
	               <span className="font-medium text-slate-700">{task.profile || 'default'}</span>
             </div>
             <div className="flex items-center gap-1.5 px-2 py-0.5 bg-white border border-slate-100/80 rounded text-[9px] text-slate-500 shadow-sm whitespace-nowrap">
               <span className="text-slate-400 font-bold uppercase tracking-tight text-[8px]">尝试次数</span>
               <span className="font-medium text-slate-700">{task.attempts || 1}</span>
             </div>
             <div className="flex items-center gap-1.5 px-2 py-0.5 bg-white border border-slate-100/80 rounded text-[9px] text-slate-500 shadow-sm whitespace-nowrap">
               <span className="text-slate-400 font-bold uppercase tracking-tight text-[8px]">GPU</span>
               <span className="font-medium text-slate-700">{task.gpu !== undefined ? task.gpu : '-'}</span>
             </div>
             {task.gpuMemoryBudgetMb && (
               <div className="flex items-center gap-1.5 px-2 py-0.5 bg-white border border-slate-100/80 rounded text-[9px] text-slate-500 shadow-sm whitespace-nowrap">
                 <span className="text-slate-400 font-bold uppercase tracking-tight text-[8px]">预算</span>
                 <span className="font-medium text-slate-700">{(task.gpuMemoryBudgetMb / 1024).toFixed(1)}G</span>
               </div>
             )}
             <div className="flex items-center gap-1.5 px-2 py-0.5 bg-white border border-slate-100/80 rounded text-[9px] text-slate-500 shadow-sm w-full max-w-[150px] sm:max-w-xs xl:max-w-md flex-1">
	               <span className="text-slate-400 font-bold uppercase tracking-tight text-[8px] whitespace-nowrap">目录</span>
	               <span className="font-mono text-slate-700 w-full truncate">{task.workingDir || '/root'}</span>
	             </div>
	             <TaskNotesPill notes={task.notes} className="flex-1" />
	          </div>

	          {!isQueueView && (
             <div className="flex items-center gap-4 text-[9px] font-medium shrink-0">
               {attemptOptions.length > 1 && (
                 <select
                   value={String(effectiveSelectedAttempt)}
                   onClick={(e) => e.stopPropagation()}
                   onDoubleClick={(e) => e.stopPropagation()}
                   onChange={(e) => {
                     e.stopPropagation();
                     const attempt = Number(e.target.value);
                     handleSelectAttempt(Number.isFinite(attempt) ? attempt : latestAttempt);
                   }}
                   className="max-w-[120px] rounded border border-slate-200 bg-white px-2 py-1 text-[9px] font-bold text-slate-500 outline-none transition-colors hover:border-blue-200 hover:text-blue-600"
                   title="选择要查看的尝试记录"
                 >
                   {attemptOptions.map(log => {
                     const statusLabel = getAttemptStatusLabel(log.status);
                     return (
                       <option key={log.attempt} value={log.attempt}>
                         第 {log.attempt} 次{statusLabel ? ` · ${statusLabel}` : ''}
                       </option>
                     );
                   })}
                 </select>
               )}
               <div className="flex items-center gap-2 text-slate-400">
                 <span className="uppercase tracking-widest text-[8px] font-bold text-slate-300">开始</span>
                 <span className="text-slate-500 tabular-nums font-mono">{selectedStartedAt || '-'}</span>
               </div>
               <div className="flex items-center gap-2 text-slate-400">
                 <span className="uppercase tracking-widest text-[8px] font-bold text-slate-300">结束</span>
                 <span className="text-slate-500 tabular-nums font-mono">{selectedFinishedAt || '-'}</span>
               </div>
             </div>
          )}
        </div>

        {/* Expandable Log View */}
        {isLogExpanded && (
          <div className="mt-2 pt-3 border-t border-slate-100 flex flex-col">
             <div className="flex items-center justify-between px-2 bg-slate-900 rounded-t-lg py-2 border-b border-slate-700/50">
               <div className="flex items-center gap-2">
                 <Terminal className="text-emerald-500 w-3 h-3" />
                 <span className="text-slate-300 font-mono text-[9px] uppercase tracking-wider font-bold">任务 #{task.id} 运行日志</span>
               </div>
               <button
                 onClick={(e) => { e.stopPropagation(); setIsLogFullScreen(true); }}
                 className="flex items-center gap-1 bg-slate-800 hover:bg-slate-700 text-slate-300 px-2 py-1 rounded text-[10px] font-bold uppercase tracking-tighter transition-colors"
               >
                 <Maximize2 className="w-3 h-3" />
                 全屏
               </button>
             </div>
             <div className="p-3 bg-slate-900 rounded-b-lg font-mono text-[11px] text-slate-300 w-full overflow-hidden relative" style={{ height: "600px" }}>
               <TaskLogViewer
                 task={task}
                 selectedAttempt={controlledLogAttempt}
                 onSelectedAttemptChange={handleSelectAttempt}
               />
             </div>
          </div>
        )}
      </div>

      {isLogFullScreen && (
        <div className="fixed inset-0 z-[100] bg-slate-900 flex flex-col text-slate-100">
          <div className="flex items-center justify-between p-3 bg-black/40 border-b border-white/10 shrink-0">
             <div className="flex items-center gap-3">
               <Terminal className="text-emerald-500 w-4 h-4" />
               <div>
                  <h4 className="font-bold text-[13px] tracking-tight uppercase"><span className="text-slate-400">日志</span> / 任务 #{task.id}</h4>
                  <p className="text-[10px] text-slate-400 font-mono">{task.name}</p>
               </div>
             </div>
             <button
               onClick={(e) => { e.stopPropagation(); setIsLogFullScreen(false); }}
               className="p-1.5 hover:bg-white/10 rounded transition-colors text-slate-300"
             >
               <Minimize2 className="w-4 h-4" />
             </button>
          </div>
          <div className="flex-1 overflow-hidden p-6 font-mono text-[13px] relative bg-slate-900">
             <TaskLogViewer
               task={task}
               isFullScreen
               selectedAttempt={controlledLogAttempt}
               onSelectedAttemptChange={handleSelectAttempt}
             />
          </div>
        </div>
      )}
    </div>
  );
}

type TerminalStreamPayload = {
  task_id?: number;
  source?: string;
  data?: string;
  status?: string;
  exit_code?: number | null;
};

function decodeBase64Bytes(value: string) {
  const binary = window.atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

const CARRIAGE_RETURN = 13;
const LINE_FEED = 10;
const ANSI_ERASE_LINE = [27, 91, 50, 75];

function decodeTerminalBytes(
  value: string,
  pendingCarriageReturnRef: React.MutableRefObject<boolean>,
) {
  const decoded = decodeBase64Bytes(value);
  if (!decoded.length) return decoded;

  const output: number[] = [];
  let startIndex = 0;

  if (pendingCarriageReturnRef.current) {
    pendingCarriageReturnRef.current = false;
    output.push(CARRIAGE_RETURN);
    if (decoded[0] === LINE_FEED) {
      output.push(LINE_FEED);
      startIndex = 1;
    } else {
      output.push(...ANSI_ERASE_LINE);
    }
  }

  for (let index = startIndex; index < decoded.length; index += 1) {
    const byte = decoded[index];
    if (byte !== CARRIAGE_RETURN) {
      output.push(byte);
      continue;
    }

    if (index + 1 >= decoded.length) {
      pendingCarriageReturnRef.current = true;
      continue;
    }

    output.push(CARRIAGE_RETURN);
    if (decoded[index + 1] === LINE_FEED) {
      output.push(LINE_FEED);
      index += 1;
    } else {
      output.push(...ANSI_ERASE_LINE);
    }
  }

  return new Uint8Array(output);
}

function ConsoleTerminal({
  task,
  isFullScreen = false,
  onMessage,
}: {
  task: Task | null;
  isFullScreen?: boolean;
  onMessage?: (message: string) => void;
}) {
  if (!task) {
    return (
      <TerminalLog
        taskName="系统监控"
        content="系统休眠中，请选择任务查看日志..."
        isFullScreen={isFullScreen}
      />
    );
  }
  return (
    <TaskLogViewer
      task={task}
      allowLive
      isFullScreen={isFullScreen}
      onMessage={onMessage}
    />
  );
}

function TaskLogViewer({
  task,
  allowLive = false,
  isFullScreen = false,
  onMessage,
  selectedAttempt,
  onSelectedAttemptChange,
}: {
  task: Task;
  allowLive?: boolean;
  isFullScreen?: boolean;
  onMessage?: (message: string) => void;
  selectedAttempt?: number | null;
  onSelectedAttemptChange?: (attempt: number | null) => void;
}) {
  const canUseLive = allowLive && task.status === 'running';
  const [logs, setLogs] = useState<TaskLogEntry[]>([]);
  const isAttemptControlled = selectedAttempt !== undefined;
  const [internalSelectedAttempt, setInternalSelectedAttempt] = useState<number | null>(null);
  const currentSelectedAttempt = isAttemptControlled ? selectedAttempt : internalSelectedAttempt;
  const [content, setContent] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const hasLoadedRef = useRef(false);

  const selectableLogs = useMemo(
    () => logs.filter(log => !(canUseLive && log.is_current)),
    [canUseLive, logs],
  );
  const isLiveSelected = canUseLive && currentSelectedAttempt === null;
  const selectedLog = selectableLogs.find(log => log.attempt === currentSelectedAttempt) || null;

  const updateSelectedAttempt = useCallback((attempt: number | null) => {
    if (!isAttemptControlled) {
      setInternalSelectedAttempt(attempt);
    }
    onSelectedAttemptChange?.(attempt);
  }, [isAttemptControlled, onSelectedAttemptChange]);

  useEffect(() => {
    setLogs([]);
    if (!isAttemptControlled) {
      setInternalSelectedAttempt(null);
    }
    setContent(canUseLive ? '实时终端连接中...' : '正在加载日志...');
    hasLoadedRef.current = false;
  }, [task.id, task.status, canUseLive, isAttemptControlled]);

  useEffect(() => {
    let cancelled = false;
    const loadLogs = async () => {
      if (!hasLoadedRef.current && !isLiveSelected) {
        setIsLoading(true);
      }
      try {
        if (isLiveSelected) {
          const payload = await api<{ logs?: TaskLogEntry[] }>(`/api/tasks/${task.id}/logs`);
          if (!cancelled) {
            setLogs(payload.logs || []);
            setContent('实时终端连接中...');
            hasLoadedRef.current = true;
          }
          return;
        }

        const suffix = currentSelectedAttempt !== null ? `?attempt=${currentSelectedAttempt}` : '';
        const payload = await api<TaskLogPayload>(`/api/tasks/${task.id}/log${suffix}`);
        if (cancelled) return;
        const nextLogs = payload.logs || [];
        setLogs(nextLogs);
        if (currentSelectedAttempt === null && payload.selected_attempt !== null && payload.selected_attempt !== undefined) {
          updateSelectedAttempt(payload.selected_attempt);
        }
        setContent(payload.content || '(日志为空)');
        hasLoadedRef.current = true;
      } catch (error) {
        if (!cancelled) {
          setContent(error instanceof Error ? `日志加载失败: ${error.message}` : '日志加载失败');
          hasLoadedRef.current = true;
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    };

    loadLogs();
    const timer = window.setInterval(loadLogs, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [task.id, currentSelectedAttempt, isLiveSelected, updateSelectedAttempt]);

  const handleSelectAttempt = (value: string) => {
    hasLoadedRef.current = false;
    if (value === 'live') {
      updateSelectedAttempt(null);
      setContent('实时终端连接中...');
      return;
    }
    const attempt = Number(value);
    updateSelectedAttempt(Number.isFinite(attempt) ? attempt : null);
    setContent('正在加载日志...');
  };

  const handleDeleteLog = async () => {
    if (currentSelectedAttempt === null || !selectedLog) return;
    const confirmed = window.confirm(
      `确认删除任务 #${task.id} 第 ${currentSelectedAttempt} 次运行日志吗？\n\n只会删除这个日志文件，任务记录会保留。`
    );
    if (!confirmed) return;

    setIsDeleting(true);
    try {
      await api(`/api/tasks/${task.id}/logs/${currentSelectedAttempt}`, { method: 'DELETE' });
      const nextLogs = logs.filter(log => log.attempt !== currentSelectedAttempt);
      const nextSelectableLogs = nextLogs.filter(log => !(canUseLive && log.is_current));
      const nextAttempt = nextSelectableLogs.length
        ? nextSelectableLogs[nextSelectableLogs.length - 1].attempt
        : null;
      setLogs(nextLogs);
      updateSelectedAttempt(nextAttempt);
      setContent(nextAttempt === null && canUseLive ? '实时终端连接中...' : '该任务暂无可用日志。');
      hasLoadedRef.current = false;
      onMessage?.(`任务 #${task.id} 第 ${currentSelectedAttempt} 次运行日志已删除。`);
    } catch (error) {
      const text = error instanceof Error ? error.message : '删除日志失败';
      setContent(`删除日志失败: ${text}`);
      onMessage?.(text);
    } finally {
      setIsDeleting(false);
    }
  };

  const selectValue = isLiveSelected ? 'live' : (currentSelectedAttempt !== null ? String(currentSelectedAttempt) : '');
  const hasSelectableOptions = canUseLive || selectableLogs.length > 0 || currentSelectedAttempt !== null;
  const selectedLogTime = formatTime(selectedLog?.finished_at || selectedLog?.modified_at);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-2 flex flex-wrap items-center gap-2 border-b border-slate-800/80 pb-2">
        <select
          value={selectValue}
          disabled={!hasSelectableOptions}
          onChange={(event) => handleSelectAttempt(event.target.value)}
          className="max-w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[10px] font-bold text-slate-300 outline-none transition-colors hover:border-slate-500"
        >
          {canUseLive && <option value="live">实时终端</option>}
          {!hasSelectableOptions && <option value="">无日志</option>}
          {!canUseLive && selectableLogs.length === 0 && currentSelectedAttempt !== null && (
            <option value={currentSelectedAttempt}>第 {currentSelectedAttempt} 次</option>
          )}
          {selectableLogs.map(log => (
            <option key={log.attempt} value={log.attempt}>
              第 {log.attempt} 次 | {formatBytes(log.size_bytes)}
            </option>
          ))}
        </select>
        {selectedLogTime && (
          <span className="text-[10px] font-mono text-slate-500">{selectedLogTime}</span>
        )}
        <button
          type="button"
          title={currentSelectedAttempt === null ? '实时终端不能删除' : '删除当前日志'}
          disabled={currentSelectedAttempt === null || !selectedLog || isDeleting}
          onClick={handleDeleteLog}
          className={`ml-auto rounded p-1.5 transition-colors ${
            currentSelectedAttempt === null || !selectedLog || isDeleting
              ? 'cursor-not-allowed text-slate-700'
              : 'text-slate-500 hover:bg-rose-500/10 hover:text-rose-400'
          }`}
        >
          {isDeleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
        </button>
      </div>
      <div className="relative min-h-0 flex-1 overflow-hidden">
        {isLiveSelected ? (
          <LiveTerminal taskId={task.id} taskName={task.name} isFullScreen={isFullScreen} />
        ) : (
          <>
            {isLoading && (
              <div className="absolute inset-0 z-10 flex items-center justify-center rounded bg-slate-900/80 backdrop-blur-sm">
                <div className="flex items-center gap-3">
                  <Loader2 className="h-5 w-5 animate-spin text-blue-500" />
                  <span className="text-sm font-bold tracking-tight text-slate-300">正在加载日志...</span>
                </div>
              </div>
            )}
            <TerminalLog taskName={task.name} content={content || '(日志为空)'} isFullScreen={isFullScreen} />
          </>
        )}
      </div>
    </div>
  );
}

function LiveTerminal({
  taskId,
  taskName,
}: {
  taskId: string;
  taskName: string;
  isFullScreen?: boolean;
}) {
  return (
    <StreamingTerminal
      streamKey={`task-${taskId}`}
      title={taskName}
      streamUrl={`/api/tasks/${taskId}/terminal/stream`}
      resizeUrl={`/api/tasks/${taskId}/terminal/resize`}
      statusSuffix={`#${taskId}`}
      connectingMessage={`[exp-scheduler] connecting terminal stream for task #${taskId}`}
      liveStatus="实时终端"
      finishedFallback="任务已结束"
    />
  );
}

function NvitopTerminal() {
  return (
    <StreamingTerminal
      streamKey="nvitop"
      title="nvitop"
      streamUrl="/api/system/nvitop/terminal/stream"
      resizeUrl="/api/system/nvitop/terminal/resize"
      statusSuffix="nvitop"
      connectingMessage="[exp-scheduler] connecting nvitop terminal"
      liveStatus="nvitop 实时终端"
      finishedFallback="nvitop 已结束"
      normalizeCarriageReturns={false}
      scrollback={0}
      streamWithInitialSize
    />
  );
}

function StreamingTerminal({
  streamKey,
  title,
  streamUrl,
  resizeUrl,
  statusSuffix,
  connectingMessage,
  liveStatus,
  finishedFallback,
  normalizeCarriageReturns = true,
  scrollback = 5000,
  streamWithInitialSize = false,
}: {
  streamKey: string;
  title: string;
  streamUrl: string;
  resizeUrl: string;
  statusSuffix: string;
  connectingMessage: string;
  liveStatus: string;
  finishedFallback: string;
  normalizeCarriageReturns?: boolean;
  scrollback?: number;
  streamWithInitialSize?: boolean;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const resizeTimerRef = useRef<number | null>(null);
  const lastSizeKeyRef = useRef('');
  const autoFollowRef = useRef(true);
  const pendingCarriageReturnRef = useRef(false);
  const [connectionStatus, setConnectionStatus] = useState('连接中');

  const sendResize = useCallback(async () => {
    const terminal = terminalRef.current;
    if (!terminal) return;
    const cols = Math.max(2, terminal.cols || 0);
    const rows = Math.max(1, terminal.rows || 0);
    if (!cols || !rows) return;
    const sizeKey = `${streamKey}:${cols}x${rows}`;
    if (lastSizeKeyRef.current === sizeKey) return;
    try {
      await api(resizeUrl, {
        method: 'POST',
        body: JSON.stringify({ cols, rows }),
      });
      lastSizeKeyRef.current = sizeKey;
    } catch {
      // Resize is best-effort; the stream itself can keep running.
    }
  }, [resizeUrl, streamKey]);

  const fitAndResize = useCallback(() => {
    try {
      fitAddonRef.current?.fit();
    } catch {
      return;
    }
    if (resizeTimerRef.current !== null) {
      window.clearTimeout(resizeTimerRef.current);
    }
    resizeTimerRef.current = window.setTimeout(() => {
      resizeTimerRef.current = null;
      void sendResize();
    }, 120);
  }, [sendResize]);

  const streamUrlWithCurrentSize = useCallback(() => {
    const terminal = terminalRef.current;
    if (!streamWithInitialSize || !terminal) {
      return streamUrl;
    }
    try {
      fitAddonRef.current?.fit();
    } catch {
      // The stream can still start with the backend default size.
    }
    const cols = Math.max(2, terminal.cols || 0);
    const rows = Math.max(1, terminal.rows || 0);
    if (!cols || !rows) {
      return streamUrl;
    }
    const separator = streamUrl.includes('?') ? '&' : '?';
    return `${streamUrl}${separator}cols=${cols}&rows=${rows}`;
  }, [streamUrl, streamWithInitialSize]);

  const isTerminalNearBottom = (terminal: XTerm) => {
    const buffer = terminal.buffer.active;
    return buffer.baseY - buffer.viewportY <= 1;
  };

  const writePayload = useCallback((data?: string, options: { reset?: boolean } = {}) => {
    const terminal = terminalRef.current;
    if (!terminal) return;
    if (options.reset) {
      terminal.reset();
      autoFollowRef.current = true;
      pendingCarriageReturnRef.current = false;
    }
    if (!data) return;
    const shouldFollow = Boolean(options.reset || autoFollowRef.current || isTerminalNearBottom(terminal));
    try {
      const bytes = normalizeCarriageReturns
        ? decodeTerminalBytes(data, pendingCarriageReturnRef)
        : decodeBase64Bytes(data);
      terminal.write(bytes, () => {
        if (shouldFollow) {
          terminal.scrollToBottom();
        }
      });
    } catch {
      terminal.writeln('\r\n[exp-scheduler] 终端数据解析失败');
    }
  }, [normalizeCarriageReturns]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const terminal = new XTerm({
      allowProposedApi: false,
      convertEol: false,
      cursorBlink: false,
      disableStdin: true,
      fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.1,
      scrollback,
      theme: {
        background: '#0f172a',
        foreground: '#e2e8f0',
        cursor: '#f8fafc',
        selectionBackground: 'rgba(255,255,255,0.14)',
      },
    });
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(host);
    terminal.onScroll(() => {
      autoFollowRef.current = isTerminalNearBottom(terminal);
    });
    terminalRef.current = terminal;
    fitAddonRef.current = fitAddon;

    const resizeObserver = new ResizeObserver(() => {
      window.requestAnimationFrame(fitAndResize);
    });
    resizeObserver.observe(host);
    if (document.fonts?.ready) {
      document.fonts.ready.then(() => window.requestAnimationFrame(fitAndResize)).catch(() => {});
    }
    window.requestAnimationFrame(fitAndResize);

    return () => {
      if (resizeTimerRef.current !== null) {
        window.clearTimeout(resizeTimerRef.current);
        resizeTimerRef.current = null;
      }
      resizeObserver.disconnect();
      terminal.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
      lastSizeKeyRef.current = '';
      pendingCarriageReturnRef.current = false;
    };
  }, [fitAndResize, scrollback]);

  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;

    setConnectionStatus('连接中');
    terminal.reset();
    pendingCarriageReturnRef.current = false;
    terminal.writeln(connectingMessage);

    const source = new EventSource(streamUrlWithCurrentSize());

    source.addEventListener('snapshot', (event) => {
      const payload = JSON.parse(event.data) as TerminalStreamPayload;
      setConnectionStatus(liveStatus);
      writePayload(payload.data, { reset: true });
      window.requestAnimationFrame(fitAndResize);
    });

    source.addEventListener('chunk', (event) => {
      const payload = JSON.parse(event.data) as TerminalStreamPayload;
      setConnectionStatus(liveStatus);
      writePayload(payload.data);
    });

    source.addEventListener('exit', (event) => {
      const payload = JSON.parse(event.data || '{}') as TerminalStreamPayload;
      setConnectionStatus(payload.status ? `已结束: ${payload.status}` : finishedFallback);
      source.close();
    });

    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        setConnectionStatus('连接已关闭');
      } else {
        setConnectionStatus('正在重连');
      }
    };

    return () => {
      source.close();
    };
  }, [
    connectingMessage,
    finishedFallback,
    fitAndResize,
    liveStatus,
    streamUrlWithCurrentSize,
    writePayload,
  ]);

  return (
    <div className="relative h-full w-full">
      <div className="absolute right-0 top-0 z-10 rounded-bl-lg bg-slate-800/90 px-2 py-1 text-[10px] font-bold text-slate-300">
        {connectionStatus} / {statusSuffix}
      </div>
      <div className="mb-2 flex gap-2 pr-28 text-[10px] font-bold uppercase tracking-widest text-slate-500">
        <Terminal className="h-3.5 w-3.5 text-emerald-500" />
        <span className="truncate">{title}</span>
      </div>
      <div ref={hostRef} className="xterm-host h-[calc(100%-1.5rem)] w-full" />
    </div>
  );
}

function TerminalLog({ taskName, content, isFullScreen = false }: { taskName: string; content: string; isFullScreen?: boolean }) {
  return (
    <div className={`space-y-1.5 overflow-y-auto custom-scrollbar pr-2 ${isFullScreen ? 'h-full' : 'max-h-full h-full'}`}>
      <div className="flex gap-2">
        <span className="text-emerald-500 font-bold">➜</span>
        <span className="text-slate-400">root@gpu-node1:~$</span>
        <span className="text-white italic">tail -f {taskName}</span>
      </div>
      <div className="h-px bg-slate-800/50 my-2" />
      <pre className="px-6 pb-4 text-slate-300 whitespace-pre-wrap break-words">{content}</pre>
      <div className="px-6 pb-2">
        <span className="w-1.5 h-4 bg-slate-600 inline-block animate-pulse align-middle" />
      </div>
    </div>
  );
}
