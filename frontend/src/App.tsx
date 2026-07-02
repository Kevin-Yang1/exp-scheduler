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
  Archive,
  Zap,
	  Bookmark,
	  Edit2,
	  Link2,
	  Search,
	  ArrowLeftRight,
	  KeyRound,
	  Network,
	  HardDrive,
	  ChevronDown,
	  SquareTerminal,
	  Code2,
	  Bot,
	  FolderOpen,
	  Folder,
	  PlayCircle,
	  Pencil
	} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import React, { useCallback, useEffect, useMemo, useRef, useState, ReactNode, FormEvent } from 'react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';

// --- Types ---
type GpuScheduleAction = 'enable' | 'disable';
type QueueName = 'normal' | 'urgent' | 'staged';

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
  isPhysicallyIdle: boolean;
  isGloballyEnabled: boolean;
  isCoolingDown?: boolean;
  cooldownRemainingSeconds?: number;
  autoRestoreIdleSince?: string;
  autoRestoreIdleWaiting?: boolean;
  autoRestoreIdleWaitSeconds?: number;
  autoRestoreIdleRequiredSeconds?: number;
  autoRestoreIdleRemainingSeconds?: number;
}

interface Task {
  id: string;
  name: string;
  status: 'running' | 'pending' | 'staged' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
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
  gpuMemoryReservationMb?: number | null;
  profileId?: number | null;
  queueName?: QueueName;
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
  status: 'queued' | 'staged' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
  assigned_gpu?: number | null;
  requested_gpu?: number | null;
  gpu_memory_budget_mb?: number | null;
  gpu_memory_reservation_mb?: number | null;
  profile_id?: number | null;
  profile_name?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  exit_code?: number | null;
  attempt_count?: number | null;
  queue_name?: QueueName;
  depends_on?: number[];
  dependency_count?: number;
  has_dependencies?: boolean;
  attempt_logs?: TaskLogEntry[];
}

interface TaskCounts {
  queued: number;
  urgent_queued: number;
  staged: number;
  running: number;
  history: number;
  history_filtered: number;
  total: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  interrupted: number;
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
  auto_restore_idle_since?: string;
  auto_restore_idle_waiting?: boolean;
  auto_restore_idle_wait_seconds?: number;
  auto_restore_idle_required_seconds?: number;
  auto_restore_idle_remaining_seconds?: number;
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

const HISTORY_PAGE_SIZE = 100;

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

function formatDurationSeconds(value?: number | null) {
  if (value === undefined || value === null || !Number.isFinite(value)) return '--';
  const totalSeconds = Math.max(0, Math.floor(value));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function currentAutoRestoreWaitSeconds(gpu: GPUStatus, nowMs: number) {
  if (gpu.autoRestoreIdleSince) {
    const startedAt = new Date(gpu.autoRestoreIdleSince).getTime();
    if (!Number.isNaN(startedAt)) {
      return Math.max(0, (nowMs - startedAt) / 1000);
    }
  }
  return gpu.autoRestoreIdleWaitSeconds ?? 0;
}

function taskStatusLabel(status: Task['status']) {
  switch (status) {
    case 'pending': return '排队中';
    case 'staged': return '暂存';
    case 'running': return '运行中';
    case 'succeeded': return '成功';
    case 'failed': return '失败';
    case 'cancelled': return '取消';
    case 'interrupted': return '中断';
    default: return status;
  }
}

function queueLabel(queueName: QueueName) {
  switch (queueName) {
    case 'urgent': return '紧急队列';
    case 'staged': return '暂存队列';
    default: return '普通队列';
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
  const queueName = task.status === 'staged' ? 'staged' : (task.queue_name || 'normal');
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
    isUrgent: queueName === 'urgent',
    attempts: task.attempt_count || undefined,
    notes: task.notes || undefined,
    env: task.env || {},
    requestedGpu: task.requested_gpu ?? null,
    gpuMemoryBudgetMb: task.gpu_memory_budget_mb ?? null,
    gpuMemoryReservationMb: task.gpu_memory_reservation_mb ?? null,
    profileId: task.profile_id ?? null,
    queueName,
    dependsOn: task.depends_on || [],
    dependencyCount: task.dependency_count ?? (task.depends_on?.length || 0),
    hasDependencies: task.has_dependencies ?? ((task.depends_on?.length || 0) > 0),
    attemptLogs: task.attempt_logs || [],
    raw: task,
  };
}

function mapGpu(gpu: BackendGPU): GPUStatus {
  const isCoolingDown = Boolean(gpu.cooldown_until || (gpu.cooldown_remaining_seconds ?? 0) > 0);
  const isPhysicallyIdle = gpu.physically_idle ?? gpu.is_idle;
  return {
    id: gpu.index,
    name: gpu.name,
    memoryUsed: gpu.memory_used_mb || 0,
    memoryTotal: gpu.memory_total_mb || 0,
    memoryFree: gpu.memory_free_mb ?? Math.max(0, (gpu.memory_total_mb || 0) - (gpu.memory_used_mb || 0)),
    utilization: gpu.utilization_gpu || 0,
    isBusy: isCoolingDown || !isPhysicallyIdle || Boolean(gpu.scheduler_occupied || gpu.has_processes),
    isPhysicallyIdle,
    isGloballyEnabled: gpu.globally_enabled,
    isCoolingDown,
    cooldownRemainingSeconds: gpu.cooldown_remaining_seconds,
    autoRestoreIdleSince: gpu.auto_restore_idle_since,
    autoRestoreIdleWaiting: gpu.auto_restore_idle_waiting,
    autoRestoreIdleWaitSeconds: gpu.auto_restore_idle_wait_seconds,
    autoRestoreIdleRequiredSeconds: gpu.auto_restore_idle_required_seconds,
    autoRestoreIdleRemainingSeconds: gpu.auto_restore_idle_remaining_seconds,
  };
}

function normalizeGpuIds(ids: number[]) {
  return Array.from(new Set(ids)).sort((left, right) => left - right);
}

function haveSameGpuIds(left: number[], right: number[]) {
  const normalizedLeft = normalizeGpuIds(left);
  const normalizedRight = normalizeGpuIds(right);
  return normalizedLeft.length === normalizedRight.length &&
    normalizedLeft.every((id, index) => id === normalizedRight[index]);
}

type AppTab = 'dashboard' | 'queue' | 'history' | 'activity' | 'nvitop' | 'sync' | 'terminals' | 'conda' | 'backup' | 'settings';

export default function App() {
  const [activeTab, setActiveTab] = useState<AppTab>('dashboard');
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [isPaused, setIsPaused] = useState(false);
  const [showNewTask, setShowNewTask] = useState(false);
  const [isTaskModalExpanded, setIsTaskModalExpanded] = useState(false);
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
  const [historyLimit, setHistoryLimit] = useState(HISTORY_PAGE_SIZE);
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
  const [taskCounts, setTaskCounts] = useState<TaskCounts | null>(null);
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
  const [nowMs, setNowMs] = useState(() => Date.now());
  const followRunningTaskRef = useRef(true);
  const gpuSettingsDraftRef = useRef(false);
  const appliedEnabledGpusRef = useRef<number[]>([]);
  const commandTextareaRef = useRef<HTMLTextAreaElement | null>(null);

  const resetCommandTextareaSize = useCallback(() => {
    if (!commandTextareaRef.current) return;
    commandTextareaRef.current.style.height = '';
    commandTextareaRef.current.style.width = '';
  }, []);

  const toggleTaskModalExpanded = useCallback(() => {
    setIsTaskModalExpanded(prev => {
      if (prev) {
        resetCommandTextareaSize();
      }
      return !prev;
    });
  }, [resetCommandTextareaSize]);

  const runningTasks = useMemo(() => tasks.filter(t => t.status === 'running'), [tasks]);
  const historyTasks = useMemo(() => tasks.filter(t => !['running', 'pending', 'staged'].includes(t.status)), [tasks]);
  const urgentQueueTasks = useMemo(() => tasks.filter(t => t.isUrgent && t.status === 'pending'), [tasks]);
  const standardQueueTasks = useMemo(() => tasks.filter(t => !t.isUrgent && t.status === 'pending'), [tasks]);
  const stagedQueueTasks = useMemo(() => tasks.filter(t => t.status === 'staged'), [tasks]);
  const selectedTask = useMemo(() => tasks.find(t => t.id === selectedTaskId) || null, [tasks, selectedTaskId]);
  const enabledGpuSet = useMemo(() => new Set(enabledGpus), [enabledGpus]);
  const hasGpuSettingsDraft = useMemo(() => !haveSameGpuIds(enabledGpus, appliedEnabledGpus), [appliedEnabledGpus, enabledGpus]);
  const visibleGpus = useMemo(() => gpus.filter(gpu => enabledGpuSet.has(gpu.id)), [gpus, enabledGpuSet]);
  const queueDepth = taskCounts
    ? taskCounts.queued + taskCounts.urgent_queued + (taskCounts.staged || 0)
    : urgentQueueTasks.filter(t => t.status === 'pending').length + standardQueueTasks.filter(t => t.status === 'pending').length + stagedQueueTasks.length;
  const hasWaitingUrgentTask = urgentQueueTasks.some(task => task.status === 'pending');
  const totalTaskCount = taskCounts?.total ?? tasks.length;
  const runningTaskCount = taskCounts?.running ?? runningTasks.length;
  const historyTotalCount = taskCounts?.history ?? historyTasks.length;
  const failedCount = taskCounts
    ? taskCounts.failed + taskCounts.interrupted
    : historyTasks.filter(t => t.status === 'failed' || t.status === 'interrupted').length;
  const failureRate = historyTotalCount ? `${((failedCount / historyTotalCount) * 100).toFixed(1)}%` : '0%';
  const filteredHistoryTasks = useMemo(() => historyTasks.filter(t => {
    if (statusFilter === 'all') return true;
    if (statusFilter === 'marked') return markedTaskIds.has(t.id);
    return t.status === statusFilter;
  }), [historyTasks, markedTaskIds, statusFilter]);
  const historyFilteredTotalCount = taskCounts?.history_filtered ?? historyTotalCount;
  const historyLoadTargetCount = statusFilter === 'marked' ? historyTotalCount : historyFilteredTotalCount;
  const canLoadMoreHistory = historyTasks.length < historyLoadTargetCount;
  const canUseBatchDelete = activeTab === 'queue' || activeTab === 'history';
  const dependencyCandidates = useMemo<DependencyCandidate[]>(() => {
    const currentTaskId = taskDraft?.id;
    const selectedIds = new Set((taskDraft?.dependsOn || []).map(String));
    const knownCandidates: DependencyCandidate[] = tasks.filter(task =>
      task.id !== currentTaskId &&
      (task.status === 'pending' || task.status === 'staged' || task.status === 'running' || selectedIds.has(task.id))
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
  const isMetadataOnlyTaskEdit = Boolean(isEditingTask && taskDraft && taskDraft.status !== 'pending' && taskDraft.status !== 'staged');
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

  useEffect(() => {
    if (activeTab !== 'settings') return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => {
      window.clearInterval(timer);
    };
  }, [activeTab]);

  useEffect(() => {
    if (!isTaskModalExpanded) {
      resetCommandTextareaSize();
    }
  }, [isTaskModalExpanded, resetCommandTextareaSize]);

  const discardGpuSettingsDraft = useCallback(() => {
    gpuSettingsDraftRef.current = false;
    setEnabledGpus(appliedEnabledGpusRef.current);
  }, []);

  useEffect(() => {
    if (activeTab !== 'settings' || !hasGpuSettingsDraft) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest('[data-gpu-settings-draft-keep="true"]')) return;
      discardGpuSettingsDraft();
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
    };
  }, [activeTab, discardGpuSettingsDraft, hasGpuSettingsDraft]);

  const updateEnabledGpuDraft = useCallback((updater: (current: number[]) => number[]) => {
    setEnabledGpus(current => {
      const next = normalizeGpuIds(updater(current));
      const isDraft = !haveSameGpuIds(next, appliedEnabledGpusRef.current);
      gpuSettingsDraftRef.current = isDraft;
      return next;
    });
  }, []);

  const refreshAll = useCallback(async () => {
    const taskParams = new URLSearchParams({
      history_sort: historySort,
      history_limit: String(historyLimit),
    });
    if (['succeeded', 'failed', 'interrupted', 'cancelled'].includes(statusFilter)) {
      taskParams.set('history_status', statusFilter);
    }
    const [taskPayload, gpuPayload, settingsPayload, profilePayload, serverPayload, schedulerSettingsPayload] = await Promise.all([
      api<{
        queued: BackendTask[];
        urgent_queued: BackendTask[];
        staged: BackendTask[];
        running: BackendTask[];
        history: BackendTask[];
        queue_paused: boolean;
        counts?: TaskCounts;
      }>(`/api/tasks?${taskParams.toString()}`),
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
      ...(taskPayload.staged || []),
      ...(taskPayload.history || []),
    ].map(mapTask);
    setGpus(nextGpus);
    setTasks(nextTasks);
    setTaskCounts(taskPayload.counts || null);
    setProfiles(profilePayload.profiles || []);
    if (schedulerSettingsPayload) {
      setSchedulerSettings(schedulerSettingsPayload);
    }
    setIsPaused(Boolean(taskPayload.queue_paused));
    const nextEnabledGpus = normalizeGpuIds(settingsPayload.allowed_gpu_ids ?? nextGpus.map(gpu => gpu.id));
    appliedEnabledGpusRef.current = nextEnabledGpus;
    setAppliedEnabledGpus(nextEnabledGpus);
    setEnabledGpus(current => {
      if (!gpuSettingsDraftRef.current) {
        return nextEnabledGpus;
      }
      if (haveSameGpuIds(current, nextEnabledGpus)) {
        gpuSettingsDraftRef.current = false;
        return nextEnabledGpus;
      }
      return current;
    });
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
  }, [historyLimit, historySort, statusFilter]);

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
    source.addEventListener('update', (event) => {
      try {
        const parsed = JSON.parse(((event as MessageEvent).data as string) || '{}');
        const type = String(parsed?.type || '');
        // 文件同步/节点/终端相关事件由 SyncPage 自行订阅处理，
        // 高频 transfer_progress 等事件不应触发全量刷新
        if (/^(transfer_|node_|ssh_keys_|terminal_|conda_)/.test(type)) return;
      } catch {
        // 数据解析失败时退回全量刷新
      }
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
    setIsTaskModalExpanded(false);
    setShowNewTask(true);
  };

  const closeNewTaskModal = () => {
    setShowNewTask(false);
    setIsTaskModalExpanded(false);
    setTaskDraft(null);
    setIsEditingTask(false);
  };

  const openLogTask = (taskId: string) => {
    followRunningTaskRef.current = false;
    setSelectedTaskId(taskId);
    setActiveTab('dashboard');
  };

  const toggleGpu = (id: number) => {
    updateEnabledGpuDraft(prev =>
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
    if (!hasGpuSettingsDraft) return;
    const allGpuIds = gpus.map(gpu => gpu.id);
    const allowed_gpu_ids = haveSameGpuIds(enabledGpus, allGpuIds) ? null : enabledGpus;
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
    const settings = await api<{ allowed_gpu_ids: number[] | null; gpu_schedule?: Record<string, GpuScheduleEntry> }>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ allowed_gpu_ids, stop_running_gpu_ids }),
    });
    const nextEnabledGpus = normalizeGpuIds(settings.allowed_gpu_ids ?? gpus.map(gpu => gpu.id));
    gpuSettingsDraftRef.current = false;
    appliedEnabledGpusRef.current = nextEnabledGpus;
    setAppliedEnabledGpus(nextEnabledGpus);
    setEnabledGpus(nextEnabledGpus);
    setGpuSchedule(settings.gpu_schedule || {});
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
      const queue_name = String(formData.get('queue_name') || (formData.get('is_urgent') ? 'urgent' : 'normal')) as QueueName;
      const payload = {
        name,
        command: String(formData.get('command') || ''),
        cwd: String(formData.get('cwd') || '').trim() || null,
        notes,
        env: parseEnv(String(formData.get('env') || '')),
        is_urgent: queue_name === 'urgent',
        queue_name,
        requested_gpu: formData.get('requested_gpu') ? Number(formData.get('requested_gpu')) : null,
        gpu_memory_budget_mb: formData.get('gpu_memory_budget_gb') ? Math.round(Number(formData.get('gpu_memory_budget_gb')) * 1024) : null,
        gpu_memory_reservation_mb: formData.get('gpu_memory_reservation_gb') ? Math.round(Number(formData.get('gpu_memory_reservation_gb')) * 1024) : null,
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

  const interruptTaskToStaged = async (task: Task) => {
    if (!window.confirm(`确认中断任务 #${task.id} 吗？\n\n当前任务会被停止并移入暂存队列，不会继续自动调度。`)) {
      return;
    }
    try {
      await api(`/api/tasks/${task.id}/interrupt`, { method: 'POST' });
      setMessage(`任务 #${task.id} 正在中断，完成后会移入暂存队列。`);
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '任务中断失败');
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

  const moveTaskToQueue = async (taskId: string, queueName: QueueName) => {
    try {
      await api(`/api/tasks/${taskId}/queue`, {
        method: 'PATCH',
        body: JSON.stringify({ queue_name: queueName }),
      });
      setMessage(`任务 #${taskId} 已移到${queueLabel(queueName)}。`);
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '任务移动队列失败');
      await refreshAll();
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

  const handleDrop = async (e: React.DragEvent, targetQueue: QueueName, isDropZone = false) => {
    e.preventDefault();
    if (!dragState) return;
    const { id, overId, position } = dragState;
    setDragState(null);

    const task = tasks.find(t => t.id === id);
    if (!task) return;

    try {
      const targetStatus = targetQueue === 'staged' ? 'staged' : 'pending';
      if (task.queueName !== targetQueue || task.status !== targetStatus) {
         await api(`/api/tasks/${task.id}/queue`, {
             method: 'PATCH',
             body: JSON.stringify({ queue_name: targetQueue })
         });
      }

      const targetPendingTaskIdsStr = tasks
        .filter(t => targetQueue === 'staged' ? t.status === 'staged' : t.status === 'pending')
        .filter(t => {
           if (t.id === id) return false;
           return t.queueName === targetQueue;
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
    setIsTaskModalExpanded(false);
    setShowNewTask(true);
    setMessage(`正在基于任务 #${task.id} 复用新建。`);
  };

  const editTask = (task: Task) => {
    setTaskDraft(task);
    setIsEditingTask(true);
    setIsTaskModalExpanded(false);
    setShowNewTask(true);
    setMessage(task.status === 'pending' || task.status === 'staged' ? `正在重新编辑任务 #${task.id}。` : `正在编辑任务 #${task.id} 的记录信息。`);
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
            count={runningTaskCount}
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
            count={historyTotalCount}
          />
          <NavItem
            active={activeTab === 'nvitop'}
            onClick={() => setActiveTab('nvitop')}
            icon={<Terminal className="w-5 h-5" />}
            label="GPU 监控"
          />
          <NavItem
            active={activeTab === 'sync'}
            onClick={() => setActiveTab('sync')}
            icon={<ArrowLeftRight className="w-5 h-5" />}
            label="文件同步"
          />
          <NavItem
            active={activeTab === 'terminals'}
            onClick={() => setActiveTab('terminals')}
            icon={<SquareTerminal className="w-5 h-5" />}
            label="多终端"
          />
          <NavItem
            active={activeTab === 'conda'}
            onClick={() => setActiveTab('conda')}
            icon={<HardDrive className="w-5 h-5" />}
            label="Conda环境"
          />
          <NavItem
            active={activeTab === 'backup'}
            onClick={() => setActiveTab('backup')}
            icon={<Archive className="w-5 h-5" />}
            label="定时备份"
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
	              {activeTab === 'sync' && <ArrowLeftRight className="w-4 h-4" />}
	              {activeTab === 'terminals' && <SquareTerminal className="w-4 h-4" />}
	              {activeTab === 'conda' && <HardDrive className="w-4 h-4" />}
	              {activeTab === 'backup' && <Archive className="w-4 h-4" />}
	              {activeTab === 'settings' && <Settings className="w-4 h-4" />}
            </div>
            <h2 className="text-lg font-bold text-slate-900 tracking-tight">
	              {activeTab === 'dashboard' && '控制台概览'}
	              {activeTab === 'queue' && '任务队列'}
	              {activeTab === 'history' && '历史记录'}
	              {activeTab === 'activity' && '系统日志'}
	              {activeTab === 'nvitop' && 'GPU 监控'}
	              {activeTab === 'sync' && '文件同步'}
	              {activeTab === 'terminals' && '多终端'}
	              {activeTab === 'conda' && 'Conda 环境对比'}
	              {activeTab === 'backup' && '定时备份'}
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
                  <StatCard label="任务总数" value={String(totalTaskCount)} type="neutral" />
                  <StatCard label="活跃任务" value={String(runningTaskCount)} type="blue" />
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
                            onInterrupt={() => interruptTaskToStaged(task)}
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
                    <div>
                      <span className="font-bold text-slate-800 text-base">历史记录</span>
                      <p className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
                        {statusFilter === 'marked'
                          ? `已标记 ${filteredHistoryTasks.length} / 已加载 ${historyTasks.length}`
                          : `已显示 ${filteredHistoryTasks.length} / ${historyLoadTargetCount}`}
                      </p>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 flex-wrap justify-end">
                    <div className="flex items-center gap-1 bg-slate-100 p-1 rounded-lg border border-slate-200">
                      {(['finished_at', 'started_at'] as HistorySortKey[]).map((sortKey) => (
                        <button
                          key={sortKey}
                          onClick={() => {
                            setHistorySort(sortKey);
                            setHistoryLimit(HISTORY_PAGE_SIZE);
                          }}
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
                          onClick={() => {
                            setStatusFilter(filter);
                            setHistoryLimit(HISTORY_PAGE_SIZE);
                          }}
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
                  {canLoadMoreHistory && (
                    <div className="flex justify-center pt-1">
                      <button
                        onClick={() => setHistoryLimit(prev => prev + HISTORY_PAGE_SIZE)}
                        className="px-4 py-2 text-xs font-bold text-blue-600 bg-white border border-blue-100 rounded-lg hover:bg-blue-50 transition-colors"
                      >
                        加载更多 {historyTasks.length}/{historyLoadTargetCount}
                      </button>
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
                             onStage={() => moveTaskToQueue(task.id, 'staged')}
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

                {/* Waiting Queue */}
                <div className="space-y-4 bg-slate-50/50 p-5 rounded-2xl border border-slate-200">
                  <div className="flex items-center justify-between px-1">
                    <div className="flex items-center gap-2">
                       <h3 className="text-sm font-bold text-slate-800">等待队列 (Waiting)</h3>
                    </div>
                    <span className="text-[10px] font-bold text-slate-400 bg-slate-50 px-2 py-0.5 rounded border border-slate-100 uppercase">等待执行</span>
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
                             onStage={() => moveTaskToQueue(task.id, 'staged')}
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

                {/* Staged Queue */}
                <div className="space-y-4 bg-emerald-50/30 p-5 rounded-2xl border border-emerald-100">
                  <div className="flex items-center justify-between px-1">
                    <div className="flex items-center gap-2">
                       <Archive className="w-4 h-4 text-emerald-500" />
                       <h3 className="text-sm font-bold text-slate-800">暂存队列 (Staged)</h3>
                    </div>
                    <span className="text-[10px] font-bold text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-100 uppercase">暂不调度</span>
                  </div>
                  <div className="min-h-[120px] flex flex-col">
                    {stagedQueueTasks.length > 0 ? (
                       <div
                         className={`space-y-4 min-h-[120px] pb-4 transition-colors ${dragState && dragState.overId === 'empty-staged' ? 'bg-emerald-50/50' : ''}`}
                         onDragOver={(e) => { e.preventDefault(); if(e.target === e.currentTarget) setDragState(prev => prev ? { ...prev, overId: 'empty-staged', position: 'after' } : null); }}
                         onDrop={(e) => {
                           if(dragState?.overId === 'empty-staged') { handleDrop(e, 'staged', true); }
                         }}
                       >
                         {stagedQueueTasks.map(task => (
                           <HistoryRowInner
                             key={task.id}
                             task={task}
                             isQueueView={true}
                             onDuplicate={() => duplicateTask(task)}
                             onEdit={() => editTask(task)}
                             onDelete={() => deleteTask(task.id)}
                             onMoveToNormal={() => moveTaskToQueue(task.id, 'normal')}
                             onMoveToUrgent={() => moveTaskToQueue(task.id, 'urgent')}
                             onSelectLog={() => openLogTask(task.id)}
                             isMarked={markedTaskIds.has(task.id)}
                             toggleMark={(e) => { e.stopPropagation(); toggleTaskMark(task.id); }}
                             isBatchDeleteMode={isBatchDeleteMode}
                             isSelectedForDelete={selectedForDelete.has(task.id)}
                             onToggleSelectForDelete={() => toggleTaskForBatchDelete(task)}
                             isDragging={dragState?.id === task.id}
                             dragOverPosition={dragState?.overId === task.id ? dragState.position : null}
                             onDragStart={(e) => handleDragStart(e, task.id)}
                             onDragOver={(e) => handleDragOver(e, task.id, 'staged')}
                             onDrop={(e) => handleDrop(e, 'staged')}
                             onDragEnd={() => setDragState(null)}
                           />
                         ))}
                       </div>
                    ) : (
                      <div
                        onDragOver={(e) => { e.preventDefault(); setDragState(prev => prev ? { ...prev, overId: 'empty-staged', position: 'after' } : null); }}
                        onDrop={(e) => handleDrop(e, 'staged', true)}
                        className={`bg-white/50 border border-dashed rounded-xl flex-1 flex flex-col items-center justify-center p-8 transition-colors ${dragState && dragState.overId === 'empty-staged' ? 'border-emerald-400 bg-emerald-50 text-emerald-500' : 'border-slate-200 text-slate-300'}`}
                      >
                        <Archive className="w-10 h-10 mb-2 opacity-50" />
                        <p className="text-sm font-medium">暂无暂存任务，支持拖拽移入</p>
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
                            const showAutoRestoreWait = Boolean(gpu.autoRestoreIdleRequiredSeconds && !gpu.isGloballyEnabled && !enabledGpus.includes(gpu.id));
                            return (
                            <div key={gpu.id} data-gpu-settings-draft-keep="true" className={`space-y-2 rounded-xl border p-3 transition-all ${
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
                                {showAutoRestoreWait && (
                                  <GpuAutoRestoreWait gpu={gpu} nowMs={nowMs} />
                                )}
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
                          <div data-gpu-settings-draft-keep="true" className="flex w-[210px] max-w-full self-start flex-col gap-3 rounded-xl border border-emerald-100 bg-emerald-50/60 p-3">
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
                          data-gpu-settings-draft-keep="true"
                          onClick={() => updateEnabledGpuDraft(() => gpus.map(g => g.id))}
                          className="px-6 py-2.5 rounded-lg bg-slate-50 border border-slate-200 text-slate-600 text-sm font-bold hover:bg-slate-100 transition-all"
                        >
                          恢复全部可用
                        </button>
                        <button
                          data-gpu-settings-draft-keep="true"
                          type="button"
                          onClick={applyGpuSettings}
                          disabled={!hasGpuSettingsDraft}
                          className={`px-8 py-2.5 rounded-lg text-sm font-bold transition-all ${
                            hasGpuSettingsDraft
                              ? 'bg-slate-900 text-white shadow-lg shadow-slate-900/20 ring-2 ring-blue-100 hover:bg-slate-800 active:scale-95'
                              : 'bg-slate-100 text-slate-400 border border-slate-200 shadow-none cursor-not-allowed'
                          }`}
                        >
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

            {activeTab === 'sync' && (
              <motion.div
                key="sync"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="space-y-6"
              >
                <SyncPage />
              </motion.div>
            )}

            {activeTab === 'terminals' && (
              <motion.div
                key="terminals"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="space-y-6"
              >
                <MultiTerminalPage />
              </motion.div>
            )}

            {activeTab === 'conda' && (
              <motion.div
                key="conda"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="space-y-6"
              >
                <CondaPage />
              </motion.div>
            )}

            {activeTab === 'backup' && (
              <motion.div
                key="backup"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="space-y-6"
              >
                <BackupPage />
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
              className={`relative w-full bg-white border border-slate-200 rounded-2xl shadow-2xl overflow-hidden flex flex-col transition-all ${
                isTaskModalExpanded
                  ? 'max-w-7xl max-h-[calc(100vh-3rem)]'
                  : 'max-w-4xl'
              }`}
            >
              <div className="shrink-0 px-8 py-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between">
                <div>
                  <h2 className="text-lg font-bold text-slate-900 tracking-tight">{isMetadataOnlyTaskEdit ? '编辑记录信息' : taskDraft ? (isEditingTask ? '编辑任务' : '复用任务创建新任务') : '创建新运行时任务'}</h2>
                  <p className="text-xs text-slate-500 font-medium">{isMetadataOnlyTaskEdit ? `任务 #${taskDraft?.id} 的名称和备注` : taskDraft ? (isEditingTask ? `配置任务 #${taskDraft.id} 并提交更新` : `已填入任务 #${taskDraft.id} 的参数，可修改后提交`) : '配置参数并提交给调度系统'}</p>
                </div>
                <button onClick={closeNewTaskModal} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-900 transition-colors">
                  <Plus className="w-5 h-5 rotate-45" />
                </button>
              </div>

              <form
                key={taskDraft?.id || 'new'}
                onSubmit={submitTask}
                className={`px-8 py-5 space-y-4 overflow-y-auto custom-scrollbar ${
                  isTaskModalExpanded ? 'max-h-[calc(100vh-9rem)]' : 'max-h-[80vh]'
                }`}
              >
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
                        <div className="grid grid-cols-3 gap-1 bg-slate-100 border border-slate-200 rounded-lg p-1">
                          {([
                            ['normal', '普通'],
                            ['urgent', '紧急'],
                            ['staged', '暂存'],
                          ] as [QueueName, string][]).map(([queueName, label]) => {
                            const selectedQueue = taskDraft?.queueName || (taskDraft?.isUrgent ? 'urgent' : 'normal');
                            return (
                              <label key={queueName} className="cursor-pointer">
                                <input
                                  type="radio"
                                  name="queue_name"
                                  value={queueName}
                                  defaultChecked={selectedQueue === queueName}
                                  className="peer sr-only"
                                />
                                <span className="block rounded-md px-2 py-1.5 text-center text-[11px] font-bold text-slate-500 transition-all peer-checked:bg-white peer-checked:text-blue-600 peer-checked:shadow-sm">
                                  {label}
                                </span>
                              </label>
                            );
                          })}
                        </div>
                      </div>

                      {/* GPU Memory Budget */}
                      <div className="space-y-1.5">
                        <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">显存预算 (GB)</label>
                        <input
                          type="number"
                          min="0"
                          step="any"
                          name="gpu_memory_budget_gb"
                          defaultValue={taskDraft?.gpuMemoryBudgetMb ? taskDraft.gpuMemoryBudgetMb / 1024 : ''}
                          placeholder="不填写则使用默认空闲阈值"
                          className="w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors"
                        />
                      </div>

                      <div className="space-y-1.5">
                        <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">显存预留 (GB)</label>
                        <input
                          type="number"
                          min="0"
                          step="any"
                          name="gpu_memory_reservation_gb"
                          defaultValue={taskDraft?.gpuMemoryReservationMb ? taskDraft.gpuMemoryReservationMb / 1024 : ''}
                          placeholder="可选，启动前临时占用"
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
                      size={Math.min(isTaskModalExpanded ? 6 : 4, Math.max(2, dependencyCandidates.length))}
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
                      <div className="relative">
                        <textarea
                          ref={commandTextareaRef}
                          rows={isTaskModalExpanded ? 18 : 4}
                          name="command"
                          defaultValue={taskDraft?.command || ''}
                          placeholder="python main.py --model llama --dataset sft..."
                          required
                          className={`w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 pr-12 text-slate-900 font-mono placeholder-slate-400 outline-none focus:border-blue-500 transition-colors ${
                            isTaskModalExpanded
                              ? 'min-h-[420px] text-[12px] leading-5 resize-y'
                              : 'min-h-[96px] text-[11px] resize-none'
                          }`}
                          spellCheck={false}
                        />
                        <button
                          type="button"
                          onClick={toggleTaskModalExpanded}
                          title={isTaskModalExpanded ? '还原窗口' : '放大窗口'}
                          className="absolute right-2 top-2 z-10 rounded-md border border-slate-200 bg-white/90 p-1.5 text-slate-400 shadow-sm transition-colors hover:bg-slate-50 hover:text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                        >
                          {isTaskModalExpanded ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
                        </button>
                      </div>
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
                        rows={isTaskModalExpanded ? 3 : 1}
                        name="env"
                        defaultValue={envToText(taskDraft?.env)}
                        placeholder="WANDB_MODE=offline"
                        className={`w-full bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-slate-900 font-mono text-[11px] placeholder-slate-400 outline-none focus:border-blue-500 transition-colors ${
                          isTaskModalExpanded ? 'resize-y' : 'resize-none'
                        }`}
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

function GpuAutoRestoreWait({ gpu, nowMs }: { gpu: GPUStatus; nowMs: number }) {
  const requiredSeconds = gpu.autoRestoreIdleRequiredSeconds ?? 0;
  const waitSeconds = gpu.autoRestoreIdleWaiting ? currentAutoRestoreWaitSeconds(gpu, nowMs) : 0;
  const remainingSeconds = requiredSeconds > 0 ? Math.max(0, requiredSeconds - waitSeconds) : gpu.autoRestoreIdleRemainingSeconds;
  const progress = requiredSeconds > 0 ? Math.min(100, (waitSeconds / requiredSeconds) * 100) : 0;
  const statusText = gpu.autoRestoreIdleWaiting ? '还需' : '等待空闲';
  const detailText = gpu.autoRestoreIdleWaiting
    ? `${statusText} ${formatDurationSeconds(remainingSeconds)}`
    : '空闲后开始计时';

  return (
    <div className="rounded-lg border border-emerald-100 bg-white/80 px-2 py-1.5 text-emerald-800">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-bold">已等待</span>
        <span className="font-mono text-[12px] font-black tabular-nums">
          {formatDurationSeconds(waitSeconds)}
        </span>
      </div>
      <div className="mt-1 h-1 rounded-full bg-emerald-100 overflow-hidden">
        <div
          className="h-full rounded-full bg-emerald-500 transition-all"
          style={{ width: `${progress}%` }}
        />
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-[9px] font-bold text-emerald-700/80">
        <span>{detailText}</span>
        {requiredSeconds > 0 && <span className="font-mono tabular-nums">{formatDurationSeconds(requiredSeconds)}</span>}
      </div>
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

  const TaskCardInner = ({ task, isSelected, onSelect, onCancel, onInterrupt, onPreempt, canPreempt, onDuplicate, onEdit, isMarked, toggleMark }: { task: Task; isSelected?: boolean; onSelect?: () => void; onCancel?: () => void; onInterrupt?: () => void; onPreempt?: () => void; canPreempt?: boolean; onDuplicate?: () => void; onEdit?: () => void; isMarked?: boolean; toggleMark?: (e: React.MouseEvent) => void; key?: React.Key }) => {
  const canEdit = Boolean(onEdit);
  const editTitle = task.status === 'pending' || task.status === 'staged' ? '编辑任务' : '编辑记录信息';

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
              {task.gpuMemoryReservationMb && (
                <>
                  <span>•</span>
                  <span>预留 {(task.gpuMemoryReservationMb / 1024).toFixed(1)}G</span>
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
                title="中断到暂存队列"
                onClick={(e) => {
                  e.stopPropagation();
                  onInterrupt?.();
                }}
                className="p-1.5 text-slate-400 hover:text-emerald-600 hover:bg-emerald-50 rounded-lg transition-all active:scale-90"
              >
                <Pause className="w-3.5 h-3.5" />
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
                title="取消任务"
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
	        <code
	          className={`text-[10px] font-mono block whitespace-pre-wrap break-words leading-relaxed ${isSelected ? 'text-slate-800' : 'text-slate-600'}`}
	          style={{ overflowWrap: 'anywhere' }}
	        >
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
  onStage,
  onMoveToNormal,
  onMoveToUrgent,
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
  onStage?: () => void;
  onMoveToNormal?: () => void;
  onMoveToUrgent?: () => void;
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
	  const editTitle = task.status === 'pending' || task.status === 'staged' ? '编辑任务' : '编辑记录信息';
  const canBatchDelete = task.status !== 'running';
  const attemptOptions = useMemo(() => task.attemptLogs || [], [task.attemptLogs]);
  const latestAttempt = attemptOptions.length > 0
    ? attemptOptions[attemptOptions.length - 1].attempt
    : (task.attempts || 1);
  const canViewLogs = task.status === 'running' || attemptOptions.length > 0 || Boolean(task.attempts && task.attempts > 0);

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
      case 'staged': return 'bg-emerald-50 text-emerald-700 border-emerald-100';
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
      case 'staged': return '暂存';
      case 'pending': return '待排队';
      default: return task.status;
    }
  };

  return (
    <div
      onClick={isBatchDeleteMode && canBatchDelete ? onToggleSelectForDelete : undefined}
      onDoubleClick={!isBatchDeleteMode && !isQueueView ? handleToggleLog : undefined}
      draggable={!isBatchDeleteMode && isQueueView && (task.status === 'pending' || task.status === 'staged')}
      onDragStart={!isBatchDeleteMode ? onDragStart : undefined}
      onDragOver={!isBatchDeleteMode ? onDragOver : undefined}
      onDragLeave={!isBatchDeleteMode ? onDragLeave : undefined}
      onDrop={!isBatchDeleteMode ? onDrop : undefined}
      onDragEnd={!isBatchDeleteMode ? onDragEnd : undefined}
      style={isDragging ? { opacity: 0.4 } : undefined}
      className={`px-5 py-4 rounded-xl border transition-all group shadow-sm hover:shadow-md ${isBatchDeleteMode && canBatchDelete ? 'cursor-pointer' : ''} ${isBatchDeleteMode && !canBatchDelete ? 'cursor-not-allowed opacity-60' : ''} ${!isBatchDeleteMode && isQueueView && (task.status === 'pending' || task.status === 'staged') ? 'cursor-grab active:cursor-grabbing' : ''} ${
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
                : task.status === 'staged'
                  ? <Archive className="w-4 h-4 text-emerald-500" />
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
              {isQueueView && task.status === 'staged' && (
                <>
                  <button title="加入普通队列" onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onMoveToNormal?.(); }} className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90">
                    <Layers className="w-3.5 h-3.5" />
                  </button>
                  <button title="加入紧急队列" onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onMoveToUrgent?.(); }} className="p-1.5 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-lg transition-all active:scale-90">
                    <Zap className="w-3.5 h-3.5" />
                  </button>
                </>
              )}
              {isQueueView && task.status === 'pending' && (
                <button title="移入暂存队列" onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onStage?.(); }} className="p-1.5 text-slate-400 hover:text-emerald-600 hover:bg-emerald-50 rounded-lg transition-all active:scale-90">
                  <Archive className="w-3.5 h-3.5" />
                </button>
              )}
              {(!isQueueView || canViewLogs) && (
                 <>
                     {!isQueueView && canRequeue && (
                       <button title="重新入队" onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onRequeue?.(); }} className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90">
                         <RotateCw className="w-3.5 h-3.5" />
                       </button>
                     )}
                     <button title={isLogExpanded ? "收起日志" : "查看日志"} onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); handleToggleLog(); }} className="p-1.5 text-slate-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all active:scale-90">
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
           <code
             className="text-[10px] font-mono text-slate-600 whitespace-pre-wrap break-words leading-relaxed block"
             style={{ overflowWrap: 'anywhere' }}
           >
             {task.command}
           </code>
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
             {task.gpuMemoryReservationMb && (
               <div className="flex items-center gap-1.5 px-2 py-0.5 bg-white border border-slate-100/80 rounded text-[9px] text-slate-500 shadow-sm whitespace-nowrap">
                 <span className="text-slate-400 font-bold uppercase tracking-tight text-[8px]">预留</span>
                 <span className="font-medium text-slate-700">{(task.gpuMemoryReservationMb / 1024).toFixed(1)}G</span>
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
const ESCAPE = 27;
const DEFAULT_TERMINAL_COLUMNS = 160;
const ANSI_ERASE_LINE = [27, 91, 50, 75];
const ANSI_CURSOR_UP = [27, 91, 49, 65];

type AnsiParseState = 'normal' | 'escape' | 'csi' | 'osc' | 'oscEscape';

function wrappedRowCount(cellWidth: number, columns: number) {
  const safeColumns = Math.max(1, columns || DEFAULT_TERMINAL_COLUMNS);
  const safeWidth = Math.max(1, cellWidth);
  return Math.floor((safeWidth - 1) / safeColumns) + 1;
}

function pushEraseWrappedLine(output: number[], rows: number) {
  output.push(CARRIAGE_RETURN, ...ANSI_ERASE_LINE);
  for (let row = 1; row < rows; row += 1) {
    output.push(...ANSI_CURSOR_UP, CARRIAGE_RETURN, ...ANSI_ERASE_LINE);
  }
}

function trackTerminalCellWidth(
  byte: number,
  lineCellWidthRef: React.MutableRefObject<number>,
  ansiParseStateRef: React.MutableRefObject<AnsiParseState>,
) {
  const state = ansiParseStateRef.current;
  if (state === 'escape') {
    if (byte === 91) {
      ansiParseStateRef.current = 'csi';
    } else if (byte === 93) {
      ansiParseStateRef.current = 'osc';
    } else {
      ansiParseStateRef.current = 'normal';
    }
    return;
  }
  if (state === 'csi') {
    if (byte >= 64 && byte <= 126) {
      ansiParseStateRef.current = 'normal';
    }
    return;
  }
  if (state === 'osc') {
    if (byte === 7) {
      ansiParseStateRef.current = 'normal';
    } else if (byte === ESCAPE) {
      ansiParseStateRef.current = 'oscEscape';
    }
    return;
  }
  if (state === 'oscEscape') {
    ansiParseStateRef.current = byte === 92 ? 'normal' : 'osc';
    return;
  }

  if (byte === ESCAPE) {
    ansiParseStateRef.current = 'escape';
  } else if (byte === LINE_FEED) {
    lineCellWidthRef.current = 0;
  } else if (byte === 9) {
    lineCellWidthRef.current += 4;
  } else if (byte === 8) {
    lineCellWidthRef.current = Math.max(0, lineCellWidthRef.current - 1);
  } else if (byte >= 32 && byte !== 127 && (byte < 128 || byte >= 192)) {
    lineCellWidthRef.current += 1;
  }
}

function decodeTerminalBytes(
  value: string,
  pendingCarriageReturnRef: React.MutableRefObject<boolean>,
  lineCellWidthRef: React.MutableRefObject<number>,
  ansiParseStateRef: React.MutableRefObject<AnsiParseState>,
  columns: number,
) {
  const decoded = decodeBase64Bytes(value);
  if (!decoded.length) return decoded;

  const output: number[] = [];
  let startIndex = 0;

  if (pendingCarriageReturnRef.current) {
    pendingCarriageReturnRef.current = false;
    if (decoded[0] === LINE_FEED) {
      output.push(CARRIAGE_RETURN, LINE_FEED);
      lineCellWidthRef.current = 0;
      ansiParseStateRef.current = 'normal';
      startIndex = 1;
    } else {
      pushEraseWrappedLine(output, wrappedRowCount(lineCellWidthRef.current, columns));
      lineCellWidthRef.current = 0;
      ansiParseStateRef.current = 'normal';
    }
  }

  for (let index = startIndex; index < decoded.length; index += 1) {
    const byte = decoded[index];
    if (byte !== CARRIAGE_RETURN) {
      output.push(byte);
      trackTerminalCellWidth(byte, lineCellWidthRef, ansiParseStateRef);
      continue;
    }

    if (index + 1 >= decoded.length) {
      pendingCarriageReturnRef.current = true;
      continue;
    }

    if (decoded[index + 1] === LINE_FEED) {
      output.push(CARRIAGE_RETURN, LINE_FEED);
      lineCellWidthRef.current = 0;
      ansiParseStateRef.current = 'normal';
      index += 1;
    } else {
      pushEraseWrappedLine(output, wrappedRowCount(lineCellWidthRef.current, columns));
      lineCellWidthRef.current = 0;
      ansiParseStateRef.current = 'normal';
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
  const [loadFullLogRequested, setLoadFullLogRequested] = useState(false);
  const loadFullLog = isFullScreen || loadFullLogRequested;
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
    setLoadFullLogRequested(false);
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

        const params = new URLSearchParams();
        if (currentSelectedAttempt !== null) {
          params.set('attempt', String(currentSelectedAttempt));
        }
        if (loadFullLog) {
          params.set('full', '1');
        }
        const suffix = params.toString() ? `?${params.toString()}` : '';
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
    const shouldPoll = isLiveSelected || !loadFullLog || task.status === 'running';
    const timer = shouldPoll ? window.setInterval(loadLogs, 2000) : null;
    return () => {
      cancelled = true;
      if (timer !== null) {
        window.clearInterval(timer);
      }
    };
  }, [task.id, task.status, currentSelectedAttempt, isLiveSelected, loadFullLog, updateSelectedAttempt]);

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

  const handleToggleFullLog = () => {
    hasLoadedRef.current = false;
    setLoadFullLogRequested(prev => !prev);
    setContent(isLiveSelected ? '实时终端连接中...' : '正在加载日志...');
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
        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            title={loadFullLog ? '当前加载完整日志' : '加载完整日志'}
            disabled={isFullScreen}
            onClick={handleToggleFullLog}
            className={`inline-flex items-center gap-1 rounded px-1.5 py-1 text-[10px] font-bold transition-colors ${
              loadFullLog
                ? 'bg-emerald-500/10 text-emerald-400'
                : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'
            } ${isFullScreen ? 'cursor-default' : ''}`}
          >
            <FileText className="h-3.5 w-3.5" />
            <span>{loadFullLog ? '完整' : '尾部'}</span>
          </button>
          <button
            type="button"
            title={currentSelectedAttempt === null ? '实时终端不能删除' : '删除当前日志'}
            disabled={currentSelectedAttempt === null || !selectedLog || isDeleting}
            onClick={handleDeleteLog}
            className={`rounded p-1.5 transition-colors ${
              currentSelectedAttempt === null || !selectedLog || isDeleting
                ? 'cursor-not-allowed text-slate-700'
                : 'text-slate-500 hover:bg-rose-500/10 hover:text-rose-400'
            }`}
          >
            {isDeleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
          </button>
        </div>
      </div>
      <div className="relative min-h-0 flex-1 overflow-hidden">
        {isLiveSelected ? (
          <LiveTerminal
            taskId={task.id}
            taskName={task.name}
            isFullScreen={isFullScreen}
            loadFullSnapshot={loadFullLog}
          />
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
  isFullScreen = false,
  loadFullSnapshot = false,
}: {
  taskId: string;
  taskName: string;
  isFullScreen?: boolean;
  loadFullSnapshot?: boolean;
}) {
  const streamUrl = `/api/tasks/${taskId}/terminal/stream${isFullScreen || loadFullSnapshot ? '?full=1' : ''}`;
  return (
    <StreamingTerminal
      streamKey={`task-${taskId}`}
      title={taskName}
      streamUrl={streamUrl}
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
  scrollback = 20000,
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
  const lineCellWidthRef = useRef(0);
  const ansiParseStateRef = useRef<AnsiParseState>('normal');
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
      lineCellWidthRef.current = 0;
      ansiParseStateRef.current = 'normal';
    }
    if (!data) return;
    const shouldFollow = Boolean(options.reset || autoFollowRef.current || isTerminalNearBottom(terminal));
    try {
      const bytes = normalizeCarriageReturns
        ? decodeTerminalBytes(
            data,
            pendingCarriageReturnRef,
            lineCellWidthRef,
            ansiParseStateRef,
            terminal.cols || DEFAULT_TERMINAL_COLUMNS,
          )
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
      lineCellWidthRef.current = 0;
      ansiParseStateRef.current = 'normal';
    };
  }, [fitAndResize, scrollback]);

  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;

    setConnectionStatus('连接中');
    terminal.reset();
    pendingCarriageReturnRef.current = false;
    lineCellWidthRef.current = 0;
    ansiParseStateRef.current = 'normal';
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

// ==================== 文件同步页（节点注册表 + 传输任务 + SSH 密钥库 + 连通性矩阵） ====================

interface SyncNode {
  id: string;
  name: string;
  is_local?: boolean;
  host?: string | null;
  ssh_port?: number;
  username?: string | null;
  auth_method?: 'key' | 'password' | null;
  ssh_key_id?: string | null;
  has_password?: boolean;
  notes?: string | null;
  rsync_version?: string | null;
  has_sshpass?: boolean | null;
  tcp_forward_ok?: boolean | null;
  agent_forward_ok?: boolean | null;
}

interface SshKeyInfo {
  id: string;
  name: string;
  kind?: string | null;
  key_path?: string | null;
  fingerprint?: string | null;
  notes?: string | null;
  created_at?: string | null;
}

interface NodeLinkInfo {
  from_node_id: string;
  to_node_id: string;
  status: 'unknown' | 'ok' | 'failed';
  latency_ms?: number | null;
  last_probe_at?: string | null;
  last_error?: string | null;
  applicable?: boolean;
}

interface TransferJob {
  id: string;
  name?: string | null;
  src_node_id: string;
  src_path: string;
  dst_node_id: string;
  dst_path: string;
  route?: string | null;
  route_resolved_by?: string | null;
  route_attempts?: unknown[];
  rsync_args?: string[];
  delete_extras?: boolean;
  dry_run?: boolean;
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
  phase?: string | null;
  progress_percent?: number | null;
  bytes_transferred?: number | null;
  transfer_rate?: string | null;
  eta?: string | null;
  files_transferred?: number | null;
  exit_code?: number | null;
  error?: string | null;
  error_code?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

interface RouteCandidate {
  route: string;
  feasible: boolean;
  reasons: string[];
  requires_probe: string[][];
}

interface TransferPlan {
  candidates: RouteCandidate[];
  recommended: string | null;
  needs_probe: boolean;
}

interface TransferFormDraft {
  name: string;
  src_node_id: string;
  src_path: string;
  dst_node_id: string;
  dst_path: string;
  src_contents_only: boolean;
  compress: boolean;
  bwlimit: string;
  excludes: string[];
  extraArgs: string;
  dry_run: boolean;
  delete_extras: boolean;
  route: string;
}

const EMPTY_TRANSFER_DRAFT: TransferFormDraft = {
  name: '',
  src_node_id: 'local',
  src_path: '',
  dst_node_id: '',
  dst_path: '',
  src_contents_only: false,
  compress: false,
  bwlimit: '',
  excludes: [],
  extraArgs: '',
  dry_run: false,
  delete_extras: false,
  route: 'auto',
};

function syncNodeOptionLabel(node: SyncNode) {
  return node.is_local ? `本机(${node.name})` : node.name;
}

function transferStatusLabel(status: TransferJob['status']) {
  switch (status) {
    case 'pending': return '排队中';
    case 'running': return '传输中';
    case 'succeeded': return '成功';
    case 'failed': return '失败';
    case 'cancelled': return '取消';
    case 'interrupted': return '中断';
    default: return status;
  }
}

function transferStatusStyle(status: TransferJob['status']) {
  switch (status) {
    case 'running': return 'bg-blue-50 text-blue-600 border-blue-100';
    case 'pending': return 'bg-amber-50 text-amber-700 border-amber-100';
    case 'succeeded': return 'bg-emerald-50 text-emerald-700 border-emerald-100';
    case 'failed': return 'bg-rose-50 text-rose-700 border-rose-100';
    case 'interrupted': return 'bg-amber-50 text-amber-700 border-amber-100';
    default: return 'bg-slate-50 text-slate-500 border-slate-200';
  }
}

function transferRouteLabel(route?: string | null) {
  switch (route) {
    case 'local': return '本机复制';
    case 'direct_from_src': return '直连·源端发起';
    case 'direct_from_dst': return '直连·目标端拉取';
    case 'bridged_push': return '桥接·源端经主控推送';
    case 'bridged_pull': return '桥接·目标端经主控拉取';
    case 'auto': return '自动';
    default: return route || '自动';
  }
}

function transferRouteBadgeText(job: TransferJob, nodeNames: Record<string, string>) {
  const src = nodeNames[job.src_node_id] || job.src_node_id;
  const dst = nodeNames[job.dst_node_id] || job.dst_node_id;
  switch (job.route) {
    case 'local': return '本机内复制';
    case 'bridged_push':
    case 'bridged_pull':
      return `${src} ⇒(经主控)⇒ ${dst}`;
    default:
      return `${src} ⇒ ${dst}`;
  }
}

function transferDurationText(job: TransferJob) {
  if (!job.started_at) return '--';
  const start = new Date(job.started_at).getTime();
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return '--';
  return formatDurationSeconds(Math.max(0, (end - start) / 1000));
}

function capabilityMark(value?: boolean | null) {
  if (value === true) return <span className="text-emerald-600 font-bold">✓</span>;
  if (value === false) return <span className="text-rose-500 font-bold">✕</span>;
  return <span className="text-slate-300 font-bold">?</span>;
}

function SyncPage() {
  const [nodes, setNodes] = useState<SyncNode[]>([]);
  const [sshKeys, setSshKeys] = useState<SshKeyInfo[]>([]);
  const [links, setLinks] = useState<NodeLinkInfo[]>([]);
  const [linksProbing, setLinksProbing] = useState(false);
  const [activeJobs, setActiveJobs] = useState<TransferJob[]>([]);
  const [historyJobs, setHistoryJobs] = useState<TransferJob[]>([]);
  const [maxConcurrent, setMaxConcurrent] = useState<number | null>(null);
  const [showTransferForm, setShowTransferForm] = useState(false);
  const [transferDraft, setTransferDraft] = useState<TransferFormDraft | null>(null);
  const [logJob, setLogJob] = useState<TransferJob | null>(null);
  const [nodeModal, setNodeModal] = useState<{ node: SyncNode | null } | null>(null);
  const [showKeyModal, setShowKeyModal] = useState(false);
  const [testingNodeId, setTestingNodeId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; kind: 'info' | 'success' | 'error' } | null>(null);

  const showNotice = useCallback((text: string, kind: 'info' | 'success' | 'error' = 'info') => {
    setNotice({ text, kind });
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 8000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const loadNodes = useCallback(async () => {
    const payload = await api<{ nodes: SyncNode[] }>('/api/nodes');
    setNodes(payload.nodes || []);
  }, []);

  const loadSshKeys = useCallback(async () => {
    const payload = await api<{ keys: SshKeyInfo[] }>('/api/ssh-keys');
    setSshKeys(payload.keys || []);
  }, []);

  const loadLinks = useCallback(async () => {
    const payload = await api<{ links: NodeLinkInfo[]; probing?: boolean }>('/api/nodes/links');
    setLinks(payload.links || []);
    setLinksProbing(Boolean(payload.probing));
  }, []);

  const loadTransfers = useCallback(async () => {
    const payload = await api<{ active: TransferJob[]; history: TransferJob[] }>('/api/transfers');
    setActiveJobs(payload.active || []);
    setHistoryJobs(payload.history || []);
  }, []);

  const loadTransferSettings = useCallback(async () => {
    const payload = await api<{ max_concurrent_transfers?: number }>('/api/transfers/settings');
    setMaxConcurrent(payload.max_concurrent_transfers ?? null);
  }, []);

  const refreshSyncData = useCallback(async () => {
    await Promise.all([loadNodes(), loadSshKeys(), loadLinks(), loadTransfers(), loadTransferSettings()]);
  }, [loadNodes, loadSshKeys, loadLinks, loadTransfers, loadTransferSettings]);

  useEffect(() => {
    refreshSyncData().catch(error => showNotice((error as Error).message, 'error'));
    const source = new EventSource('/api/events');
    source.addEventListener('update', (event) => {
      let parsed: { type?: string; payload?: Record<string, unknown> };
      try {
        parsed = JSON.parse(((event as MessageEvent).data as string) || '{}');
      } catch {
        return;
      }
      const type = String(parsed?.type || '');
      const payload = (parsed?.payload || {}) as Record<string, unknown>;
      if (type === 'transfer_progress') {
        // 进度事件直接 merge 到对应活跃卡片，不触发全量刷新
        const jobId = String(payload.job_id || '');
        setActiveJobs(prev => prev.map(job => job.id === jobId
          ? {
              ...job,
              status: 'running',
              phase: payload.phase != null ? String(payload.phase) : job.phase,
              progress_percent: typeof payload.percent === 'number' ? payload.percent : job.progress_percent,
              bytes_transferred: typeof payload.bytes === 'number' ? payload.bytes : job.bytes_transferred,
              transfer_rate: payload.rate != null ? String(payload.rate) : job.transfer_rate,
              eta: payload.eta != null ? String(payload.eta) : job.eta,
            }
          : job));
        return;
      }
      if (['transfer_created', 'transfer_started', 'transfer_finished', 'transfer_deleted'].includes(type)) {
        loadTransfers().catch(() => {});
        return;
      }
      if (type === 'transfer_settings_updated') {
        loadTransferSettings().catch(() => {});
        return;
      }
      if (type === 'node_link_updated') {
        const link = payload.link as NodeLinkInfo | undefined;
        if (link && link.from_node_id && link.to_node_id) {
          setLinks(prev => [
            ...prev.filter(item => !(item.from_node_id === link.from_node_id && item.to_node_id === link.to_node_id)),
            link,
          ]);
        }
        return;
      }
      if (type === 'node_links_probe_started') {
        setLinksProbing(true);
        return;
      }
      if (type === 'node_links_probe_finished') {
        setLinksProbing(false);
        loadLinks().catch(() => {});
        return;
      }
      if (type === 'node_updated') {
        loadNodes().catch(() => {});
        loadLinks().catch(() => {});
        return;
      }
      if (type === 'ssh_keys_updated') {
        loadSshKeys().catch(() => {});
      }
    });
    source.onerror = () => {};
    // SSE 断线兜底：低频轮询传输列表
    const timer = window.setInterval(() => {
      loadTransfers().catch(() => {});
    }, 15000);
    return () => {
      source.close();
      window.clearInterval(timer);
    };
  }, [refreshSyncData, loadNodes, loadSshKeys, loadLinks, loadTransfers, loadTransferSettings, showNotice]);

  const nodeNames = useMemo(() => {
    const map: Record<string, string> = {};
    nodes.forEach(node => {
      map[node.id] = node.is_local ? '本机' : node.name;
    });
    return map;
  }, [nodes]);

  const remoteNodes = useMemo(() => nodes.filter(node => !node.is_local), [nodes]);
  const okLinkCount = useMemo(() => links.filter(link => link.status === 'ok').length, [links]);
  const failedLinkCount = useMemo(() => links.filter(link => link.status === 'failed').length, [links]);

  const cancelJob = async (job: TransferJob) => {
    if (!window.confirm(`确定取消传输任务「${job.name || job.id}」？`)) return;
    try {
      await api(`/api/transfers/${job.id}/cancel`, { method: 'POST' });
      await loadTransfers();
      showNotice('已请求取消传输任务', 'info');
    } catch (error) {
      showNotice((error as Error).message, 'error');
    }
  };

  const deleteJob = async (job: TransferJob) => {
    if (!window.confirm(`确定删除传输记录「${job.name || job.id}」？日志文件将一并清除。`)) return;
    try {
      await api(`/api/transfers/${job.id}`, { method: 'DELETE' });
      await loadTransfers();
    } catch (error) {
      showNotice((error as Error).message, 'error');
    }
  };

  const retryJob = (job: TransferJob) => {
    const args = (job.rsync_args || []).map(String);
    const excludes = args
      .filter(arg => arg.startsWith('--exclude='))
      .map(arg => arg.slice('--exclude='.length));
    const bwlimitArg = args.find(arg => arg.startsWith('--bwlimit='));
    const rest = args.filter(arg =>
      arg !== '-z' && arg !== '--compress' && !arg.startsWith('--exclude=') && !arg.startsWith('--bwlimit=')
    );
    setTransferDraft({
      name: job.name || '',
      src_node_id: job.src_node_id,
      src_path: job.src_path,
      dst_node_id: job.dst_node_id,
      dst_path: job.dst_path,
      src_contents_only: job.src_path.length > 1 && job.src_path.endsWith('/'),
      compress: args.includes('-z') || args.includes('--compress'),
      bwlimit: bwlimitArg ? bwlimitArg.slice('--bwlimit='.length) : '',
      excludes,
      extraArgs: rest.join(' '),
      dry_run: Boolean(job.dry_run),
      delete_extras: Boolean(job.delete_extras),
      route: job.route_resolved_by === 'manual' && job.route ? job.route : 'auto',
    });
    setShowTransferForm(true);
  };

  const testNode = async (node: SyncNode) => {
    setTestingNodeId(node.id);
    try {
      const result = await api<{ ok: boolean; latency_ms?: number | null; detail?: string }>(
        `/api/nodes/${node.id}/test`,
        { method: 'POST' },
      );
      await Promise.all([loadNodes(), loadLinks()]);
      if (result.ok) {
        const latency = result.latency_ms != null ? `${Math.round(Number(result.latency_ms))}ms` : '延迟未知';
        showNotice(`节点 ${node.name} 测试通过（${latency}）：${result.detail || ''}`, 'success');
      } else {
        showNotice(`节点 ${node.name} 测试失败：${result.detail || '未知错误'}`, 'error');
      }
    } catch (error) {
      showNotice((error as Error).message, 'error');
    } finally {
      setTestingNodeId(null);
    }
  };

  const deleteNode = async (node: SyncNode) => {
    if (!window.confirm(`确定删除节点「${node.name}」？相关连通性记录也会被清除。`)) return;
    try {
      await api(`/api/nodes/${node.id}`, { method: 'DELETE' });
      await Promise.all([loadNodes(), loadLinks()]);
      showNotice(`已删除节点 ${node.name}`, 'success');
    } catch (error) {
      showNotice((error as Error).message, 'error');
    }
  };

  const deleteSshKey = async (key: SshKeyInfo) => {
    if (!window.confirm(`确定删除 SSH 密钥「${key.name}」？`)) return;
    try {
      await api(`/api/ssh-keys/${key.id}`, { method: 'DELETE' });
      await loadSshKeys();
      showNotice(`已删除密钥 ${key.name}`, 'success');
    } catch (error) {
      showNotice((error as Error).message, 'error');
    }
  };

  const probeLinks = async (pairs?: string[][]) => {
    try {
      await api('/api/nodes/links/probe', {
        method: 'POST',
        body: JSON.stringify({ pairs: pairs || null }),
      });
      setLinksProbing(true);
    } catch (error) {
      showNotice((error as Error).message, 'error');
    }
  };

  const updateMaxConcurrent = async (value: number) => {
    try {
      await api('/api/transfers/settings', {
        method: 'PUT',
        body: JSON.stringify({ max_concurrent_transfers: value }),
      });
      setMaxConcurrent(value);
    } catch (error) {
      showNotice((error as Error).message, 'error');
    }
  };

  const noticeStyle = notice?.kind === 'error'
    ? 'bg-rose-50 border-rose-200 text-rose-700'
    : notice?.kind === 'success'
      ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
      : 'bg-blue-50 border-blue-200 text-blue-700';

  return (
    <div className="space-y-6">
      {notice && (
        <div className={`border rounded-xl px-4 py-3 text-xs font-medium shadow-sm flex items-start gap-2 ${noticeStyle}`}>
          {notice.kind === 'error'
            ? <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            : <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />}
          <span className="flex-1 whitespace-pre-wrap break-all">{notice.text}</span>
          <button onClick={() => setNotice(null)} className="shrink-0 opacity-60 hover:opacity-100 transition-opacity">
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="注册节点" value={String(remoteNodes.length)} type="neutral" />
        <StatCard label="活跃传输" value={String(activeJobs.length)} type="blue" />
        <StatCard label="连通边 OK" value={String(okLinkCount)} type="amber" />
        <StatCard label="失败边" value={String(failedLinkCount)} type="rose" />
      </div>

      {/* ---------- 区块 1：传输任务 ---------- */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        <div className="p-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-blue-600 rounded-lg text-white">
              <ArrowLeftRight className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-bold text-slate-800 text-base">传输任务</h3>
              <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">rsync transfers</p>
            </div>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {maxConcurrent !== null && (
              <label className="flex items-center gap-1.5 text-[10px] font-bold text-slate-400 uppercase tracking-widest">
                并发上限
                <select
                  value={maxConcurrent}
                  onChange={(event) => updateMaxConcurrent(Number(event.target.value))}
                  className="bg-white border border-slate-200 rounded-lg px-2 py-1.5 text-xs text-slate-700 outline-none focus:border-blue-500 transition-colors cursor-pointer"
                >
                  {[1, 2, 3, 4, 5, 6, 7, 8].map(value => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
              </label>
            )}
            <button
              onClick={() => loadTransfers().catch(error => showNotice((error as Error).message, 'error'))}
              className="flex items-center gap-2 px-3 py-2 bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 rounded-lg text-xs font-bold transition-colors shadow-sm"
            >
              <RefreshCcw className="w-3.5 h-3.5" />
              刷新
            </button>
            <button
              onClick={() => { setTransferDraft(null); setShowTransferForm(true); }}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-xs font-bold shadow-md shadow-blue-600/20 hover:bg-blue-700 active:scale-95 transition-all"
            >
              <Plus className="w-3.5 h-3.5" />
              新建传输
            </button>
          </div>
        </div>

        <div className="p-4 space-y-3 bg-slate-50/40">
          <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest px-1">进行中（{activeJobs.length}）</p>
          {activeJobs.map(job => (
            <TransferJobCard key={job.id} job={job} nodeNames={nodeNames} onCancel={() => cancelJob(job)} />
          ))}
          {activeJobs.length === 0 && (
            <div className="p-6 text-center text-slate-400 text-xs bg-white border border-dashed border-slate-200 rounded-xl">
              暂无进行中的传输任务
            </div>
          )}
        </div>

        <div className="p-4 border-t border-slate-100 space-y-3">
          <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest px-1">历史记录（{historyJobs.length}）</p>
          {historyJobs.length > 0 ? (
            <div className="overflow-x-auto custom-scrollbar">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="text-[10px] font-bold text-slate-400 uppercase tracking-widest border-b border-slate-100">
                    <th className="py-2 pr-3">状态</th>
                    <th className="py-2 pr-3">任务</th>
                    <th className="py-2 pr-3">路由</th>
                    <th className="py-2 pr-3">耗时</th>
                    <th className="py-2 pr-3">完成时间</th>
                    <th className="py-2 text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {historyJobs.map(job => (
                    <TransferHistoryRow
                      key={job.id}
                      job={job}
                      nodeNames={nodeNames}
                      onRetry={() => retryJob(job)}
                      onShowLog={() => setLogJob(job)}
                      onDelete={() => deleteJob(job)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="p-6 text-center text-slate-400 text-xs bg-slate-50/60 border border-dashed border-slate-200 rounded-xl">
              暂无历史传输记录
            </div>
          )}
        </div>
      </div>

      {/* ---------- 区块 3：节点管理 ---------- */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        <div className="p-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-slate-800 rounded-lg text-white">
              <HardDrive className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-bold text-slate-800 text-base">节点管理</h3>
              <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">本机作为 local 伪节点自动存在</p>
            </div>
          </div>
          <button
            onClick={() => setNodeModal({ node: null })}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-xs font-bold shadow-md shadow-blue-600/20 hover:bg-blue-700 active:scale-95 transition-all"
          >
            <Plus className="w-3.5 h-3.5" />
            注册节点
          </button>
        </div>
        <div className="p-4">
          {remoteNodes.length > 0 ? (
            <div className="overflow-x-auto custom-scrollbar">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="text-[10px] font-bold text-slate-400 uppercase tracking-widest border-b border-slate-100">
                    <th className="py-2 pr-3">名称</th>
                    <th className="py-2 pr-3">连接</th>
                    <th className="py-2 pr-3">认证</th>
                    <th className="py-2 pr-3">能力</th>
                    <th className="py-2 text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {remoteNodes.map(node => (
                    <tr key={node.id} className="border-b border-slate-50 hover:bg-slate-50/60 transition-colors">
                      <td className="py-2.5 pr-3">
                        <span className="font-bold text-slate-800">{node.name}</span>
                        {node.notes && <p className="text-[10px] text-slate-400 truncate max-w-[180px]" title={node.notes}>{node.notes}</p>}
                      </td>
                      <td className="py-2.5 pr-3 font-mono text-slate-600">
                        {node.username}@{node.host}:{node.ssh_port}
                      </td>
                      <td className="py-2.5 pr-3">
                        {node.auth_method === 'password' ? (
                          <span className="px-2 py-0.5 rounded border text-[9px] font-bold bg-amber-50 text-amber-700 border-amber-100">密码</span>
                        ) : (
                          <span className="px-2 py-0.5 rounded border text-[9px] font-bold bg-emerald-50 text-emerald-700 border-emerald-100">密钥</span>
                        )}
                      </td>
                      <td className="py-2.5 pr-3">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className={`px-2 py-0.5 rounded border text-[9px] font-bold ${node.rsync_version ? 'bg-blue-50 text-blue-600 border-blue-100' : 'bg-slate-50 text-slate-400 border-slate-100'}`}>
                            {node.rsync_version ? `rsync ${node.rsync_version}` : 'rsync 未知'}
                          </span>
                          <span className="text-[10px] text-slate-500 flex items-center gap-1" title="sshd 是否允许 TCP 端口转发（桥接发起端必需）">
                            转发 {capabilityMark(node.tcp_forward_ok)}
                          </span>
                          <span className="text-[10px] text-slate-500 flex items-center gap-1" title="sshd 是否允许 agent 转发（远程发起端向对端认证必需）">
                            Agent {capabilityMark(node.agent_forward_ok)}
                          </span>
                        </div>
                      </td>
                      <td className="py-2.5">
                        <div className="flex items-center gap-1.5 justify-end">
                          <button
                            onClick={() => testNode(node)}
                            disabled={testingNodeId === node.id}
                            className="flex items-center gap-1 px-2.5 py-1 bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 rounded text-[10px] font-bold transition-colors disabled:opacity-60"
                          >
                            {testingNodeId === node.id
                              ? <Loader2 className="w-3 h-3 animate-spin" />
                              : <Zap className="w-3 h-3" />}
                            {testingNodeId === node.id ? '测试中' : '测试'}
                          </button>
                          <button
                            onClick={() => setNodeModal({ node })}
                            className="p-1.5 bg-white border border-slate-200 hover:bg-slate-50 text-slate-500 hover:text-blue-600 rounded transition-colors"
                            title="编辑节点"
                          >
                            <Edit2 className="w-3 h-3" />
                          </button>
                          <button
                            onClick={() => deleteNode(node)}
                            className="p-1.5 bg-white border border-slate-200 hover:bg-rose-50 text-slate-500 hover:text-rose-600 rounded transition-colors"
                            title="删除节点"
                          >
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="p-8 text-center text-slate-400 space-y-2">
              <Server className="w-8 h-8 mx-auto opacity-20" />
              <p className="text-sm">尚未注册远程节点，点击右上角“注册节点”开始</p>
            </div>
          )}
        </div>
      </div>

      {/* ---------- 区块 4：SSH 密钥库 + 连通性矩阵 ---------- */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
          <div className="p-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-emerald-600 rounded-lg text-white">
                <KeyRound className="w-5 h-5" />
              </div>
              <div>
                <h3 className="font-bold text-slate-800 text-base">SSH 密钥库</h3>
                <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">私钥仅存储于本机</p>
              </div>
            </div>
            <button
              onClick={() => setShowKeyModal(true)}
              className="flex items-center gap-2 px-3 py-2 bg-blue-600 text-white rounded-lg text-xs font-bold shadow-md shadow-blue-600/20 hover:bg-blue-700 active:scale-95 transition-all"
            >
              <Plus className="w-3.5 h-3.5" />
              新增密钥
            </button>
          </div>
          <div className="p-4 space-y-2">
            {sshKeys.map(key => (
              <div key={key.id} className="flex items-center justify-between gap-3 bg-slate-50/60 border border-slate-100 rounded-lg px-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-bold text-slate-800 text-xs">{key.name}</span>
                    {key.kind && (
                      <span className="px-2 py-0.5 rounded border text-[9px] font-bold bg-blue-50 text-blue-600 border-blue-100 uppercase">{key.kind}</span>
                    )}
                  </div>
                  {key.fingerprint && (
                    <p className="text-[10px] text-slate-400 font-mono truncate" title={key.fingerprint}>{key.fingerprint}</p>
                  )}
                  {key.notes && <p className="text-[10px] text-slate-400 truncate">{key.notes}</p>}
                </div>
                <button
                  onClick={() => deleteSshKey(key)}
                  className="p-1.5 bg-white border border-slate-200 hover:bg-rose-50 text-slate-500 hover:text-rose-600 rounded transition-colors shrink-0"
                  title="删除密钥"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
            ))}
            {sshKeys.length === 0 && (
              <div className="p-8 text-center text-slate-400 space-y-2">
                <KeyRound className="w-8 h-8 mx-auto opacity-20" />
                <p className="text-sm">暂无 SSH 密钥，可粘贴私钥或引用已有路径</p>
              </div>
            )}
          </div>
        </div>

        <LinkMatrix
          nodes={nodes}
          links={links}
          probing={linksProbing}
          onProbe={probeLinks}
        />
      </div>

      {/* ---------- 弹窗 ---------- */}
      {showTransferForm && (
        <NewTransferModal
          nodes={nodes}
          nodeNames={nodeNames}
          initialDraft={transferDraft}
          onClose={() => { setShowTransferForm(false); setTransferDraft(null); }}
          onCreated={() => {
            setShowTransferForm(false);
            setTransferDraft(null);
            loadTransfers().catch(() => {});
            showNotice('传输任务已创建', 'success');
          }}
        />
      )}
      {nodeModal && (
        <NodeEditModal
          node={nodeModal.node}
          sshKeys={sshKeys}
          onClose={() => setNodeModal(null)}
          onSaved={(text) => {
            setNodeModal(null);
            loadNodes().catch(() => {});
            loadLinks().catch(() => {});
            showNotice(text, 'success');
          }}
        />
      )}
      {showKeyModal && (
        <SshKeyModal
          onClose={() => setShowKeyModal(false)}
          onSaved={(text) => {
            setShowKeyModal(false);
            loadSshKeys().catch(() => {});
            showNotice(text, 'success');
          }}
        />
      )}
      {logJob && (
        <TransferLogModal job={logJob} onClose={() => setLogJob(null)} />
      )}
    </div>
  );
}

function TransferJobCard({ job, nodeNames, onCancel }: {
  job: TransferJob;
  nodeNames: Record<string, string>;
  onCancel: () => void;
  key?: React.Key;
}) {
  const percent = Math.max(0, Math.min(100, Number(job.progress_percent ?? 0)));
  const isConnecting = job.status === 'running' && job.phase === 'connecting';
  const srcName = nodeNames[job.src_node_id] || job.src_node_id;
  const dstName = nodeNames[job.dst_node_id] || job.dst_node_id;
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm space-y-3 hover:border-blue-200 transition-colors">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className={`px-2 py-0.5 rounded border text-[9px] font-bold ${transferStatusStyle(job.status)}`}>
            {transferStatusLabel(job.status)}
          </span>
          <h4 className="font-bold text-slate-900 text-[13px] truncate">{job.name || `传输 ${job.id.slice(0, 8)}`}</h4>
          <span
            className="px-2 py-0.5 rounded border text-[9px] font-bold bg-slate-50 text-slate-600 border-slate-200 font-mono"
            title={transferRouteLabel(job.route)}
          >
            {transferRouteBadgeText(job, nodeNames)}
          </span>
          {job.dry_run && (
            <span className="px-2 py-0.5 rounded border text-[9px] font-bold bg-violet-50 text-violet-600 border-violet-100">DRY-RUN</span>
          )}
          {job.delete_extras && (
            <span className="px-2 py-0.5 rounded border text-[9px] font-bold bg-rose-50 text-rose-600 border-rose-100">--delete</span>
          )}
        </div>
        <button
          onClick={onCancel}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-rose-50 text-rose-600 border border-rose-200 hover:bg-rose-100 rounded-lg text-[10px] font-bold transition-colors"
        >
          <Trash2 className="w-3 h-3" />
          取消
        </button>
      </div>

      <p
        className="text-[11px] text-slate-500 font-mono truncate"
        title={`${srcName}:${job.src_path} → ${dstName}:${job.dst_path}`}
      >
        {srcName}:{job.src_path} → {dstName}:{job.dst_path}
      </p>

      {job.status === 'pending' ? (
        <div className="flex items-center gap-2 text-xs text-amber-600 font-bold">
          <Clock className="w-3.5 h-3.5" />
          排队等待调度...
        </div>
      ) : isConnecting ? (
        <div className="flex items-center gap-2 text-xs text-blue-600 font-bold">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          正在建立连接...
        </div>
      ) : (
        <div className="space-y-1.5">
          <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-500 rounded-full transition-all"
              style={{ width: `${percent}%` }}
            />
          </div>
          <div className="flex items-center gap-4 flex-wrap text-[10px] font-bold text-slate-500 font-mono tabular-nums">
            <span className="text-blue-600">{percent.toFixed(1)}%</span>
            <span>{job.transfer_rate || '--'}</span>
            <span>ETA {job.eta || '--'}</span>
            <span>{formatBytes(job.bytes_transferred)}</span>
            {job.files_transferred != null && <span>{job.files_transferred} 个文件</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function TransferHistoryRow({ job, nodeNames, onRetry, onShowLog, onDelete }: {
  job: TransferJob;
  nodeNames: Record<string, string>;
  onRetry: () => void;
  onShowLog: () => void;
  onDelete: () => void;
  key?: React.Key;
}) {
  const srcName = nodeNames[job.src_node_id] || job.src_node_id;
  const dstName = nodeNames[job.dst_node_id] || job.dst_node_id;
  return (
    <tr className="border-b border-slate-50 hover:bg-slate-50/60 transition-colors align-top">
      <td className="py-2.5 pr-3">
        <span className={`px-2 py-0.5 rounded border text-[9px] font-bold whitespace-nowrap ${transferStatusStyle(job.status)}`}>
          {transferStatusLabel(job.status)}
        </span>
      </td>
      <td className="py-2.5 pr-3 min-w-[200px]">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-bold text-slate-800">{job.name || `传输 ${job.id.slice(0, 8)}`}</span>
          {job.dry_run && (
            <span className="px-1.5 py-0.5 rounded border text-[8px] font-bold bg-violet-50 text-violet-600 border-violet-100">DRY-RUN</span>
          )}
          {job.delete_extras && (
            <span className="px-1.5 py-0.5 rounded border text-[8px] font-bold bg-rose-50 text-rose-600 border-rose-100">--delete</span>
          )}
        </div>
        <p
          className="text-[10px] text-slate-400 font-mono truncate max-w-[320px]"
          title={`${srcName}:${job.src_path} → ${dstName}:${job.dst_path}`}
        >
          {srcName}:{job.src_path} → {dstName}:{job.dst_path}
        </p>
        {job.error && (
          <p className="text-[10px] text-rose-600 truncate max-w-[320px]" title={job.error}>
            {job.error_code ? `[${job.error_code}] ` : ''}{job.error}
          </p>
        )}
      </td>
      <td className="py-2.5 pr-3 whitespace-nowrap">
        <span
          className="px-2 py-0.5 rounded border text-[9px] font-bold bg-slate-50 text-slate-600 border-slate-200 font-mono"
          title={transferRouteLabel(job.route)}
        >
          {transferRouteBadgeText(job, nodeNames)}
        </span>
      </td>
      <td className="py-2.5 pr-3 font-mono text-slate-600 whitespace-nowrap">{transferDurationText(job)}</td>
      <td className="py-2.5 pr-3 text-slate-500 whitespace-nowrap">{formatTime(job.finished_at) || '--'}</td>
      <td className="py-2.5">
        <div className="flex items-center gap-1.5 justify-end">
          <button
            onClick={onRetry}
            className="flex items-center gap-1 px-2.5 py-1 bg-white border border-slate-200 hover:bg-blue-50 text-slate-600 hover:text-blue-600 rounded text-[10px] font-bold transition-colors"
            title="以原参数打开新建表单"
          >
            <RotateCw className="w-3 h-3" />
            重试
          </button>
          <button
            onClick={onShowLog}
            className="flex items-center gap-1 px-2.5 py-1 bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 rounded text-[10px] font-bold transition-colors"
          >
            <FileText className="w-3 h-3" />
            日志
          </button>
          <button
            onClick={onDelete}
            className="p-1.5 bg-white border border-slate-200 hover:bg-rose-50 text-slate-500 hover:text-rose-600 rounded transition-colors"
            title="删除记录"
          >
            <Trash2 className="w-3 h-3" />
          </button>
        </div>
      </td>
    </tr>
  );
}

function TransferLogModal({ job, onClose }: { job: TransferJob; onClose: () => void }) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [isFull, setIsFull] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api<{ content?: string; size?: number; log_path?: string }>(`/api/transfers/${job.id}/log?full=${isFull}`)
      .then(payload => {
        if (cancelled) return;
        setContent(payload.content || '');
        setError('');
      })
      .catch(err => {
        if (cancelled) return;
        setError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [job.id, isFull]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-slate-900/40 backdrop-blur-sm">
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        className="relative w-full max-w-4xl bg-white border border-slate-200 rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[calc(100vh-3rem)]"
      >
        <div className="shrink-0 px-6 py-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-base font-bold text-slate-900 tracking-tight truncate">传输日志：{job.name || job.id}</h2>
            <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">{isFull ? '完整日志' : '日志尾部'}</p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {!isFull && (
              <button
                onClick={() => setIsFull(true)}
                className="px-3 py-1.5 bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 rounded-lg text-[10px] font-bold transition-colors"
              >
                加载完整日志
              </button>
            )}
            <button onClick={onClose} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-900 transition-colors">
              <Plus className="w-5 h-5 rotate-45" />
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-auto p-4 bg-slate-950 custom-scrollbar min-h-[280px]">
          {loading ? (
            <div className="flex items-center gap-2 text-slate-400 text-xs">
              <Loader2 className="w-4 h-4 animate-spin" />
              加载日志中...
            </div>
          ) : error ? (
            <p className="text-rose-400 text-xs">{error}</p>
          ) : (
            <pre className="text-slate-200 text-[11px] leading-relaxed whitespace-pre-wrap break-all font-mono">
              {content || '（日志为空）'}
            </pre>
          )}
        </div>
      </motion.div>
    </div>
  );
}

function NewTransferModal({ nodes, nodeNames, initialDraft, onClose, onCreated }: {
  nodes: SyncNode[];
  nodeNames: Record<string, string>;
  initialDraft: TransferFormDraft | null;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [draft, setDraft] = useState<TransferFormDraft>(initialDraft || EMPTY_TRANSFER_DRAFT);
  const [showAdvanced, setShowAdvanced] = useState(Boolean(
    initialDraft && (
      initialDraft.compress || initialDraft.bwlimit || initialDraft.excludes.length > 0 ||
      initialDraft.extraArgs || initialDraft.dry_run || initialDraft.delete_extras
    )
  ));
  const [excludeInput, setExcludeInput] = useState('');
  const [plan, setPlan] = useState<TransferPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [probing, setProbing] = useState(false);
  const [planError, setPlanError] = useState('');
  const [submitError, setSubmitError] = useState('');
  const [browsing, setBrowsing] = useState<'src' | 'dst' | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const update = (patch: Partial<TransferFormDraft>) => setDraft(prev => ({ ...prev, ...patch }));

  const planReady = Boolean(
    draft.src_node_id && draft.dst_node_id && draft.src_path.trim() && draft.dst_path.trim()
  );

  // 两端节点与路径就绪后防抖请求路由方案
  useEffect(() => {
    if (!planReady) {
      setPlan(null);
      setPlanError('');
      return;
    }
    const timer = window.setTimeout(() => {
      setPlanning(true);
      api<TransferPlan>('/api/transfers/plan', {
        method: 'POST',
        body: JSON.stringify({
          src_node_id: draft.src_node_id,
          src_path: draft.src_path.trim(),
          dst_node_id: draft.dst_node_id,
          dst_path: draft.dst_path.trim(),
        }),
      })
        .then(payload => {
          setPlan(payload);
          setPlanError('');
        })
        .catch(error => {
          setPlan(null);
          setPlanError((error as Error).message);
        })
        .finally(() => setPlanning(false));
    }, 500);
    return () => window.clearTimeout(timer);
  }, [planReady, draft.src_node_id, draft.src_path, draft.dst_node_id, draft.dst_path]);

  const probePlan = async () => {
    if (!planReady || probing) return;
    setProbing(true);
    try {
      const payload = await api<TransferPlan>('/api/transfers/plan?probe=true', {
        method: 'POST',
        body: JSON.stringify({
          src_node_id: draft.src_node_id,
          src_path: draft.src_path.trim(),
          dst_node_id: draft.dst_node_id,
          dst_path: draft.dst_path.trim(),
        }),
      });
      setPlan(payload);
      setPlanError('');
    } catch (error) {
      setPlanError((error as Error).message);
    } finally {
      setProbing(false);
    }
  };

  const addExclude = () => {
    const pattern = excludeInput.trim();
    if (!pattern) return;
    if (!draft.excludes.includes(pattern)) {
      update({ excludes: [...draft.excludes, pattern] });
    }
    setExcludeInput('');
  };

  const buildRsyncArgs = () => {
    const args: string[] = [];
    if (draft.compress) args.push('-z');
    const bwlimit = draft.bwlimit.trim();
    if (bwlimit) args.push(`--bwlimit=${bwlimit}`);
    draft.excludes.forEach(pattern => args.push(`--exclude=${pattern}`));
    draft.extraArgs.trim().split(/\s+/).filter(Boolean).forEach(arg => args.push(arg));
    return args;
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!draft.src_node_id || !draft.dst_node_id) {
      setSubmitError('请选择源节点和目标节点');
      return;
    }
    if (!draft.src_path.trim() || !draft.dst_path.trim()) {
      setSubmitError('请填写源路径和目标路径');
      return;
    }
    if (draft.delete_extras && !window.confirm(
      '高危操作确认：--delete 会删除目标目录中源端不存在的文件，可能造成数据永久丢失！\n\n建议先勾选 dry-run 试运行确认影响范围。确定继续提交？'
    )) {
      return;
    }
    setSubmitting(true);
    try {
      await api('/api/transfers', {
        method: 'POST',
        body: JSON.stringify({
          name: draft.name.trim() || null,
          src_node_id: draft.src_node_id,
          src_path: draft.src_path.trim(),
          dst_node_id: draft.dst_node_id,
          dst_path: draft.dst_path.trim(),
          src_contents_only: draft.src_contents_only,
          rsync_args: buildRsyncArgs(),
          delete_extras: draft.delete_extras,
          dry_run: draft.dry_run,
          route: draft.route,
          probe_unknown: true,
        }),
      });
      onCreated();
    } catch (error) {
      // 409 等错误直接展示后端 detail
      setSubmitError((error as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass = 'w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors';
  const labelClass = 'text-[10px] font-bold text-slate-500 uppercase tracking-widest';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-slate-900/40 backdrop-blur-sm">
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        className="relative w-full max-w-3xl bg-white border border-slate-200 rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[calc(100vh-3rem)]"
      >
        <div className="shrink-0 px-6 py-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-bold text-slate-900 tracking-tight">新建传输</h2>
            <p className="text-xs text-slate-500 font-medium">配置 rsync 传输参数并选择路由</p>
          </div>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-900 transition-colors">
            <Plus className="w-5 h-5 rotate-45" />
          </button>
        </div>

        <form onSubmit={submit} className="px-6 py-5 space-y-4 overflow-y-auto custom-scrollbar">
          <div className="space-y-1.5">
            <label className={labelClass}>任务名称</label>
            <input
              type="text"
              value={draft.name}
              onChange={(event) => update({ name: event.target.value })}
              placeholder="例如: 同步 checkpoint 到 A100 节点（可选）"
              className={inputClass}
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
            <div className="space-y-1.5">
              <label className={labelClass}>源节点</label>
              <select
                value={draft.src_node_id}
                onChange={(event) => update({ src_node_id: event.target.value })}
                className={`${inputClass} cursor-pointer`}
              >
                <option value="">选择源节点</option>
                {nodes.map(node => (
                  <option key={node.id} value={node.id}>{syncNodeOptionLabel(node)}</option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <label className={labelClass}>目标节点</label>
              <select
                value={draft.dst_node_id}
                onChange={(event) => update({ dst_node_id: event.target.value })}
                className={`${inputClass} cursor-pointer`}
              >
                <option value="">选择目标节点</option>
                {nodes.map(node => (
                  <option key={node.id} value={node.id}>{syncNodeOptionLabel(node)}</option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <label className={labelClass}>源路径</label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={draft.src_path}
                  onChange={(event) => update({ src_path: event.target.value })}
                  placeholder="/data/checkpoints/llama"
                  required
                  className={`${inputClass} font-mono flex-1`}
                  spellCheck={false}
                />
                <button
                  type="button"
                  onClick={() => setBrowsing('src')}
                  className="px-3 py-2 rounded-lg text-xs font-bold text-blue-600 border border-blue-200 hover:bg-blue-50 transition-colors shrink-0"
                >
                  <FolderOpen className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
            <div className="space-y-1.5">
              <label className={labelClass}>目标路径</label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={draft.dst_path}
                  onChange={(event) => update({ dst_path: event.target.value })}
                  placeholder="/data/checkpoints/"
                  required
                  className={`${inputClass} font-mono flex-1`}
                  spellCheck={false}
                />
                <button
                  type="button"
                  onClick={() => setBrowsing('dst')}
                  className="px-3 py-2 rounded-lg text-xs font-bold text-blue-600 border border-blue-200 hover:bg-blue-50 transition-colors shrink-0"
                >
                  <FolderOpen className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          </div>

          <div className="space-y-1.5">
            <label className={labelClass}>源目录语义</label>
            <div className="grid grid-cols-2 gap-1 bg-slate-100 border border-slate-200 rounded-lg p-1">
              {([
                [false, '复制目录本身'],
                [true, '仅复制目录内容'],
              ] as [boolean, string][]).map(([value, label]) => (
                <label key={String(value)} className="cursor-pointer">
                  <input
                    type="radio"
                    name="src_contents_only"
                    checked={draft.src_contents_only === value}
                    onChange={() => update({ src_contents_only: value })}
                    className="peer sr-only"
                  />
                  <span className="block rounded-md px-2 py-1.5 text-center text-[11px] font-bold text-slate-500 transition-all peer-checked:bg-white peer-checked:text-blue-600 peer-checked:shadow-sm">
                    {label}
                  </span>
                </label>
              ))}
            </div>
          </div>

          {/* 高级选项（rsync 参数） */}
          <div className="border border-slate-200 rounded-xl overflow-hidden">
            <button
              type="button"
              onClick={() => setShowAdvanced(prev => !prev)}
              className="w-full flex items-center justify-between px-4 py-2.5 bg-slate-50/60 hover:bg-slate-50 text-left transition-colors"
            >
              <span className={labelClass}>高级选项（rsync 参数）</span>
              <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform ${showAdvanced ? 'rotate-180' : ''}`} />
            </button>
            {showAdvanced && (
              <div className="p-4 space-y-4 border-t border-slate-100">
                <div className="flex items-center gap-6 flex-wrap">
                  <label className="flex items-center gap-2 text-xs font-bold text-slate-600 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={draft.compress}
                      onChange={(event) => update({ compress: event.target.checked })}
                      className="rounded border-slate-300"
                    />
                    压缩传输 (-z)
                  </label>
                  <label className="flex items-center gap-2 text-xs font-bold text-slate-600 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={draft.dry_run}
                      onChange={(event) => update({ dry_run: event.target.checked })}
                      className="rounded border-slate-300"
                    />
                    试运行 (dry-run)
                  </label>
                  <label className="flex items-center gap-2 text-xs font-bold text-rose-600 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={draft.delete_extras}
                      onChange={(event) => update({ delete_extras: event.target.checked })}
                      className="rounded border-slate-300"
                    />
                    删除目标端多余文件 (--delete)
                  </label>
                </div>
                {draft.delete_extras && (
                  <p className="text-[11px] text-rose-600 font-medium flex items-start gap-1.5 bg-rose-50 border border-rose-100 rounded-lg px-3 py-2">
                    <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                    --delete 会删除目标目录中源端不存在的文件，提交时需要二次确认，建议先 dry-run。
                  </p>
                )}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
                  <div className="space-y-1.5">
                    <label className={labelClass}>带宽限制 --bwlimit</label>
                    <input
                      type="text"
                      value={draft.bwlimit}
                      onChange={(event) => update({ bwlimit: event.target.value })}
                      placeholder="例如 20M（留空不限制）"
                      className={`${inputClass} font-mono`}
                      spellCheck={false}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className={labelClass}>其他白名单参数</label>
                    <input
                      type="text"
                      value={draft.extraArgs}
                      onChange={(event) => update({ extraArgs: event.target.value })}
                      placeholder="例如 --checksum --update"
                      className={`${inputClass} font-mono`}
                      spellCheck={false}
                    />
                  </div>
                </div>
                <div className="space-y-1.5">
                  <label className={labelClass}>排除规则 --exclude</label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={excludeInput}
                      onChange={(event) => setExcludeInput(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault();
                          addExclude();
                        }
                      }}
                      placeholder="例如 *.pyc 或 __pycache__/"
                      className={`${inputClass} font-mono flex-1`}
                      spellCheck={false}
                    />
                    <button
                      type="button"
                      onClick={addExclude}
                      className="px-4 py-2 bg-slate-100 border border-slate-200 hover:bg-slate-200 text-slate-600 rounded-lg text-xs font-bold transition-colors shrink-0"
                    >
                      添加
                    </button>
                  </div>
                  {draft.excludes.length > 0 && (
                    <div className="flex items-center gap-1.5 flex-wrap pt-1">
                      {draft.excludes.map(pattern => (
                        <span key={pattern} className="flex items-center gap-1 px-2 py-0.5 bg-slate-100 border border-slate-200 rounded text-[10px] font-mono text-slate-600">
                          {pattern}
                          <button
                            type="button"
                            onClick={() => update({ excludes: draft.excludes.filter(item => item !== pattern) })}
                            className="text-slate-400 hover:text-rose-500 transition-colors"
                          >
                            <Plus className="w-3 h-3 rotate-45" />
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* 路由选择 */}
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <label className={labelClass}>传输路由</label>
              <div className="flex items-center gap-2">
                {planning && (
                  <span className="flex items-center gap-1.5 text-[10px] font-bold text-slate-400">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    解析路由中...
                  </span>
                )}
                {plan?.needs_probe && (
                  <button
                    type="button"
                    onClick={probePlan}
                    disabled={probing}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-100 rounded-lg text-[10px] font-bold transition-colors disabled:opacity-60"
                  >
                    {probing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
                    {probing ? '探测中...' : '探测后确定'}
                  </button>
                )}
              </div>
            </div>
            {planError && <p className="text-[11px] text-rose-600">{planError}</p>}
            {!planReady && (
              <p className="text-[11px] text-slate-400">选定源/目标节点与路径后自动解析可用路由</p>
            )}
            <label
              className={`block border rounded-lg px-3 py-2.5 cursor-pointer transition-colors ${
                draft.route === 'auto'
                  ? 'border-blue-500 ring-1 ring-blue-200 bg-blue-50/40'
                  : 'border-slate-200 bg-white hover:border-blue-200'
              }`}
            >
              <div className="flex items-center gap-2">
                <input
                  type="radio"
                  name="transfer_route"
                  checked={draft.route === 'auto'}
                  onChange={() => update({ route: 'auto' })}
                />
                <span className="text-xs font-bold text-slate-800">自动选择</span>
                {plan?.recommended && (
                  <span className="text-[10px] text-slate-400">推荐：{transferRouteLabel(plan.recommended)}</span>
                )}
              </div>
            </label>
            {(plan?.candidates || []).map(candidate => {
              const isBridged = candidate.route.startsWith('bridged');
              const isRecommended = plan?.recommended === candidate.route;
              const needsProbe = (candidate.requires_probe || []).length > 0;
              const selected = draft.route === candidate.route;
              return (
                <label
                  key={candidate.route}
                  className={`block border rounded-lg px-3 py-2.5 transition-colors ${
                    !candidate.feasible
                      ? 'border-slate-200 bg-slate-50/60 opacity-60 cursor-not-allowed'
                      : selected
                        ? 'border-blue-500 ring-1 ring-blue-200 bg-blue-50/40 cursor-pointer'
                        : isRecommended
                          ? 'border-blue-300 bg-blue-50/20 cursor-pointer hover:border-blue-400'
                          : 'border-slate-200 bg-white cursor-pointer hover:border-blue-200'
                  }`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <input
                      type="radio"
                      name="transfer_route"
                      disabled={!candidate.feasible}
                      checked={selected}
                      onChange={() => candidate.feasible && update({ route: candidate.route })}
                    />
                    <span className="text-xs font-bold text-slate-800">{transferRouteLabel(candidate.route)}</span>
                    {isRecommended && (
                      <span className="px-1.5 py-0.5 rounded border text-[9px] font-bold bg-blue-50 text-blue-600 border-blue-100">推荐</span>
                    )}
                    {candidate.feasible && needsProbe && (
                      <span className="px-1.5 py-0.5 rounded border text-[9px] font-bold bg-amber-50 text-amber-700 border-amber-100">待探测</span>
                    )}
                    {!candidate.feasible && (
                      <span className="px-1.5 py-0.5 rounded border text-[9px] font-bold bg-slate-100 text-slate-400 border-slate-200">不可行</span>
                    )}
                  </div>
                  {isBridged && candidate.feasible && (
                    <p className="mt-1.5 text-[11px] text-amber-600 font-medium flex items-start gap-1.5">
                      <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                      数据经主控中转，速率受主控带宽限制
                    </p>
                  )}
                  {candidate.feasible && needsProbe && (
                    <p className="mt-1 text-[10px] text-slate-400">
                      待探测链路：{(candidate.requires_probe || []).map(pair => `${nodeNames[pair[0]] || pair[0]} → ${nodeNames[pair[1]] || pair[1]}`).join('、')}
                    </p>
                  )}
                  {(candidate.reasons || []).length > 0 && (
                    <ul className="mt-1 text-[10px] text-slate-400 list-disc list-inside space-y-0.5">
                      {candidate.reasons.map((reason, index) => (
                        <li key={index}>{reason}</li>
                      ))}
                    </ul>
                  )}
                </label>
              );
            })}
          </div>

          {submitError && (
            <p className="text-[11px] text-rose-600 font-medium bg-rose-50 border border-rose-100 rounded-lg px-3 py-2 whitespace-pre-wrap break-all">
              {submitError}
            </p>
          )}

          <div className="flex justify-end gap-3 pt-4 border-t border-slate-100">
            <button
              type="button"
              onClick={onClose}
              className="px-6 py-2 rounded-lg text-slate-500 font-bold hover:bg-slate-50 transition-colors text-sm"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-10 py-2 bg-blue-600 text-white rounded-lg font-bold shadow-md shadow-blue-600/20 hover:bg-blue-700 active:scale-95 transition-all text-sm disabled:opacity-60"
            >
              {submitting ? '提交中...' : '创建传输任务'}
            </button>
          </div>
        </form>
      </motion.div>
      {browsing && (
        <DirectoryBrowser
          nodeId={browsing === 'src' ? draft.src_node_id : draft.dst_node_id}
          onSelect={(path) => {
            if (browsing === 'src') update({ src_path: path });
            else update({ dst_path: path });
            setBrowsing(null);
          }}
          onClose={() => setBrowsing(null)}
        />
      )}
    </div>
  );
}

function NodeEditModal({ node, sshKeys, onClose, onSaved }: {
  node: SyncNode | null;
  sshKeys: SshKeyInfo[];
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const isEdit = Boolean(node);
  const [name, setName] = useState(node?.name || '');
  const [host, setHost] = useState(node?.host || '');
  const [sshPort, setSshPort] = useState(String(node?.ssh_port ?? 22));
  const [username, setUsername] = useState(node?.username || '');
  const [authMethod, setAuthMethod] = useState<'key' | 'password'>(node?.auth_method === 'password' ? 'password' : 'key');
  const [sshKeyId, setSshKeyId] = useState(node?.ssh_key_id || '');
  const [password, setPassword] = useState('');
  const [notes, setNotes] = useState(node?.notes || '');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const keepOldPassword = isEdit && node?.auth_method === 'password' && Boolean(node?.has_password);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (authMethod === 'key' && !sshKeyId) {
      setError('密钥认证方式必须选择一个 SSH 密钥');
      return;
    }
    if (authMethod === 'password' && !password && !keepOldPassword) {
      setError('密码认证方式必须填写密码');
      return;
    }
    const body = {
      name: name.trim(),
      host: host.trim(),
      ssh_port: Number(sshPort) || 22,
      username: username.trim(),
      auth_method: authMethod,
      ssh_key_id: authMethod === 'key' ? sshKeyId : null,
      // 编辑时密码留空 → null 表示沿用旧密码
      password: authMethod === 'password' ? (password || null) : null,
      notes: notes.trim() || null,
    };
    setSubmitting(true);
    try {
      if (isEdit && node) {
        await api(`/api/nodes/${node.id}`, { method: 'PUT', body: JSON.stringify(body) });
      } else {
        await api('/api/nodes', { method: 'POST', body: JSON.stringify(body) });
      }
      onSaved(isEdit ? `已更新节点 ${name.trim()}` : `已注册节点 ${name.trim()}`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass = 'w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors';
  const labelClass = 'text-[10px] font-bold text-slate-500 uppercase tracking-widest';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-slate-900/40 backdrop-blur-sm">
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        className="relative w-full max-w-xl bg-white border border-slate-200 rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[calc(100vh-3rem)]"
      >
        <div className="shrink-0 px-6 py-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-bold text-slate-900 tracking-tight">{isEdit ? '编辑节点' : '注册节点'}</h2>
            <p className="text-xs text-slate-500 font-medium">{isEdit ? `修改节点 ${node?.name} 的连接参数` : '注册一台可通过 SSH 访问的服务器'}</p>
          </div>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-900 transition-colors">
            <Plus className="w-5 h-5 rotate-45" />
          </button>
        </div>

        <form onSubmit={submit} className="px-6 py-5 space-y-4 overflow-y-auto custom-scrollbar">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
            <div className="space-y-1.5">
              <label className={labelClass}>节点名称</label>
              <input
                type="text"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如: a100-01"
                required
                className={inputClass}
              />
            </div>
            <div className="space-y-1.5">
              <label className={labelClass}>主机地址</label>
              <input
                type="text"
                value={host}
                onChange={(event) => setHost(event.target.value)}
                placeholder="IP 或域名"
                required
                className={`${inputClass} font-mono`}
                spellCheck={false}
              />
            </div>
            <div className="space-y-1.5">
              <label className={labelClass}>SSH 端口</label>
              <input
                type="number"
                min="1"
                max="65535"
                value={sshPort}
                onChange={(event) => setSshPort(event.target.value)}
                required
                className={`${inputClass} font-mono`}
              />
            </div>
            <div className="space-y-1.5">
              <label className={labelClass}>用户名</label>
              <input
                type="text"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder="例如: root"
                required
                className={`${inputClass} font-mono`}
                spellCheck={false}
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className={labelClass}>认证方式</label>
            <div className="grid grid-cols-2 gap-1 bg-slate-100 border border-slate-200 rounded-lg p-1">
              {([
                ['key', 'SSH 密钥'],
                ['password', '密码'],
              ] as ['key' | 'password', string][]).map(([value, label]) => (
                <label key={value} className="cursor-pointer">
                  <input
                    type="radio"
                    name="auth_method"
                    checked={authMethod === value}
                    onChange={() => setAuthMethod(value)}
                    className="peer sr-only"
                  />
                  <span className="block rounded-md px-2 py-1.5 text-center text-[11px] font-bold text-slate-500 transition-all peer-checked:bg-white peer-checked:text-blue-600 peer-checked:shadow-sm">
                    {label}
                  </span>
                </label>
              ))}
            </div>
          </div>

          {authMethod === 'key' ? (
            <div className="space-y-1.5">
              <label className={labelClass}>SSH 密钥</label>
              <select
                value={sshKeyId}
                onChange={(event) => setSshKeyId(event.target.value)}
                className={`${inputClass} cursor-pointer`}
              >
                <option value="">选择密钥库中的密钥</option>
                {sshKeys.map(key => (
                  <option key={key.id} value={key.id}>{key.name}{key.kind ? `（${key.kind}）` : ''}</option>
                ))}
              </select>
              {sshKeys.length === 0 && (
                <p className="text-[11px] text-amber-600">密钥库为空，请先在“SSH 密钥库”中添加密钥</p>
              )}
            </div>
          ) : (
            <div className="space-y-1.5">
              <label className={labelClass}>登录密码</label>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder={keepOldPassword ? '留空则保持原密码' : '输入 SSH 登录密码'}
                className={`${inputClass} font-mono`}
                autoComplete="new-password"
              />
              <p className="text-[11px] text-rose-600 font-medium flex items-start gap-1.5">
                <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                密码将以明文存储于本机数据库，建议优先使用 SSH 密钥认证
              </p>
            </div>
          )}

          <div className="space-y-1.5">
            <label className={labelClass}>备注</label>
            <input
              type="text"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="可选备注说明"
              className={inputClass}
            />
          </div>

          {error && (
            <p className="text-[11px] text-rose-600 font-medium bg-rose-50 border border-rose-100 rounded-lg px-3 py-2 whitespace-pre-wrap break-all">
              {error}
            </p>
          )}

          <div className="flex justify-end gap-3 pt-4 border-t border-slate-100">
            <button
              type="button"
              onClick={onClose}
              className="px-6 py-2 rounded-lg text-slate-500 font-bold hover:bg-slate-50 transition-colors text-sm"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-10 py-2 bg-blue-600 text-white rounded-lg font-bold shadow-md shadow-blue-600/20 hover:bg-blue-700 active:scale-95 transition-all text-sm disabled:opacity-60"
            >
              {submitting ? '保存中...' : isEdit ? '保存修改' : '注册节点'}
            </button>
          </div>
        </form>
      </motion.div>
    </div>
  );
}

function SshKeyModal({ onClose, onSaved }: { onClose: () => void; onSaved: (message: string) => void }) {
  const [mode, setMode] = useState<'paste' | 'path'>('paste');
  const [name, setName] = useState('');
  const [privateKey, setPrivateKey] = useState('');
  const [keyPath, setKeyPath] = useState('');
  const [notes, setNotes] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (mode === 'paste' && !privateKey.trim()) {
      setError('请粘贴私钥内容');
      return;
    }
    if (mode === 'path' && !keyPath.trim()) {
      setError('请填写私钥文件路径');
      return;
    }
    setSubmitting(true);
    try {
      await api('/api/ssh-keys', {
        method: 'POST',
        body: JSON.stringify({
          name: name.trim(),
          private_key: mode === 'paste' ? privateKey : null,
          private_key_path: mode === 'path' ? keyPath.trim() : null,
          notes: notes.trim() || null,
        }),
      });
      onSaved(`已添加 SSH 密钥 ${name.trim()}`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass = 'w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none focus:border-blue-500 transition-colors';
  const labelClass = 'text-[10px] font-bold text-slate-500 uppercase tracking-widest';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-slate-900/40 backdrop-blur-sm">
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        className="relative w-full max-w-xl bg-white border border-slate-200 rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[calc(100vh-3rem)]"
      >
        <div className="shrink-0 px-6 py-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-bold text-slate-900 tracking-tight">新增 SSH 密钥</h2>
            <p className="text-xs text-slate-500 font-medium">粘贴私钥内容或引用本机已有的私钥文件</p>
          </div>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-900 transition-colors">
            <Plus className="w-5 h-5 rotate-45" />
          </button>
        </div>

        <form onSubmit={submit} className="px-6 py-5 space-y-4 overflow-y-auto custom-scrollbar">
          <div className="space-y-1.5">
            <label className={labelClass}>密钥名称</label>
            <input
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如: cluster-ed25519"
              required
              className={inputClass}
            />
          </div>

          <div className="space-y-1.5">
            <label className={labelClass}>来源</label>
            <div className="grid grid-cols-2 gap-1 bg-slate-100 border border-slate-200 rounded-lg p-1">
              {([
                ['paste', '粘贴私钥'],
                ['path', '引用已有路径'],
              ] as ['paste' | 'path', string][]).map(([value, label]) => (
                <label key={value} className="cursor-pointer">
                  <input
                    type="radio"
                    name="ssh_key_mode"
                    checked={mode === value}
                    onChange={() => setMode(value)}
                    className="peer sr-only"
                  />
                  <span className="block rounded-md px-2 py-1.5 text-center text-[11px] font-bold text-slate-500 transition-all peer-checked:bg-white peer-checked:text-blue-600 peer-checked:shadow-sm">
                    {label}
                  </span>
                </label>
              ))}
            </div>
          </div>

          {mode === 'paste' ? (
            <div className="space-y-1.5">
              <label className={labelClass}>私钥内容</label>
              <textarea
                rows={8}
                value={privateKey}
                onChange={(event) => setPrivateKey(event.target.value)}
                placeholder={'-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----'}
                className={`${inputClass} font-mono text-[11px] resize-y`}
                spellCheck={false}
              />
              <p className="text-[11px] text-slate-400">私钥会以 0600 权限保存到本机数据目录，不会出现在任何 API 响应或日志中</p>
            </div>
          ) : (
            <div className="space-y-1.5">
              <label className={labelClass}>私钥文件路径</label>
              <input
                type="text"
                value={keyPath}
                onChange={(event) => setKeyPath(event.target.value)}
                placeholder="/root/.ssh/id_ed25519"
                className={`${inputClass} font-mono`}
                spellCheck={false}
              />
              <p className="text-[11px] text-slate-400">引用主控本机上已存在的私钥文件，不复制内容</p>
            </div>
          )}

          <div className="space-y-1.5">
            <label className={labelClass}>备注</label>
            <input
              type="text"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="可选备注说明"
              className={inputClass}
            />
          </div>

          {error && (
            <p className="text-[11px] text-rose-600 font-medium bg-rose-50 border border-rose-100 rounded-lg px-3 py-2 whitespace-pre-wrap break-all">
              {error}
            </p>
          )}

          <div className="flex justify-end gap-3 pt-4 border-t border-slate-100">
            <button
              type="button"
              onClick={onClose}
              className="px-6 py-2 rounded-lg text-slate-500 font-bold hover:bg-slate-50 transition-colors text-sm"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-10 py-2 bg-blue-600 text-white rounded-lg font-bold shadow-md shadow-blue-600/20 hover:bg-blue-700 active:scale-95 transition-all text-sm disabled:opacity-60"
            >
              {submitting ? '保存中...' : '添加密钥'}
            </button>
          </div>
        </form>
      </motion.div>
    </div>
  );
}

function LinkMatrix({ nodes, links, probing, onProbe }: {
  nodes: SyncNode[];
  links: NodeLinkInfo[];
  probing: boolean;
  onProbe: (pairs?: string[][]) => void;
}) {
  const remoteNodes = nodes.filter(node => !node.is_local);

  const findLink = (fromId: string, toId: string) =>
    links.find(link => link.from_node_id === fromId && link.to_node_id === toId);

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
      <div className="p-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-violet-600 rounded-lg text-white">
            <Network className="w-5 h-5" />
          </div>
          <div>
            <h3 className="font-bold text-slate-800 text-base">连通性矩阵</h3>
            <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">行=发起端 / 列=目标节点</p>
          </div>
        </div>
        <button
          onClick={() => onProbe()}
          disabled={probing}
          className="flex items-center gap-2 px-3 py-2 bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 rounded-lg text-xs font-bold transition-colors shadow-sm disabled:opacity-60"
        >
          {probing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCcw className="w-3.5 h-3.5" />}
          {probing ? '探测中...' : '全矩阵探测'}
        </button>
      </div>
      <div className="p-4">
        {remoteNodes.length > 0 ? (
          <>
            <div className="overflow-x-auto custom-scrollbar">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-[10px] font-bold text-slate-400 uppercase tracking-widest border-b border-slate-100">
                    <th className="py-2 pr-3 text-left">发起端 \ 目标</th>
                    {remoteNodes.map(node => (
                      <th key={node.id} className="py-2 px-2 text-center max-w-[100px] truncate" title={node.name}>{node.name}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {nodes.map(fromNode => (
                    <tr key={fromNode.id} className="border-b border-slate-50">
                      <td className="py-2 pr-3 font-bold text-slate-700 whitespace-nowrap">
                        {fromNode.is_local ? '本机' : fromNode.name}
                      </td>
                      {remoteNodes.map(toNode => {
                        if (fromNode.id === toNode.id) {
                          return (
                            <td key={toNode.id} className="py-2 px-2 text-center text-slate-200 font-bold">—</td>
                          );
                        }
                        const link = findLink(fromNode.id, toNode.id);
                        const applicable = link?.applicable ?? (Boolean(fromNode.is_local) || toNode.auth_method !== 'password');
                        if (!applicable) {
                          return (
                            <td
                              key={toNode.id}
                              className="py-2 px-2 text-center text-slate-200 font-bold"
                              title="目标节点为密码认证，不能由远端连入（不适用）"
                            >
                              —
                            </td>
                          );
                        }
                        const status = link?.status || 'unknown';
                        return (
                          <td key={toNode.id} className="py-2 px-2 text-center">
                            <button
                              onClick={() => onProbe([[fromNode.id, toNode.id]])}
                              className="inline-flex items-center gap-1 px-2 py-1 rounded hover:bg-slate-50 transition-colors"
                              title={
                                status === 'failed'
                                  ? `失败：${link?.last_error || '未知错误'}（点击重测）`
                                  : status === 'ok'
                                    ? `连通 ${link?.latency_ms != null ? `${Math.round(Number(link.latency_ms))}ms` : ''}，上次探测 ${formatTime(link?.last_probe_at) || '未知'}（点击重测）`
                                    : '未探测（点击探测）'
                              }
                            >
                              {status === 'ok' && (
                                <>
                                  <span className="text-emerald-500 font-bold">●</span>
                                  {link?.latency_ms != null && (
                                    <span className="text-[10px] text-slate-500 font-mono tabular-nums">{Math.round(Number(link.latency_ms))}ms</span>
                                  )}
                                </>
                              )}
                              {status === 'failed' && <span className="text-rose-500 font-bold">✕</span>}
                              {status === 'unknown' && <span className="text-slate-300 font-bold">?</span>}
                            </button>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-3 flex items-center gap-4 flex-wrap text-[10px] text-slate-400 font-medium px-1">
              <span><span className="text-emerald-500 font-bold">●</span> 连通（含延迟）</span>
              <span><span className="text-rose-500 font-bold">✕</span> 失败（悬停看原因）</span>
              <span><span className="text-slate-300 font-bold">?</span> 未探测</span>
              <span><span className="text-slate-200 font-bold">—</span> 不适用</span>
              <span>点击单元格可单边重测</span>
            </div>
          </>
        ) : (
          <div className="p-8 text-center text-slate-400 space-y-2">
            <Network className="w-8 h-8 mx-auto opacity-20" />
            <p className="text-sm">注册远程节点后这里会展示连通性矩阵</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ==================== 多终端页（多节点交互终端 + 广播输入 + conda 环境对比） ====================

interface TerminalSessionInfo {
  session_id: string;
  name: string;
  node_id: string;
  node_name: string;
  is_local?: boolean;
  alive?: boolean;
  created_at?: string | null;
  last_activity_at?: string | null;
  exit_code?: number | null;
  exit_reason?: string | null;
  subscriber_count?: number;
  cols?: number;
  rows?: number;
}

interface ArchivedTerminalInfo {
  id: string;
  name: string;
  node_id: string;
  node_name: string;
  is_local: boolean;
  tmux_session: string;
  log_path: string;
  status: string;
  exit_code: number | null;
  exit_reason: string | null;
  created_at: string;
  last_activity_at: string;
  closed_at: string | null;
}

type InteractiveTerminalStreamPayload = {
  session_id?: string;
  data?: string;
  status?: string;
  exit_code?: number | null;
  reason?: string;
};

type AiTerminalKind = 'codex' | 'opencode';

const AI_TERMINAL_WORKDIR = '/home/zjx/GPU/exp-scheduler';

function shellQuote(value: string) {
  return `'${value.replace(/'/g, "'\"'\"'")}'`;
}

function isAiTerminalName(name: string) {
  return /^(Codex|OpenCode) AI\b/.test(name);
}

function buildAiTerminalCommand(kind: AiTerminalKind) {
  const cwd = shellQuote(AI_TERMINAL_WORKDIR);
  if (kind === 'codex') {
    return `codex --no-alt-screen -C ${cwd}\n`;
  }
  return `opencode ${cwd}\n`;
}

interface CondaNodeInventory {
  node_id: string;
  node_name: string;
  status: 'ok' | 'no_conda' | 'timeout' | 'error';
  conda_version?: string | null;
  envs?: string[];
  error?: string | null;
  fetched_at?: string | null;
}

/** 单次 POST 终端输入的最大原始字节数（后端上限 64KB，留余量取 32KB） */
const TERMINAL_INPUT_CHUNK_BYTES = 32768;

/** 字节数组 → base64（分块拼接，避免大输入超出调用栈） */
function bytesToBase64(bytes: Uint8Array) {
  let binary = '';
  const chunkSize = 0x2000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...Array.from(bytes.subarray(index, index + chunkSize)));
  }
  return window.btoa(binary);
}

/** UTF-8 安全的 base64 编码 */
function encodeBase64Utf8(value: string) {
  return bytesToBase64(new TextEncoder().encode(value));
}

/** 把 UTF-8 字节流切成 ≤maxBytes 的片段，且不从多字节字符中间切断（回退跳过 10xxxxxx 续字节） */
function splitUtf8Chunks(bytes: Uint8Array, maxBytes: number): Uint8Array[] {
  const chunks: Uint8Array[] = [];
  let offset = 0;
  while (offset < bytes.length) {
    let end = Math.min(offset + maxBytes, bytes.length);
    if (end < bytes.length) {
      while (end > offset && (bytes[end] & 0xc0) === 0x80) {
        end -= 1;
      }
      if (end === offset) {
        // 兜底防死循环：maxBytes 远大于单字符 4 字节，理论上不可达
        end = Math.min(offset + maxBytes, bytes.length);
      }
    }
    chunks.push(bytes.subarray(offset, end));
    offset = end;
  }
  return chunks;
}

function isXtermNearBottom(terminal: XTerm) {
  const buffer = terminal.buffer.active;
  return buffer.baseY - buffer.viewportY <= 1;
}

function InteractiveTerminal({
  sessionId,
  title,
  streamUrl,
  resizeUrl,
  inputUrl,
  statusSuffix,
  onData,
  onReconnect,
  onRename,
  onClose,
  onViewLog,
}: {
  sessionId: string;
  title: string;
  streamUrl: string;
  resizeUrl: string;
  inputUrl: string;
  statusSuffix: string;
  onData?: (data: string) => void;
  onReconnect?: () => void;
  onRename?: () => void;
  onClose?: () => void;
  onViewLog?: () => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const resizeTimerRef = useRef<number | null>(null);
  const streamResyncTimerRef = useRef<number | null>(null);
  const lastSizeKeyRef = useRef('');
  const autoFollowRef = useRef(true);
  const onDataRef = useRef(onData);
  const hasOpenedStreamRef = useRef(false);
  const [streamRevision, setStreamRevision] = useState(0);
  const [connectionStatus, setConnectionStatus] = useState('连接中');
  const [exitInfo, setExitInfo] = useState<InteractiveTerminalStreamPayload | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    onDataRef.current = onData;
  }, [onData]);

  const sendResize = useCallback(async () => {
    const terminal = terminalRef.current;
    if (!terminal) return;
    const cols = Math.max(2, terminal.cols || 0);
    const rows = Math.max(1, terminal.rows || 0);
    if (!cols || !rows) return;
    const previousSizeKey = lastSizeKeyRef.current;
    const sizeKey = `${sessionId}:${cols}x${rows}`;
    if (lastSizeKeyRef.current === sizeKey) return;
    try {
      await api(resizeUrl, {
        method: 'POST',
        body: JSON.stringify({ cols, rows }),
      });
      lastSizeKeyRef.current = sizeKey;
      if (hasOpenedStreamRef.current && previousSizeKey && previousSizeKey !== sizeKey) {
        if (streamResyncTimerRef.current !== null) {
          window.clearTimeout(streamResyncTimerRef.current);
        }
        streamResyncTimerRef.current = window.setTimeout(() => {
          streamResyncTimerRef.current = null;
          setStreamRevision(value => value + 1);
        }, 180);
      }
    } catch {
      // resize 尽力而为，失败不影响数据流
    }
  }, [resizeUrl, sessionId]);

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
    if (!terminal) return streamUrl;
    try {
      fitAddonRef.current?.fit();
    } catch {
      // 拿不到尺寸时由后端默认尺寸兜底
    }
    const cols = Math.max(2, terminal.cols || 0);
    const rows = Math.max(1, terminal.rows || 0);
    if (!cols || !rows) return streamUrl;
    const separator = streamUrl.includes('?') ? '&' : '?';
    return `${streamUrl}${separator}cols=${cols}&rows=${rows}`;
  }, [streamUrl]);

  const writePayload = useCallback((data?: string, options: { reset?: boolean } = {}) => {
    const terminal = terminalRef.current;
    if (!terminal) return;
    if (options.reset) {
      terminal.reset();
      autoFollowRef.current = true;
    }
    if (!data) return;
    const shouldFollow = Boolean(options.reset || autoFollowRef.current || isXtermNearBottom(terminal));
    try {
      // 交互式 TUI（vim/htop/conda 进度条）必须原始直写，不能做 CR 折叠归一化
      terminal.write(decodeBase64Bytes(data), () => {
        if (shouldFollow) {
          terminal.scrollToBottom();
        }
      });
    } catch {
      terminal.writeln('\r\n[exp-scheduler] 终端数据解析失败');
    }
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const terminal = new XTerm({
      allowProposedApi: false,
      convertEol: false,
      cursorBlink: true,
      disableStdin: false,
      fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.1,
      scrollback: 10000,
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
      autoFollowRef.current = isXtermNearBottom(terminal);
    });
    const dataListener = terminal.onData((data) => {
      const handler = onDataRef.current;
      if (handler) {
        handler(data);
        return;
      }
      // 无父层路由时退化为直接单发到本会话输入接口
      void api(inputUrl, {
        method: 'POST',
        body: JSON.stringify({ data: encodeBase64Utf8(data) }),
      }).catch(() => {});
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
      if (streamResyncTimerRef.current !== null) {
        window.clearTimeout(streamResyncTimerRef.current);
        streamResyncTimerRef.current = null;
      }
      resizeObserver.disconnect();
      dataListener.dispose();
      terminal.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
      lastSizeKeyRef.current = '';
      hasOpenedStreamRef.current = false;
    };
  }, [fitAndResize, inputUrl]);

  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;

    setConnectionStatus('连接中');
    setExitInfo(null);
    terminal.reset();
    terminal.writeln(`[exp-scheduler] connecting interactive terminal ${sessionId}`);

    const source = new EventSource(streamUrlWithCurrentSize());
    hasOpenedStreamRef.current = true;
    let snapshotBuffer = '';

    source.addEventListener('snapshot_start', () => {
      snapshotBuffer = '';
      setConnectionStatus('加载历史...');
    });

    source.addEventListener('snapshot_chunk', (event) => {
      const payload = JSON.parse(event.data) as InteractiveTerminalStreamPayload;
      if (payload.data) {
        snapshotBuffer += payload.data;
      }
    });

    source.addEventListener('snapshot_done', () => {
      setConnectionStatus('交互终端');
      writePayload(snapshotBuffer, { reset: true });
      snapshotBuffer = '';
      window.requestAnimationFrame(fitAndResize);
    });

    source.addEventListener('chunk', (event) => {
      const payload = JSON.parse(event.data) as InteractiveTerminalStreamPayload;
      setConnectionStatus('交互终端');
      writePayload(payload.data);
    });

    source.addEventListener('exit', (event) => {
      const payload = JSON.parse(event.data || '{}') as InteractiveTerminalStreamPayload;
      setExitInfo(payload);
      setConnectionStatus(payload.reason === 'connection_lost' ? '连接已断开' : '会话已结束');
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
  }, [fitAndResize, sessionId, streamRevision, streamUrlWithCurrentSize, writePayload]);

  const handleCopy = useCallback(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;
    const selection = terminal.getSelection();
    if (selection) {
      navigator.clipboard.writeText(selection).catch(() => {});
    }
    setContextMenu(null);
  }, []);

  const handlePaste = useCallback(async () => {
    const terminal = terminalRef.current;
    if (!terminal) return;
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        const handler = onDataRef.current;
        if (handler) {
          handler(text);
        } else {
          void api(inputUrl, {
            method: 'POST',
            body: JSON.stringify({ data: encodeBase64Utf8(text) }),
          }).catch(() => {});
        }
      }
    } catch {
      // clipboard 读取可能被浏览器拒绝
    }
    setContextMenu(null);
  }, [inputUrl]);

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setContextMenu({ x: e.clientX, y: e.clientY });
  }, []);

  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    window.addEventListener('click', close);
    window.addEventListener('scroll', close, true);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('scroll', close, true);
    };
  }, [contextMenu]);

  return (
    <div className="relative h-full w-full" onContextMenu={handleContextMenu}>
      <div className="absolute right-0 top-0 z-10 rounded-bl-lg bg-slate-800/90 px-2 py-1 text-[10px] font-bold text-slate-300">
        {connectionStatus} / {statusSuffix}
      </div>
      <div className="mb-2 flex gap-2 pr-28 text-[10px] font-bold uppercase tracking-widest text-slate-500">
        <Terminal className="h-3.5 w-3.5 text-emerald-500" />
        <span className="truncate">{title}</span>
      </div>
      <div ref={hostRef} className="xterm-host h-[calc(100%-1.5rem)] w-full" />
      {contextMenu && (
        <div
          className="fixed z-50 bg-slate-800 border border-slate-600 rounded-lg shadow-xl py-1 text-xs text-slate-200 min-w-[120px]"
          style={{ left: contextMenu.x, top: contextMenu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <button onClick={handleCopy} className="w-full text-left px-3 py-1.5 hover:bg-slate-700 transition-colors">
            复制选区
          </button>
          <button onClick={handlePaste} className="w-full text-left px-3 py-1.5 hover:bg-slate-700 transition-colors">
            粘贴
          </button>
          {onRename && (
            <button onClick={() => { onRename(); setContextMenu(null); }} className="w-full text-left px-3 py-1.5 hover:bg-slate-700 transition-colors">
              重命名
            </button>
          )}
          {onViewLog && (
            <button onClick={() => { onViewLog(); setContextMenu(null); }} className="w-full text-left px-3 py-1.5 hover:bg-slate-700 transition-colors">
              查看完整日志
            </button>
          )}
          {onClose && (
            <button onClick={() => { onClose(); setContextMenu(null); }} className="w-full text-left px-3 py-1.5 text-rose-400 hover:bg-rose-950/50 transition-colors">
              关闭终端
            </button>
          )}
        </div>
      )}
      {exitInfo && (
        <div className="absolute inset-0 z-20 flex items-center justify-center rounded bg-slate-900/85 backdrop-blur-sm">
          <div className="flex flex-col items-center gap-3 px-4 text-center">
            <AlertCircle className={`h-6 w-6 ${exitInfo.reason === 'connection_lost' ? 'text-amber-400' : 'text-slate-400'}`} />
            <p className="text-sm font-bold text-slate-200">
              {exitInfo.reason === 'connection_lost'
                ? '连接已断开'
                : `会话已结束${exitInfo.exit_code !== null && exitInfo.exit_code !== undefined ? ` (exit ${exitInfo.exit_code})` : ''}`}
            </p>
            {exitInfo.reason === 'connection_lost' && onReconnect && (
              <button
                onClick={onReconnect}
                className="flex items-center gap-2 px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-bold transition-colors"
              >
                <RotateCw className="h-3.5 w-3.5" />
                重连
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function condaStatusLabel(status: CondaNodeInventory['status']) {
  switch (status) {
    case 'ok': return '正常';
    case 'no_conda': return '无 conda';
    case 'timeout': return '超时';
    default: return '错误';
  }
}

function condaStatusBadgeStyle(status: CondaNodeInventory['status']) {
  switch (status) {
    case 'ok': return 'bg-emerald-50 text-emerald-700 border-emerald-100';
    case 'no_conda': return 'bg-slate-50 text-slate-500 border-slate-200';
    case 'timeout': return 'bg-amber-50 text-amber-700 border-amber-100';
    default: return 'bg-rose-50 text-rose-700 border-rose-100';
  }
}

function CondaPage() {
  const [notice, setNotice] = useState<{ text: string; kind: 'info' | 'success' | 'error' } | null>(null);
  return (
    <div className="space-y-6">
      {notice && (
        <div className={`border rounded-xl px-4 py-3 text-xs font-medium shadow-sm flex items-start gap-2 ${
          notice.kind === 'error'
            ? 'bg-rose-50 border-rose-200 text-rose-700'
            : notice.kind === 'success'
              ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
              : 'bg-blue-50 border-blue-200 text-blue-700'
        }`}>
          {notice.kind === 'error'
            ? <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            : <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />}
          <span className="flex-1 whitespace-pre-wrap break-all">{notice.text}</span>
          <button onClick={() => setNotice(null)} className="shrink-0 opacity-60 hover:opacity-100 transition-opacity">
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
      )}
      <CondaComparePanel onNotice={(text, kind) => setNotice({ text, kind: kind || 'info' })} />
    </div>
  );
}

function CondaComparePanel({ onNotice }: { onNotice: (text: string, kind?: 'info' | 'success' | 'error') => void }) {
  const [open, setOpen] = useState(false);
  const [inventory, setInventory] = useState<CondaNodeInventory[] | null>(null);
  const [fetchedAt, setFetchedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const loadInventory = useCallback(async (refresh: boolean) => {
    if (refresh) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const payload = await api<{ nodes?: CondaNodeInventory[]; fetched_at?: string; refreshing?: boolean }>(
        refresh ? '/api/conda/inventory/refresh' : '/api/conda/inventory',
        refresh ? { method: 'POST' } : {},
      );
      setInventory(payload.nodes || []);
      setFetchedAt(payload.fetched_at || null);
    } catch (error) {
      onNotice((error as Error).message, 'error');
    } finally {
      if (refresh) {
        setRefreshing(false);
      } else {
        setLoading(false);
      }
    }
  }, [onNotice]);

  useEffect(() => {
    if (open && inventory === null && !loading) {
      void loadInventory(false);
    }
  }, [open, inventory, loading, loadInventory]);

  const envNames = useMemo(() => {
    const names = new Set<string>();
    (inventory || []).forEach(node => (node.envs || []).forEach(env => names.add(env)));
    return [...names].sort((a, b) => a.localeCompare(b));
  }, [inventory]);

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
      <div className="p-4 bg-slate-50/50 flex items-center justify-between gap-3 flex-wrap">
        <button
          onClick={() => setOpen(prev => !prev)}
          className="flex items-center gap-3 text-left"
        >
          <div className="p-2 bg-emerald-600 rounded-lg text-white">
            <HardDrive className="w-5 h-5" />
          </div>
          <div>
            <h3 className="font-bold text-slate-800 text-base flex items-center gap-2">
              conda 环境对比
              <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`} />
            </h3>
            <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">conda environments matrix</p>
          </div>
        </button>
        <div className="flex items-center gap-3">
          {fetchedAt && (
            <span className="text-[10px] text-slate-400 font-mono">{formatTime(fetchedAt)}</span>
          )}
          {open && (
            <button
              onClick={() => void loadInventory(true)}
              disabled={refreshing}
              className={`flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-bold border transition-all shadow-sm ${
                refreshing
                  ? 'bg-slate-100 text-slate-400 border-slate-200 cursor-not-allowed'
                  : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
              }`}
            >
              {refreshing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCcw className="w-3.5 h-3.5" />}
              刷新
            </button>
          )}
        </div>
      </div>
      {open && (
        <div className="border-t border-slate-100">
          {loading && (
            <div className="p-8 flex items-center justify-center gap-3 text-slate-400">
              <Loader2 className="w-5 h-5 animate-spin text-blue-500" />
              <span className="text-sm font-bold">正在采集各节点 conda 环境...</span>
            </div>
          )}
          {!loading && inventory !== null && (
            inventory.length === 0 ? (
              <div className="p-8 text-center text-slate-400 text-sm">暂无节点数据</div>
            ) : (
              <div className="overflow-x-auto custom-scrollbar">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-slate-100 bg-slate-50/30">
                      <th className="text-left px-4 py-2 text-[10px] font-bold text-slate-400 uppercase tracking-widest whitespace-nowrap">conda 环境</th>
                      {inventory.map(node => (
                        <th key={node.node_id} className="px-3 py-2 text-center align-top">
                          <div className="font-bold text-slate-700 truncate max-w-[160px] mx-auto">{node.node_name}</div>
                          <span className={`inline-block mt-1 px-1.5 py-0.5 rounded border text-[9px] font-bold ${condaStatusBadgeStyle(node.status)}`}>
                            {condaStatusLabel(node.status)}
                          </span>
                          {node.conda_version && (
                            <div className="mt-0.5 text-[9px] text-slate-400 font-mono">{node.conda_version}</div>
                          )}
                          {(node.status === 'timeout' || node.status === 'error') && node.error && (
                            <div className="mt-0.5 text-[9px] text-rose-400 max-w-[160px] truncate mx-auto" title={node.error}>
                              {node.error}
                            </div>
                          )}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {envNames.length === 0 ? (
                      <tr>
                        <td colSpan={1 + inventory.length} className="px-4 py-6 text-center text-slate-400">
                          所有节点均未发现 conda 环境
                        </td>
                      </tr>
                    ) : (
                      envNames.map(env => (
                        <tr key={env} className="border-b border-slate-50 hover:bg-slate-50/40 transition-colors">
                          <td className="px-4 py-1.5 font-mono text-slate-600 whitespace-nowrap">{env}</td>
                          {inventory.map(node => {
                            if (node.status !== 'ok') {
                              return (
                                <td key={node.node_id} className="px-3 py-1.5 text-center text-slate-300 font-bold">—</td>
                              );
                            }
                            const has = (node.envs || []).includes(env);
                            return (
                              <td key={node.node_id} className={`px-3 py-1.5 text-center ${has ? '' : 'bg-rose-50'}`}>
                                {has
                                  ? <span className="text-emerald-600 font-bold">✓</span>
                                  : <span className="text-rose-500 font-bold">✗</span>}
                              </td>
                            );
                          })}
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}

function MultiTerminalPage() {
  const [nodes, setNodes] = useState<SyncNode[]>([]);
  const [sessions, setSessions] = useState<TerminalSessionInfo[]>([]);
  const [disconnectedIds, setDisconnectedIds] = useState<Set<string>>(new Set());
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [broadcastOn, setBroadcastOn] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState('local');
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<{ text: string; kind: 'info' | 'success' | 'error' } | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');
  const [archivedTerminals, setArchivedTerminals] = useState<ArchivedTerminalInfo[]>([]);
  const [viewingLog, setViewingLog] = useState<{ sessionId: string; name: string; isArchived?: boolean } | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [minimizedIds, setMinimizedIds] = useState<Set<string>>(new Set());
  const [aiSessionIds, setAiSessionIds] = useState<Set<string>>(new Set());

  // 广播路由从 ref 读取，保证 onData 回调引用稳定（不随开关切换重建终端）
  const broadcastOnRef = useRef(false);
  const checkedIdsRef = useRef<Set<string>>(new Set());
  const disconnectedIdsRef = useRef<Set<string>>(new Set());
  const aiSessionIdsRef = useRef<Set<string>>(new Set());
  const inputQueuesRef = useRef(new Map<string, { pending: string; inflight: boolean }>());
  const inputErrorToastRef = useRef(false);

  useEffect(() => {
    broadcastOnRef.current = broadcastOn;
  }, [broadcastOn]);

  useEffect(() => {
    checkedIdsRef.current = checkedIds;
  }, [checkedIds]);

  useEffect(() => {
    disconnectedIdsRef.current = disconnectedIds;
  }, [disconnectedIds]);

  useEffect(() => {
    const inferred = new Set(aiSessionIds);
    sessions.forEach(session => {
      if (isAiTerminalName(session.name)) {
        inferred.add(session.session_id);
      }
    });
    aiSessionIdsRef.current = inferred;
  }, [aiSessionIds, sessions]);

  useEffect(() => {
    const visibleSessions = sessions.filter(session => !minimizedIds.has(session.session_id));
    const activeVisible = Boolean(activeSessionId && visibleSessions.some(session => session.session_id === activeSessionId));
    if (!activeVisible) {
      setActiveSessionId(visibleSessions[0]?.session_id || null);
    }
  }, [activeSessionId, minimizedIds, sessions]);

  const showNotice = useCallback((text: string, kind: 'info' | 'success' | 'error' = 'info') => {
    setNotice({ text, kind });
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 8000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const loadNodes = useCallback(async () => {
    const payload = await api<{ nodes: SyncNode[] }>('/api/nodes');
    setNodes(payload.nodes || []);
  }, []);

  const loadSessions = useCallback(async () => {
    const payload = await api<{ sessions: TerminalSessionInfo[] }>('/api/terminals');
    setSessions(payload.sessions || []);
  }, []);

  const mergeSessions = useCallback(async () => {
    const payload = await api<{ sessions: TerminalSessionInfo[] }>('/api/terminals');
    const fetched = payload.sessions || [];
    setSessions(prev => {
      const known = new Set(prev.map(item => item.session_id));
      const added = fetched.filter(item => !known.has(item.session_id));
      return added.length ? [...prev, ...added] : prev;
    });
  }, []);

  const loadArchived = useCallback(async () => {
    try {
      const payload = await api<{ archives: ArchivedTerminalInfo[] }>('/api/terminals/logs');
      const archives = payload.archives || [];
      setArchivedTerminals(archives);
      return archives;
    } catch {
      // 忽略
      return [];
    }
  }, []);

  const removeSessionLocally = useCallback((sessionId: string) => {
    setSessions(prev => prev.filter(item => item.session_id !== sessionId));
    setActiveSessionId(prev => (prev === sessionId ? null : prev));
    setMinimizedIds(prev => {
      if (!prev.has(sessionId)) return prev;
      const next = new Set(prev);
      next.delete(sessionId);
      return next;
    });
    setCheckedIds(prev => {
      if (!prev.has(sessionId)) return prev;
      const next = new Set(prev);
      next.delete(sessionId);
      return next;
    });
    setDisconnectedIds(prev => {
      if (!prev.has(sessionId)) return prev;
      const next = new Set(prev);
      next.delete(sessionId);
      return next;
    });
    setAiSessionIds(prev => {
      if (!prev.has(sessionId)) return prev;
      const next = new Set(prev);
      next.delete(sessionId);
      return next;
    });
    inputQueuesRef.current.delete(sessionId);
  }, []);

  useEffect(() => {
    loadNodes().catch(error => showNotice((error as Error).message, 'error'));
    loadSessions().catch(error => showNotice((error as Error).message, 'error'));
    loadArchived().catch(() => {});

    const source = new EventSource('/api/events');
    source.addEventListener('update', (event) => {
      let parsed: { type?: string; payload?: Record<string, unknown> };
      try {
        parsed = JSON.parse(((event as MessageEvent).data as string) || '{}');
      } catch {
        return;
      }
      const type = String(parsed?.type || '');
      const payload = (parsed?.payload || {}) as Record<string, unknown>;
      if (type === 'terminal_session_closed') {
        const sessionId = String(payload.session_id || '');
        if (!sessionId) return;
        if (String(payload.reason || '') === 'connection_lost') {
          // 连接断开的会话保留卡片，由覆盖层提供重连入口；同步更新 ref 让广播路由立即生效
          disconnectedIdsRef.current = new Set(disconnectedIdsRef.current).add(sessionId);
          setDisconnectedIds(prev => {
            if (prev.has(sessionId)) return prev;
            const next = new Set(prev);
            next.add(sessionId);
            return next;
          });
          // 从广播勾选集合移除，避免后续输入持续发往已断开会话（卡片保留，勾选取消）
          setCheckedIds(prev => {
            if (!prev.has(sessionId)) return prev;
            const next = new Set(prev);
            next.delete(sessionId);
            return next;
          });
        } else {
          removeSessionLocally(sessionId);
        }
      } else if (type === 'terminal_session_created') {
        // 其他页面/标签创建的会话也纳入布局（合并去重）
        void mergeSessions().catch(() => {});
      }
    });
    return () => {
      source.close();
    };
  }, [loadNodes, loadSessions, loadArchived, mergeSessions, removeSessionLocally, showNotice]);

  // ---------- 输入发送队列：每会话串行 POST，inflight 期间累积 pending ----------

  const pumpInput = useCallback(async (sessionId: string) => {
    const queue = inputQueuesRef.current.get(sessionId);
    if (!queue || queue.inflight) return;
    queue.inflight = true;
    try {
      while (queue.pending) {
        const data = queue.pending;
        queue.pending = '';
        // 大段粘贴按原始字节 ≤32KB 切片（UTF-8 边界对齐）串行发送，避免触发后端单次输入上限
        const chunks = splitUtf8Chunks(new TextEncoder().encode(data), TERMINAL_INPUT_CHUNK_BYTES);
        for (let index = 0; index < chunks.length; index += 1) {
          try {
            await api(`/api/terminals/${sessionId}/input`, {
              method: 'POST',
              body: JSON.stringify({ data: bytesToBase64(chunks[index]) }),
            });
          } catch (error) {
            // 失败时保留未发送部分（含失败的这一片），随下次输入重试，不整体丢弃
            const decoder = new TextDecoder();
            const unsent = chunks.slice(index).map(chunk => decoder.decode(chunk)).join('');
            queue.pending = unsent + queue.pending;
            throw error;
          }
        }
      }
    } catch (error) {
      if (!inputErrorToastRef.current) {
        inputErrorToastRef.current = true;
        showNotice(`终端输入发送失败（未发送内容已保留，将随下次输入重试）: ${(error as Error).message}`, 'error');
        window.setTimeout(() => {
          inputErrorToastRef.current = false;
        }, 5000);
      }
    } finally {
      queue.inflight = false;
    }
  }, [showNotice]);

  const enqueueInput = useCallback((sessionId: string, data: string) => {
    let queue = inputQueuesRef.current.get(sessionId);
    if (!queue) {
      queue = { pending: '', inflight: false };
      inputQueuesRef.current.set(sessionId, queue);
    }
    queue.pending += data;
    if (!queue.inflight) {
      void pumpInput(sessionId);
    }
  }, [pumpInput]);

  const handleTerminalData = useCallback((sessionId: string, data: string) => {
    const checked = checkedIdsRef.current;
    const disconnected = disconnectedIdsRef.current;
    const aiIds = aiSessionIdsRef.current;
    // 广播目标剔除已断开会话，避免向已不存在的会话反复 POST 触发 404
    // AI coding TUI 对输入上下文敏感，不参与广播输入。
    const targets = broadcastOnRef.current && checked.has(sessionId) && !aiIds.has(sessionId)
      ? [...checked].filter(id => !disconnected.has(id) && !aiIds.has(id))
      : [sessionId];
    targets.forEach(id => enqueueInput(id, data));
  }, [enqueueInput]);

  // ---------- 会话生命周期 ----------

  const createTerminal = useCallback(async (nodeId: string, name?: string, startupCommand?: string) => {
    const payload = await api<{ session: TerminalSessionInfo }>('/api/terminals', {
      method: 'POST',
      body: JSON.stringify({ node_id: nodeId, cols: 120, rows: 30, name, startup_command: startupCommand }),
    });
    return payload.session;
  }, []);

  const handleCreate = async () => {
    setCreating(true);
    try {
      const session = await createTerminal(selectedNodeId);
      setSessions(prev => (prev.some(item => item.session_id === session.session_id) ? prev : [...prev, session]));
      setMinimizedIds(prev => {
        if (!prev.has(session.session_id)) return prev;
        const next = new Set(prev);
        next.delete(session.session_id);
        return next;
      });
      setActiveSessionId(session.session_id);
    } catch (error) {
      showNotice((error as Error).message, 'error');
    } finally {
      setCreating(false);
    }
  };

  const handleCreateAiTerminal = async (kind: AiTerminalKind) => {
    setCreating(true);
    try {
      const label = kind === 'codex' ? 'Codex AI' : 'OpenCode AI';
      const session = await createTerminal(selectedNodeId, label, buildAiTerminalCommand(kind));
      setSessions(prev => (prev.some(item => item.session_id === session.session_id) ? prev : [...prev, session]));
      setAiSessionIds(prev => new Set(prev).add(session.session_id));
      setMinimizedIds(prev => {
        if (!prev.has(session.session_id)) return prev;
        const next = new Set(prev);
        next.delete(session.session_id);
        return next;
      });
      setCheckedIds(prev => {
        if (!prev.has(session.session_id)) return prev;
        const next = new Set(prev);
        next.delete(session.session_id);
        return next;
      });
      setActiveSessionId(session.session_id);
    } catch (error) {
      showNotice((error as Error).message, 'error');
    } finally {
      setCreating(false);
    }
  };

  const handleReconnect = async (oldSession: TerminalSessionInfo) => {
    try {
      const session = await createTerminal(oldSession.node_id);
      setSessions(prev => {
        // 新会话可能已被 SSE merge 追加，先去掉再原位替换
        const withoutNew = prev.filter(item => item.session_id !== session.session_id);
        if (!withoutNew.some(item => item.session_id === oldSession.session_id)) {
          return [...withoutNew, session];
        }
        return withoutNew.map(item => (item.session_id === oldSession.session_id ? session : item));
      });
      setCheckedIds(prev => {
        if (!prev.has(oldSession.session_id)) return prev;
        const next = new Set(prev);
        next.delete(oldSession.session_id);
        next.add(session.session_id);
        return next;
      });
      setDisconnectedIds(prev => {
        if (!prev.has(oldSession.session_id)) return prev;
        const next = new Set(prev);
        next.delete(oldSession.session_id);
        return next;
      });
      setMinimizedIds(prev => {
        const next = new Set(prev);
        next.delete(oldSession.session_id);
        next.delete(session.session_id);
        return next;
      });
      setActiveSessionId(session.session_id);
      inputQueuesRef.current.delete(oldSession.session_id);
    } catch (error) {
      showNotice((error as Error).message, 'error');
    }
  };

  const handleClose = async (session: TerminalSessionInfo) => {
    const isDisconnected = disconnectedIds.has(session.session_id);
    if (!isDisconnected) {
      const confirmed = window.confirm(
        `确认关闭 ${session.name} 的终端会话吗？\n\n会话中正在运行的命令会被终止，日志将归档保存。`
      );
      if (!confirmed) return;
      try {
        await api(`/api/terminals/${session.session_id}`, { method: 'DELETE' });
      } catch (error) {
        const text = (error as Error).message || '';
        if (!text.includes('不存在')) {
          showNotice(text, 'error');
        }
      }
    }
    removeSessionLocally(session.session_id);
    const archives = await loadArchived();
    if (archives.some(archive => archive.id === session.session_id)) {
      setViewingLog(prev => (
        prev?.sessionId === session.session_id
          ? { sessionId: session.session_id, name: session.name, isArchived: true }
          : prev
      ));
    }
  };

  const handleRename = async (sessionId: string, name: string) => {
    try {
      const payload = await api<{ session: TerminalSessionInfo }>(`/api/terminals/${sessionId}`, {
        method: 'PATCH',
        body: JSON.stringify({ name }),
      });
      setSessions(prev => prev.map(s => s.session_id === sessionId ? { ...s, name: payload.session.name } : s));
    } catch (error) {
      showNotice((error as Error).message, 'error');
    }
  };

  const startEditing = (session: TerminalSessionInfo) => {
    setEditingId(session.session_id);
    setEditingName(session.name);
  };

  const commitEditing = () => {
    if (editingId && editingName.trim()) {
      void handleRename(editingId, editingName.trim());
    }
    setEditingId(null);
    setEditingName('');
  };

  const toggleChecked = (sessionId: string) => {
    if (aiSessionIdsRef.current.has(sessionId)) return;
    setCheckedIds(prev => {
      const next = new Set(prev);
      if (next.has(sessionId)) {
        next.delete(sessionId);
      } else {
        next.add(sessionId);
      }
      return next;
    });
  };

  const minimizeSession = (sessionId: string) => {
    setMinimizedIds(prev => new Set(prev).add(sessionId));
    setActiveSessionId(prev => (prev === sessionId ? null : prev));
  };

  const restoreSession = (sessionId: string) => {
    setMinimizedIds(prev => {
      if (!prev.has(sessionId)) return prev;
      const next = new Set(prev);
      next.delete(sessionId);
      return next;
    });
    setActiveSessionId(sessionId);
  };

  const visibleSessions = sessions.filter(session => !minimizedIds.has(session.session_id));
  const minimizedSessions = sessions.filter(session => minimizedIds.has(session.session_id));
  const activeSession = visibleSessions.find(session => session.session_id === activeSessionId) || visibleSessions[0] || null;
  const noticeStyle = notice?.kind === 'error'
    ? 'bg-rose-50 border-rose-200 text-rose-700'
    : notice?.kind === 'success'
      ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
      : 'bg-blue-50 border-blue-200 text-blue-700';

  const renderSessionPanel = (session: TerminalSessionInfo, variant: 'page' | 'grid') => {
    const disconnected = disconnectedIds.has(session.session_id);
    const isActive = activeSession?.session_id === session.session_id;
    const isAiSession = aiSessionIds.has(session.session_id) || isAiTerminalName(session.name);
    return (
      <div
        key={session.session_id}
        className={`bg-white rounded-xl border shadow-sm overflow-hidden flex flex-col ${
          isActive ? 'border-blue-200 ring-1 ring-blue-100' : 'border-slate-200'
        }`}
      >
        <div className="px-3 py-2 border-b border-slate-100 bg-slate-50/50 flex items-center gap-2 min-w-0">
          <span
            className={`w-2 h-2 rounded-full shrink-0 ${disconnected ? 'bg-rose-500' : 'bg-emerald-500'}`}
            title={disconnected ? '连接已断开' : '会话存活'}
          />
          {editingId === session.session_id ? (
            <input
              autoFocus
              value={editingName}
              onChange={(e) => setEditingName(e.target.value)}
              onBlur={commitEditing}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitEditing();
                if (e.key === 'Escape') { setEditingId(null); setEditingName(''); }
              }}
              className="text-xs font-bold text-slate-700 bg-white border border-blue-400 rounded px-1 py-0.5 outline-none flex-1 min-w-0"
            />
          ) : (
            <button
              type="button"
              className="text-left text-xs font-bold text-slate-700 truncate hover:text-blue-600 flex-1 min-w-0"
              title="点击重命名"
              onClick={() => startEditing(session)}
            >
              {session.name}
            </button>
          )}
          <span className="text-[9px] text-slate-400 truncate shrink-0 max-w-[120px]">{session.node_name}</span>
          {session.is_local && (
            <span className="text-[9px] font-bold text-slate-400 uppercase border border-slate-200 rounded px-1 shrink-0">本机</span>
          )}
          {isAiSession && (
            <span className="text-[9px] font-bold text-violet-600 uppercase border border-violet-100 bg-violet-50 rounded px-1 shrink-0">AI</span>
          )}
          <label
            className={`ml-auto flex items-center gap-1 text-[10px] font-bold uppercase tracking-widest select-none shrink-0 ${
              isAiSession ? 'text-slate-300 cursor-not-allowed' : 'text-slate-400 cursor-pointer'
            }`}
            title={isAiSession ? 'AI 终端不参与广播输入' : '勾选后该终端参与广播输入'}
          >
            <input
              type="checkbox"
              checked={!isAiSession && checkedIds.has(session.session_id)}
              onChange={() => toggleChecked(session.session_id)}
              disabled={isAiSession}
              className="accent-blue-600"
            />
            广播
          </label>
          <button
            onClick={() => minimizeSession(session.session_id)}
            title="最小化终端"
            className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors shrink-0"
          >
            <Minimize2 className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => setViewingLog({ sessionId: session.session_id, name: session.name })}
            title="查看完整日志"
            className="p-1 rounded text-slate-400 hover:text-blue-500 hover:bg-blue-50 transition-colors shrink-0"
          >
            <FileText className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => void handleClose(session)}
            title="关闭终端（日志归档保存）"
            className="p-1 rounded text-slate-400 hover:text-rose-500 hover:bg-rose-50 transition-colors shrink-0"
          >
            <Plus className="w-3.5 h-3.5 rotate-45" />
          </button>
        </div>
        <div className={`bg-slate-900 p-3 ${variant === 'page' ? 'h-[min(78vh,920px)] min-h-[560px]' : 'h-[min(52vh,500px)] min-h-[340px]'}`}>
          <InteractiveTerminal
            sessionId={session.session_id}
            title={session.name}
            streamUrl={`/api/terminals/${session.session_id}/stream`}
            resizeUrl={`/api/terminals/${session.session_id}/resize`}
            inputUrl={`/api/terminals/${session.session_id}/input`}
            statusSuffix={session.session_id.slice(0, 6)}
            onData={(data) => handleTerminalData(session.session_id, data)}
            onReconnect={() => void handleReconnect(session)}
            onRename={() => startEditing(session)}
            onClose={() => void handleClose(session)}
            onViewLog={() => setViewingLog({ sessionId: session.session_id, name: session.name })}
          />
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      {notice && (
        <div className={`border rounded-xl px-4 py-3 text-xs font-medium shadow-sm flex items-start gap-2 ${noticeStyle}`}>
          {notice.kind === 'error'
            ? <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            : <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />}
          <span className="flex-1 whitespace-pre-wrap break-all">{notice.text}</span>
          <button onClick={() => setNotice(null)} className="shrink-0 opacity-60 hover:opacity-100 transition-opacity">
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
      )}

      {/* ---------- 工具栏 ---------- */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm">
        <div className="p-4 flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-blue-600 rounded-lg text-white">
              <SquareTerminal className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-bold text-slate-800 text-base">多节点交互终端</h3>
              <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">interactive terminals</p>
            </div>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <select
              value={selectedNodeId}
              onChange={(event) => setSelectedNodeId(event.target.value)}
              className="bg-white border border-slate-200 rounded-lg px-3 py-2 text-xs text-slate-700 outline-none focus:border-blue-500 transition-colors cursor-pointer"
            >
              {nodes.map(node => (
                <option key={node.id} value={node.id}>{syncNodeOptionLabel(node)}</option>
              ))}
            </select>
            <button
              onClick={() => void handleCreate()}
              disabled={creating}
              className={`flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-bold border transition-all shadow-sm ${
                creating
                  ? 'bg-blue-100 text-blue-300 border-blue-100 cursor-not-allowed'
                  : 'bg-blue-600 text-white border-blue-700 hover:bg-blue-500'
              }`}
            >
              {creating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
              新建终端
            </button>
            <div className="flex items-center gap-1 bg-slate-100 border border-slate-200 rounded-lg p-1">
              <button
                onClick={() => void handleCreateAiTerminal('codex')}
                disabled={creating}
                className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[10px] font-bold transition-colors ${
                  creating
                    ? 'text-slate-300 cursor-not-allowed'
                    : 'bg-white text-violet-700 hover:text-violet-600 shadow-sm'
                }`}
                title="启动 Codex AI 终端"
              >
                <Bot className="w-3.5 h-3.5" />
                Codex
              </button>
              <button
                onClick={() => void handleCreateAiTerminal('opencode')}
                disabled={creating}
                className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[10px] font-bold transition-colors ${
                  creating
                    ? 'text-slate-300 cursor-not-allowed'
                    : 'text-slate-600 hover:text-blue-600'
                }`}
                title="启动 OpenCode AI 终端"
              >
                <Code2 className="w-3.5 h-3.5" />
                OpenCode
              </button>
            </div>
            <div className="flex items-center gap-2 pl-3 border-l border-slate-100">
              <button
                type="button"
                role="switch"
                aria-checked={broadcastOn}
                onClick={() => setBroadcastOn(prev => !prev)}
                className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
                  broadcastOn ? 'bg-blue-600' : 'bg-slate-300'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                    broadcastOn ? 'translate-x-[18px]' : 'translate-x-[2px]'
                  }`}
                />
              </button>
              <span className={`text-xs font-bold ${broadcastOn ? 'text-blue-600' : 'text-slate-500'}`}>广播输入</span>
            </div>
          </div>
        </div>
        <div className="px-4 pb-3 text-[11px] text-slate-400">
          广播开启时，键盘输入将同步发送到所有勾选的终端
        </div>
      </div>

      {/* ---------- 终端区域 ---------- */}
      {sessions.length === 0 ? (
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-12 text-center text-slate-400 space-y-2">
          <SquareTerminal className="w-8 h-8 mx-auto opacity-20" />
          <p className="text-sm">暂无终端会话，选择节点后点击「新建终端」开始</p>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
            <div className="px-3 py-2 border-b border-slate-100 flex items-center justify-between gap-3 flex-wrap">
              <div className="flex items-center gap-2 overflow-x-auto custom-scrollbar min-w-0">
                {visibleSessions.map(session => {
                  const active = activeSession?.session_id === session.session_id;
                  const disconnected = disconnectedIds.has(session.session_id);
                  return (
                    <button
                      key={session.session_id}
                      type="button"
                      onClick={() => setActiveSessionId(session.session_id)}
                      className={`flex items-center gap-2 max-w-[220px] px-3 py-1.5 rounded-lg border text-xs font-bold transition-colors shrink-0 ${
                        active
                          ? 'bg-blue-50 border-blue-200 text-blue-700'
                          : 'bg-white border-slate-200 text-slate-500 hover:bg-slate-50'
                      }`}
                    >
                      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${disconnected ? 'bg-rose-500' : 'bg-emerald-500'}`} />
                      <span className="truncate">{session.name}</span>
                      <span className="text-[9px] text-slate-400 font-mono">{session.session_id.slice(0, 4)}</span>
                    </button>
                  );
                })}
                {visibleSessions.length === 0 && (
                  <span className="text-xs text-slate-400 px-2">所有终端已最小化</span>
                )}
              </div>
            </div>
            {minimizedSessions.length > 0 && (
              <div className="px-3 py-2 bg-slate-50/60 flex items-center gap-2 flex-wrap">
                <span className="text-[10px] font-bold uppercase tracking-widest text-slate-400">已最小化</span>
                {minimizedSessions.map(session => (
                  <button
                    key={session.session_id}
                    type="button"
                    onClick={() => restoreSession(session.session_id)}
                    className="flex items-center gap-1.5 px-2 py-1 rounded-lg border border-slate-200 bg-white text-[10px] font-bold text-slate-600 hover:text-blue-600 hover:border-blue-200 transition-colors"
                  >
                    <Maximize2 className="w-3 h-3" />
                    <span className="max-w-[160px] truncate">{session.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {visibleSessions.length === 0 ? (
            <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-8 text-center text-slate-400">
              <Minimize2 className="w-6 h-6 mx-auto mb-2 opacity-30" />
              <p className="text-sm">终端已最小化，从上方恢复指定终端</p>
            </div>
          ) : (
            <div className="grid">
              {activeSession && (
                <div key={activeSession.session_id} className="min-w-0">
                  {renderSessionPanel(activeSession, 'page')}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ---------- 历史归档日志 ---------- */}
      {archivedTerminals.length > 0 && (
        <ArchivedTerminalsPanel
          archives={archivedTerminals}
          onRefresh={() => void loadArchived()}
        />
      )}

      {/* ---------- 完整日志查看弹层 ---------- */}
      {viewingLog && (
        <TerminalLogViewer
          sessionId={viewingLog.sessionId}
          name={viewingLog.name}
          isArchived={viewingLog.isArchived}
          onClose={() => setViewingLog(null)}
        />
      )}
    </div>
  );
}

function ArchivedTerminalsPanel({
  archives,
  onRefresh,
}: {
  archives: ArchivedTerminalInfo[];
  onRefresh: () => void;
}) {
  const [viewingArchive, setViewingArchive] = useState<ArchivedTerminalInfo | null>(null);

  return (
    <>
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        <div className="p-4 bg-slate-50/50 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-slate-600 rounded-lg text-white">
              <Archive className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-bold text-slate-800 text-base">历史终端日志</h3>
              <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">archived terminal logs</p>
            </div>
          </div>
          <button
            onClick={onRefresh}
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-bold border bg-white text-slate-600 border-slate-200 hover:bg-slate-50 transition-all shadow-sm"
          >
            <RefreshCcw className="w-3.5 h-3.5" />
            刷新
          </button>
        </div>
        <div className="border-t border-slate-100 divide-y divide-slate-50">
          {archives.map(terminal => (
            <div
              key={terminal.id}
              className="px-4 py-3 flex items-center gap-3 hover:bg-slate-50/40 transition-colors"
            >
              <Archive className="w-4 h-4 text-slate-400 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-bold text-slate-700 truncate">{terminal.name}</div>
                <div className="text-[10px] text-slate-400 flex items-center gap-2">
                  <span>{terminal.node_name}</span>
                  {terminal.is_local && <span className="uppercase">本机</span>}
                  {terminal.closed_at && <span>{formatTime(terminal.closed_at)}</span>}
                </div>
              </div>
              <button
                onClick={() => setViewingArchive(terminal)}
                className="flex items-center gap-1 px-2 py-1 rounded-lg text-[10px] font-bold text-blue-600 hover:bg-blue-50 transition-colors shrink-0"
              >
                <FileText className="w-3 h-3" />
                查看日志
              </button>
            </div>
          ))}
        </div>
      </div>
      {viewingArchive && (
        <TerminalLogViewer
          sessionId={viewingArchive.id}
          name={viewingArchive.name}
          isArchived
          onClose={() => setViewingArchive(null)}
        />
      )}
    </>
  );
}

function TerminalLogViewer({
  sessionId,
  name,
  isArchived = false,
  onClose,
}: {
  sessionId: string;
  name: string;
  isArchived?: boolean;
  onClose: () => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const [loading, setLoading] = useState(true);
  const [logSize, setLogSize] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [resolvedArchived, setResolvedArchived] = useState(isArchived);
  const tailSize = 262144; // 256KB initial tail

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
      scrollback: 50000,
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
    terminalRef.current = terminal;
    fitAddonRef.current = fitAddon;
    window.requestAnimationFrame(() => {
      try { fitAddon.fit(); } catch { /* */ }
    });

    return () => {
      terminal.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
    };
  }, []);

  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;

    setLoading(true);
    setError(null);
    setResolvedArchived(isArchived);
    terminal.reset();
    terminal.writeln(`[exp-scheduler] loading log for ${name}...`);

    const url = isArchived
      ? `/api/terminals/logs/${sessionId}?tail=${tailSize}`
      : `/api/terminals/${sessionId}/log?tail=${tailSize}`;

    api<{ data: string; size: number }>(url)
      .then(async payload => {
        let effectivePayload = payload;
        let effectiveArchived = isArchived;
        if (!isArchived && payload.size === 0) {
          try {
            const archivedPayload = await api<{ data: string; size: number }>(
              `/api/terminals/logs/${sessionId}?tail=${tailSize}`,
            );
            if (archivedPayload.size > 0) {
              effectivePayload = archivedPayload;
              effectiveArchived = true;
            }
          } catch {
            // live 日志为空时归档兜底尽力而为
          }
        }
        terminal.reset();
        const bytes = decodeBase64Bytes(effectivePayload.data);
        setLogSize(effectivePayload.size);
        setResolvedArchived(effectiveArchived);
        try {
          terminal.write(bytes);
        } catch {
          terminal.writeln('\r\n[exp-scheduler] 日志数据解析失败');
        }
        if (effectivePayload.size < tailSize) {
          terminal.writeln(`\r\n[exp-scheduler] 日志已全部加载 (${effectivePayload.size} bytes)`);
        } else {
          terminal.writeln(`\r\n[exp-scheduler] 已加载最近 ${effectivePayload.size} bytes（可能还有更早的日志）`);
        }
        setLoading(false);
      })
      .catch(err => {
        setError((err as Error).message);
        setLoading(false);
      });
  }, [sessionId, name, isArchived]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/80 backdrop-blur-sm p-4">
      <div className="bg-slate-900 rounded-xl border border-slate-700 shadow-2xl w-full max-w-5xl h-[80vh] flex flex-col overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700 bg-slate-800/80 flex items-center gap-3">
          <FileText className="w-4 h-4 text-blue-400 shrink-0" />
          <span className="text-sm font-bold text-slate-200 truncate">{name}</span>
          <span className="text-[10px] text-slate-500 font-mono shrink-0">
            {resolvedArchived ? '已归档' : '实时日志'} {logSize > 0 && `/ ${logSize} bytes`}
          </span>
          <button
            onClick={onClose}
            className="ml-auto p-1 rounded text-slate-400 hover:text-rose-400 hover:bg-rose-950/50 transition-colors shrink-0"
          >
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
        <div className="flex-1 p-3 overflow-hidden">
          {error ? (
            <div className="h-full flex items-center justify-center text-rose-400 text-sm">{error}</div>
          ) : (
            <div ref={hostRef} className="h-full w-full" />
          )}
        </div>
      </div>
    </div>
  );
}

// ==================== 目录浏览器 ====================

interface DirEntry {
  name: string;
  type: 'dir' | 'file';
  size: number;
  modified: number;
}

function DirectoryBrowser({
  nodeId,
  initialPath = '~',
  onSelect,
  onClose,
}: {
  nodeId: string;
  initialPath?: string;
  onSelect: (path: string) => void;
  onClose: () => void;
}) {
  const [currentPath, setCurrentPath] = useState(initialPath);
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  const loadDir = useCallback(async (path: string) => {
    setLoading(true);
    setError(null);
    try {
      const payload = await api<{ path: string; entries: DirEntry[] }>(
        `/api/files/browse?node_id=${encodeURIComponent(nodeId)}&path=${encodeURIComponent(path)}`
      );
      setCurrentPath(payload.path);
      setEntries(payload.entries || []);
      setSelectedPath(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [nodeId]);

  useEffect(() => {
    void loadDir(initialPath);
  }, [loadDir, initialPath]);

  const handleEntryClick = (entry: DirEntry) => {
    if (entry.type === 'dir') {
      const newPath = currentPath.endsWith('/') ? currentPath + entry.name : currentPath + '/' + entry.name;
      void loadDir(newPath);
    } else {
      setSelectedPath(currentPath + '/' + entry.name);
    }
  };

  const breadcrumb = currentPath.split('/').filter(Boolean);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/80 backdrop-blur-sm p-4">
      <div className="bg-white rounded-xl border border-slate-200 shadow-2xl w-full max-w-2xl h-[70vh] flex flex-col overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-100 bg-slate-50/50 flex items-center gap-3">
          <FolderOpen className="w-4 h-4 text-blue-500 shrink-0" />
          <span className="text-sm font-bold text-slate-700">选择目录</span>
          <button
            onClick={() => void loadDir('~')}
            title="回到用户根目录"
            className="ml-auto px-2 py-1 rounded-lg text-[10px] font-bold text-slate-500 hover:bg-slate-100 transition-colors shrink-0"
          >
            ~
          </button>
          <button
            onClick={onClose}
            className="p-1 rounded text-slate-400 hover:text-rose-500 hover:bg-rose-50 transition-colors shrink-0"
          >
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
        {/* 面包屑 */}
        <div className="px-4 py-2 border-b border-slate-50 flex items-center gap-1 text-xs text-slate-500 overflow-x-auto">
          <button
            onClick={() => void loadDir('/')}
            className="hover:text-blue-600 transition-colors shrink-0"
          >root</button>
          {breadcrumb.map((part, i) => {
            const path = '/' + breadcrumb.slice(0, i + 1).join('/');
            return (
              <span key={i} className="flex items-center gap-1 shrink-0">
                <span className="text-slate-300">/</span>
                <button
                  onClick={() => void loadDir(path)}
                  className="hover:text-blue-600 transition-colors"
                >{part}</button>
              </span>
            );
          })}
        </div>
        {/* 当前路径输入 */}
        <div className="px-4 py-2 border-b border-slate-50">
          <input
            value={currentPath}
            onChange={(e) => setCurrentPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void loadDir(currentPath);
            }}
            className="w-full text-xs font-mono bg-slate-50 border border-slate-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400"
            spellCheck={false}
          />
        </div>
        {/* 目录列表 */}
        <div className="flex-1 overflow-y-auto custom-scrollbar">
          {loading ? (
            <div className="p-8 flex items-center justify-center text-slate-400">
              <Loader2 className="w-5 h-5 animate-spin mr-2" /> 加载中...
            </div>
          ) : error ? (
            <div className="p-8 text-center text-rose-500 text-sm">{error}</div>
          ) : entries.length === 0 ? (
            <div className="p-8 text-center text-slate-400 text-sm">空目录</div>
          ) : (
            <div className="divide-y divide-slate-50">
              {entries.map((entry) => (
                <button
                  key={entry.name}
                  onClick={() => handleEntryClick(entry)}
                  className={`w-full text-left px-4 py-2 flex items-center gap-3 hover:bg-blue-50/50 transition-colors ${
                    selectedPath === currentPath + '/' + entry.name ? 'bg-blue-50' : ''
                  }`}
                >
                  {entry.type === 'dir' ? (
                    <Folder className="w-4 h-4 text-amber-500 shrink-0" />
                  ) : (
                    <FileText className="w-4 h-4 text-slate-400 shrink-0" />
                  )}
                  <span className="text-sm text-slate-700 truncate flex-1">{entry.name}</span>
                  {entry.type === 'file' && (
                    <span className="text-[10px] text-slate-400 font-mono shrink-0">
                      {entry.size > 1024 ? `${(entry.size / 1024).toFixed(1)}KB` : `${entry.size}B`}
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
        {/* 底栏 */}
        <div className="px-4 py-3 border-t border-slate-100 flex items-center justify-between">
          <span className="text-xs text-slate-500 font-mono truncate">{currentPath}</span>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => void loadDir(currentPath)}
              className="px-3 py-1.5 rounded-lg text-xs font-bold text-slate-600 border border-slate-200 hover:bg-slate-50 transition-colors"
            >
              刷新
            </button>
            <button
              onClick={() => onSelect(currentPath)}
              disabled={!currentPath}
              className="px-3 py-1.5 rounded-lg text-xs font-bold text-white bg-blue-600 hover:bg-blue-500 disabled:bg-slate-300 disabled:cursor-not-allowed transition-colors"
            >
              确认选择
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ==================== 定时备份页 ====================

interface BackupJob {
  id: string;
  name: string;
  src_node_id: string;
  src_path: string;
  dst_node_id: string;
  dst_path: string;
  schedule_type: string;
  schedule_hour: number;
  schedule_minute: number;
  schedule_day_of_week: number | null;
  enabled: boolean;
  delete_extras: boolean;
  created_at: string;
  last_run_at: string | null;
  next_run_at: string | null;
  last_run?: BackupRun | null;
}

interface BackupRun {
  id: number;
  job_id: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  bytes_transferred: number | null;
  files_transferred: number | null;
  exit_code: number | null;
  error: string | null;
  log_path: string | null;
}

function scheduleLabel(job: BackupJob): string {
  const h = String(job.schedule_hour).padStart(2, '0');
  const m = String(job.schedule_minute).padStart(2, '0');
  if (job.schedule_type === 'manual') return '手动';
  if (job.schedule_type === 'daily') return `每天 ${h}:${m}`;
  if (job.schedule_type === 'weekly') {
    const days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    return `${days[job.schedule_day_of_week || 0]} ${h}:${m}`;
  }
  return job.schedule_type;
}

function BackupPage() {
  const [nodes, setNodes] = useState<SyncNode[]>([]);
  const [jobs, setJobs] = useState<BackupJob[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editingJob, setEditingJob] = useState<BackupJob | null>(null);
  const [viewingRuns, setViewingRuns] = useState<BackupJob | null>(null);
  const [viewingLog, setViewingLog] = useState<number | null>(null);
  const [notice, setNotice] = useState<{ text: string; kind: 'info' | 'success' | 'error' } | null>(null);

  const showNotice = useCallback((text: string, kind: 'info' | 'success' | 'error' = 'info') => {
    setNotice({ text, kind });
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const loadNodes = useCallback(async () => {
    const payload = await api<{ nodes: SyncNode[] }>('/api/nodes');
    setNodes(payload.nodes || []);
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const payload = await api<{ jobs: BackupJob[] }>('/api/backups');
      setJobs(payload.jobs || []);
    } catch (err) {
      showNotice((err as Error).message, 'error');
    }
  }, [showNotice]);

  useEffect(() => {
    void loadNodes().catch(() => {});
    void loadJobs().catch(() => {});
    const source = new EventSource('/api/events');
    source.addEventListener('update', (event) => {
      let parsed: { type?: string; payload?: Record<string, unknown> };
      try {
        parsed = JSON.parse(((event as MessageEvent).data as string) || '{}');
      } catch {
        return;
      }
      const type = String(parsed?.type || '');
      if (type === 'backup_job_created' || type === 'backup_job_updated' || type === 'backup_job_deleted' || type === 'backup_run_finished') {
        void loadJobs().catch(() => {});
      }
    });
    return () => { source.close(); };
  }, [loadJobs, loadNodes]);

  const handleToggle = async (job: BackupJob) => {
    try {
      await api(`/api/backups/${job.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: !job.enabled }),
      });
    } catch (err) {
      showNotice((err as Error).message, 'error');
    }
  };

  const handleRunNow = async (job: BackupJob) => {
    try {
      await api(`/api/backups/${job.id}/run`, { method: 'POST' });
      showNotice(`备份任务「${job.name}」已触发`, 'success');
    } catch (err) {
      showNotice((err as Error).message, 'error');
    }
  };

  const handleDelete = async (job: BackupJob) => {
    if (!window.confirm(`确认删除备份任务「${job.name}」吗？\n\n任务历史记录也会一并删除。`)) return;
    try {
      await api(`/api/backups/${job.id}`, { method: 'DELETE' });
    } catch (err) {
      showNotice((err as Error).message, 'error');
    }
  };

  const noticeStyle = notice?.kind === 'error'
    ? 'bg-rose-50 border-rose-200 text-rose-700'
    : notice?.kind === 'success'
      ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
      : 'bg-blue-50 border-blue-200 text-blue-700';

  return (
    <div className="space-y-6">
      {notice && (
        <div className={`border rounded-xl px-4 py-3 text-xs font-medium shadow-sm flex items-start gap-2 ${noticeStyle}`}>
          {notice.kind === 'error'
            ? <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            : <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />}
          <span className="flex-1 whitespace-pre-wrap break-all">{notice.text}</span>
          <button onClick={() => setNotice(null)} className="shrink-0 opacity-60 hover:opacity-100 transition-opacity">
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
      )}

      <div className="bg-white rounded-xl border border-slate-200 shadow-sm">
        <div className="p-4 flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-emerald-600 rounded-lg text-white">
              <Archive className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-bold text-slate-800 text-base">定时备份</h3>
              <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">scheduled incremental backup</p>
            </div>
          </div>
          <button
            onClick={() => { setEditingJob(null); setShowForm(true); }}
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-bold bg-emerald-600 text-white border border-emerald-700 hover:bg-emerald-500 transition-all shadow-sm"
          >
            <Plus className="w-3.5 h-3.5" />
            新建备份任务
          </button>
        </div>
      </div>

      {jobs.length === 0 ? (
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-12 text-center text-slate-400 space-y-2">
          <Archive className="w-8 h-8 mx-auto opacity-20" />
          <p className="text-sm">暂无备份任务，点击「新建备份任务」开始</p>
        </div>
      ) : (
        <div className="space-y-3">
          {jobs.map(job => (
            <div key={job.id} className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
              <div className="p-4 flex items-start gap-4">
                <div className={`p-2 rounded-lg shrink-0 ${job.enabled ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-50 text-slate-400'}`}>
                  <Archive className="w-5 h-5" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-bold text-slate-800">{job.name}</span>
                    <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border ${
                      job.enabled
                        ? 'bg-emerald-50 text-emerald-700 border-emerald-100'
                        : 'bg-slate-50 text-slate-400 border-slate-200'
                    }`}>
                      {job.enabled ? '启用' : '已禁用'}
                    </span>
                    <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border bg-blue-50 text-blue-700 border-blue-100">
                      {scheduleLabel(job)}
                    </span>
                    {job.delete_extras && (
                      <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border bg-amber-50 text-amber-700 border-amber-100">
                        --delete
                      </span>
                    )}
                  </div>
                  <div className="mt-1.5 text-xs text-slate-500 font-mono break-all">
                    {job.src_path} → {job.dst_path}
                  </div>
                  {job.last_run && (
                    <div className="mt-1 text-[10px] text-slate-400">
                      上次运行: {formatTime(job.last_run.started_at)} ·
                      <span className={job.last_run.status === 'succeeded' ? 'text-emerald-600 font-bold' : 'text-rose-500 font-bold'}>
                        {' '}{job.last_run.status === 'succeeded' ? '成功' : '失败'}
                      </span>
                      {job.last_run.error && <span className="text-rose-400 ml-1">({job.last_run.error})</span>}
                    </div>
                  )}
                  {job.next_run_at && job.enabled && job.schedule_type !== 'manual' && (
                    <div className="mt-0.5 text-[10px] text-blue-400">
                      下次运行: {formatTime(job.next_run_at)}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <button
                    onClick={() => void handleRunNow(job)}
                    title="立即执行"
                    className="px-2 py-1.5 rounded-lg text-[10px] font-bold text-blue-600 hover:bg-blue-50 transition-colors"
                  >
                    <PlayCircle className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => { setEditingJob(job); setShowForm(true); }}
                    title="编辑"
                    className="px-2 py-1.5 rounded-lg text-[10px] font-bold text-slate-500 hover:bg-slate-100 transition-colors"
                  >
                    <Pencil className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => void handleToggle(job)}
                    title={job.enabled ? '禁用' : '启用'}
                    className={`px-2 py-1.5 rounded-lg text-[10px] font-bold transition-colors ${
                      job.enabled ? 'text-amber-600 hover:bg-amber-50' : 'text-emerald-600 hover:bg-emerald-50'
                    }`}
                  >
                    {job.enabled ? <Pause className="w-3.5 h-3.5" /> : <PlayCircle className="w-3.5 h-3.5" />}
                  </button>
                  <button
                    onClick={() => setViewingRuns(job)}
                    title="运行历史"
                    className="px-2 py-1.5 rounded-lg text-[10px] font-bold text-slate-500 hover:bg-slate-100 transition-colors"
                  >
                    <History className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => void handleDelete(job)}
                    title="删除"
                    className="px-2 py-1.5 rounded-lg text-[10px] font-bold text-rose-500 hover:bg-rose-50 transition-colors"
                  >
                    <Plus className="w-3.5 h-3.5 rotate-45" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {showForm && (
        <BackupForm
          nodes={nodes}
          editingJob={editingJob}
          onClose={() => { setShowForm(false); setEditingJob(null); }}
          onSaved={() => { setShowForm(false); setEditingJob(null); void loadJobs(); }}
          onNotice={showNotice}
        />
      )}

      {viewingRuns && (
        <BackupRunsModal
          job={viewingRuns}
          onClose={() => setViewingRuns(null)}
          onViewLog={(runId) => setViewingLog(runId)}
        />
      )}

      {viewingLog !== null && (
        <BackupLogModal runId={viewingLog} onClose={() => setViewingLog(null)} />
      )}
    </div>
  );
}

function BackupForm({
  nodes,
  editingJob,
  onClose,
  onSaved,
  onNotice,
}: {
  nodes: SyncNode[];
  editingJob: BackupJob | null;
  onClose: () => void;
  onSaved: () => void;
  onNotice: (text: string, kind?: 'info' | 'success' | 'error') => void;
}) {
  const [name, setName] = useState(editingJob?.name || '');
  const [srcNodeId, setSrcNodeId] = useState(editingJob?.src_node_id || 'local');
  const [srcPath, setSrcPath] = useState(editingJob?.src_path || '');
  const [dstNodeId, setDstNodeId] = useState(editingJob?.dst_node_id || 'local');
  const [dstPath, setDstPath] = useState(editingJob?.dst_path || '');
  const [scheduleType, setScheduleType] = useState(editingJob?.schedule_type || 'manual');
  const [scheduleHour, setScheduleHour] = useState(editingJob?.schedule_hour ?? 2);
  const [scheduleMinute, setScheduleMinute] = useState(editingJob?.schedule_minute ?? 0);
  const [scheduleDow, setScheduleDow] = useState(editingJob?.schedule_day_of_week ?? 0);
  const [deleteExtras, setDeleteExtras] = useState(editingJob?.delete_extras || false);
  const [enabled, setEnabled] = useState(editingJob?.enabled ?? true);
  const [browsing, setBrowsing] = useState<'src' | 'dst' | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleBrowse = (which: 'src' | 'dst') => {
    setBrowsing(which);
  };

  const handleBrowseSelect = (path: string) => {
    if (browsing === 'src') setSrcPath(path);
    else if (browsing === 'dst') setDstPath(path);
    setBrowsing(null);
  };

  const handleSubmit = async () => {
    if (!name.trim()) { onNotice('请输入任务名称', 'error'); return; }
    if (!srcPath.trim()) { onNotice('请选择源目录', 'error'); return; }
    if (!dstPath.trim()) { onNotice('请选择目标目录', 'error'); return; }
    setSubmitting(true);
    try {
      const body = {
        name: name.trim(),
        src_node_id: srcNodeId,
        src_path: srcPath.trim(),
        dst_node_id: dstNodeId,
        dst_path: dstPath.trim(),
        schedule_type: scheduleType,
        schedule_hour: scheduleHour,
        schedule_minute: scheduleMinute,
        schedule_day_of_week: scheduleType === 'weekly' ? scheduleDow : null,
        enabled,
        delete_extras: deleteExtras,
      };
      if (editingJob) {
        await api(`/api/backups/${editingJob.id}`, { method: 'PATCH', body: JSON.stringify(body) });
        onNotice('备份任务已更新', 'success');
      } else {
        await api('/api/backups', { method: 'POST', body: JSON.stringify(body) });
        onNotice('备份任务已创建', 'success');
      }
      onSaved();
    } catch (err) {
      onNotice((err as Error).message, 'error');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-xl border border-slate-200 shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto custom-scrollbar">
        <div className="p-4 border-b border-slate-100 flex items-center gap-3">
          <Archive className="w-4 h-4 text-emerald-500" />
          <span className="text-sm font-bold text-slate-700">{editingJob ? '编辑备份任务' : '新建备份任务'}</span>
          <button onClick={onClose} className="ml-auto p-1 rounded text-slate-400 hover:text-rose-500 hover:bg-rose-50 transition-colors">
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <div>
            <label className="text-xs font-bold text-slate-500 uppercase tracking-wider">任务名称</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：实验数据每日备份"
              className="mt-1 w-full text-sm bg-white border border-slate-200 rounded-lg px-3 py-2 outline-none focus:border-emerald-400"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs font-bold text-slate-500 uppercase tracking-wider">源节点</label>
              <select
                value={srcNodeId}
                onChange={(e) => setSrcNodeId(e.target.value)}
                className="mt-1 w-full text-sm bg-white border border-slate-200 rounded-lg px-3 py-2 outline-none focus:border-emerald-400"
              >
                {nodes.map(n => <option key={n.id} value={n.id}>{syncNodeOptionLabel(n)}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs font-bold text-slate-500 uppercase tracking-wider">目标节点</label>
              <select
                value={dstNodeId}
                onChange={(e) => setDstNodeId(e.target.value)}
                className="mt-1 w-full text-sm bg-white border border-slate-200 rounded-lg px-3 py-2 outline-none focus:border-emerald-400"
              >
                {nodes.map(n => <option key={n.id} value={n.id}>{syncNodeOptionLabel(n)}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className="text-xs font-bold text-slate-500 uppercase tracking-wider">源目录</label>
            <div className="mt-1 flex gap-2">
              <input
                value={srcPath}
                onChange={(e) => setSrcPath(e.target.value)}
                placeholder="/path/to/source"
                className="flex-1 text-sm font-mono bg-white border border-slate-200 rounded-lg px-3 py-2 outline-none focus:border-emerald-400"
                spellCheck={false}
              />
              <button
                onClick={() => handleBrowse('src')}
                className="px-3 py-2 rounded-lg text-xs font-bold text-emerald-600 border border-emerald-200 hover:bg-emerald-50 transition-colors shrink-0"
              >
                <FolderOpen className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          <div>
            <label className="text-xs font-bold text-slate-500 uppercase tracking-wider">目标目录</label>
            <div className="mt-1 flex gap-2">
              <input
                value={dstPath}
                onChange={(e) => setDstPath(e.target.value)}
                placeholder="/path/to/destination"
                className="flex-1 text-sm font-mono bg-white border border-slate-200 rounded-lg px-3 py-2 outline-none focus:border-emerald-400"
                spellCheck={false}
              />
              <button
                onClick={() => handleBrowse('dst')}
                className="px-3 py-2 rounded-lg text-xs font-bold text-emerald-600 border border-emerald-200 hover:bg-emerald-50 transition-colors shrink-0"
              >
                <FolderOpen className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          <div>
            <label className="text-xs font-bold text-slate-500 uppercase tracking-wider">调度方式</label>
            <div className="mt-1 flex gap-3">
              {['manual', 'daily', 'weekly'].map(type => (
                <button
                  key={type}
                  onClick={() => setScheduleType(type)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-bold border transition-all ${
                    scheduleType === type
                      ? 'bg-emerald-600 text-white border-emerald-700'
                      : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
                  }`}
                >
                  {type === 'manual' ? '手动' : type === 'daily' ? '每天' : '每周'}
                </button>
              ))}
            </div>
          </div>
          {scheduleType !== 'manual' && (
            <div className="flex items-center gap-2">
              {scheduleType === 'weekly' && (
                <select
                  value={scheduleDow}
                  onChange={(e) => setScheduleDow(Number(e.target.value))}
                  className="text-sm bg-white border border-slate-200 rounded-lg px-3 py-2 outline-none focus:border-emerald-400"
                >
                  {['周一', '周二', '周三', '周四', '周五', '周六', '周日'].map((d, i) => (
                    <option key={i} value={i}>{d}</option>
                  ))}
                </select>
              )}
              <input
                type="number"
                value={scheduleHour}
                onChange={(e) => setScheduleHour(Math.max(0, Math.min(23, Number(e.target.value))))}
                min={0} max={23}
                className="w-16 text-sm bg-white border border-slate-200 rounded-lg px-3 py-2 outline-none focus:border-emerald-400 text-center"
              />
              <span className="text-sm text-slate-400">:</span>
              <input
                type="number"
                value={scheduleMinute}
                onChange={(e) => setScheduleMinute(Math.max(0, Math.min(59, Number(e.target.value))))}
                min={0} max={59}
                className="w-16 text-sm bg-white border border-slate-200 rounded-lg px-3 py-2 outline-none focus:border-emerald-400 text-center"
              />
              <span className="text-xs text-slate-400">（24小时制）</span>
            </div>
          )}
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 text-xs font-bold text-slate-600 cursor-pointer">
              <input
                type="checkbox"
                checked={deleteExtras}
                onChange={(e) => setDeleteExtras(e.target.checked)}
                className="accent-emerald-600"
              />
              --delete（删除目标端多余文件）
            </label>
            <label className="flex items-center gap-2 text-xs font-bold text-slate-600 cursor-pointer">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="accent-emerald-600"
              />
              启用
            </label>
          </div>
        </div>
        <div className="p-4 border-t border-slate-100 flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-xs font-bold text-slate-600 border border-slate-200 hover:bg-slate-50 transition-colors">
            取消
          </button>
          <button
            onClick={() => void handleSubmit()}
            disabled={submitting}
            className="px-4 py-2 rounded-lg text-xs font-bold text-white bg-emerald-600 hover:bg-emerald-500 disabled:bg-slate-300 transition-colors"
          >
            {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : editingJob ? '保存' : '创建'}
          </button>
        </div>
      </div>
      {browsing && (
        <DirectoryBrowser
          nodeId={browsing === 'src' ? srcNodeId : dstNodeId}
          onSelect={handleBrowseSelect}
          onClose={() => setBrowsing(null)}
        />
      )}
    </div>
  );
}

function BackupRunsModal({
  job,
  onClose,
  onViewLog,
}: {
  job: BackupJob;
  onClose: () => void;
  onViewLog: (runId: number) => void;
}) {
  const [runs, setRuns] = useState<BackupRun[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api<{ runs: BackupRun[] }>(`/api/backups/${job.id}/runs?limit=50`)
      .then(payload => { setRuns(payload.runs || []); setLoading(false); })
      .catch(() => setLoading(false));
  }, [job.id]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/80 backdrop-blur-sm p-4">
      <div className="bg-white rounded-xl border border-slate-200 shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-100 bg-slate-50/50 flex items-center gap-3">
          <History className="w-4 h-4 text-slate-500" />
          <span className="text-sm font-bold text-slate-700 truncate">运行历史 - {job.name}</span>
          <button onClick={onClose} className="ml-auto p-1 rounded text-slate-400 hover:text-rose-500 hover:bg-rose-50 transition-colors shrink-0">
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto custom-scrollbar">
          {loading ? (
            <div className="p-8 flex items-center justify-center text-slate-400">
              <Loader2 className="w-5 h-5 animate-spin mr-2" /> 加载中...
            </div>
          ) : runs.length === 0 ? (
            <div className="p-8 text-center text-slate-400 text-sm">暂无运行记录</div>
          ) : (
            <div className="divide-y divide-slate-50">
              {runs.map(run => (
                <div key={run.id} className="px-4 py-3 flex items-center gap-3 hover:bg-slate-50/40 transition-colors">
                  <div className={`w-2 h-2 rounded-full shrink-0 ${
                    run.status === 'succeeded' ? 'bg-emerald-500' :
                    run.status === 'failed' ? 'bg-rose-500' :
                    run.status === 'running' ? 'bg-blue-500 animate-pulse' : 'bg-slate-300'
                  }`} />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs font-bold text-slate-700">
                      {run.status === 'succeeded' ? '成功' : run.status === 'failed' ? '失败' : run.status === 'running' ? '运行中' : run.status}
                      {run.exit_code !== null && run.exit_code !== 0 && <span className="text-rose-400 ml-2">exit={run.exit_code}</span>}
                    </div>
                    <div className="text-[10px] text-slate-400">
                      {formatTime(run.started_at)}
                      {run.finished_at && ` → ${formatTime(run.finished_at)}`}
                      {run.error && <span className="text-rose-400 ml-2 truncate">{run.error}</span>}
                    </div>
                  </div>
                  <button
                    onClick={() => onViewLog(run.id)}
                    className="px-2 py-1 rounded-lg text-[10px] font-bold text-blue-600 hover:bg-blue-50 transition-colors shrink-0"
                  >
                    <FileText className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function BackupLogModal({ runId, onClose }: { runId: number; onClose: () => void }) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [isFull, setIsFull] = useState(false);

  useEffect(() => {
    setLoading(true);
    api<{ content: string; log_path: string; size: number }>(
      `/api/backups/runs/${runId}/log?full=${isFull ? 'true' : 'false'}`
    )
      .then(payload => { setContent(payload.content || '(日志为空)'); setLoading(false); })
      .catch(err => { setContent((err as Error).message); setLoading(false); });
  }, [runId, isFull]);

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-900/80 backdrop-blur-sm p-4">
      <div className="bg-slate-900 rounded-xl border border-slate-700 shadow-2xl w-full max-w-3xl h-[80vh] flex flex-col overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700 bg-slate-800/80 flex items-center gap-3">
          <FileText className="w-4 h-4 text-blue-400" />
          <span className="text-sm font-bold text-slate-200">备份日志 (run #{runId})</span>
          <button
            onClick={() => setIsFull(prev => !prev)}
            className="ml-auto px-2 py-1 rounded-lg text-[10px] font-bold text-slate-300 hover:bg-slate-700 transition-colors shrink-0"
          >
            {isFull ? '显示尾部' : '加载完整'}
          </button>
          <button onClick={onClose} className="p-1 rounded text-slate-400 hover:text-rose-400 hover:bg-rose-950/50 transition-colors shrink-0">
            <Plus className="w-4 h-4 rotate-45" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto custom-scrollbar p-4">
          {loading ? (
            <div className="text-slate-400 text-sm">加载中...</div>
          ) : (
            <pre className="text-xs font-mono text-slate-300 whitespace-pre-wrap break-all">{content}</pre>
          )}
        </div>
      </div>
    </div>
  );
}
