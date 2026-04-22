const state = {
  tasks: { queued: [], urgent_queued: [], running: [], history: [], queue_paused: false },
  gpus: [],
  settings: { allowed_gpu_ids: null },
  profiles: [],
  serverInfo: null,
  discovery: { conda_envs: [], venvs: [], search_roots: [], conda_executable: null },
  selectedLogTaskId: null,
  logAutoFollow: true,
  terminal: null,
  terminalFitAddon: null,
  terminalDecoder: null,
  terminalSource: null,
  terminalStreamTaskId: null,
  terminalReconnectTimer: null,
  editingTaskId: null,
  duplicateSourceTaskId: null,
  logTimer: null,
  eventSource: null,
};

const LOG_AUTO_FOLLOW_THRESHOLD = 16;

const nodes = {
  taskForm: document.getElementById("task-form"),
  formMessage: document.getElementById("form-message"),
  taskProfileSelect: document.getElementById("task-profile-select"),
  taskRequestedGpuSelect: document.getElementById("task-requested-gpu-select"),
  taskIsUrgent: document.getElementById("task-is-urgent"),
  taskSubmitButton: document.getElementById("task-submit-button"),
  taskCancelEditButton: document.getElementById("task-cancel-edit-button"),
  taskEditBanner: document.getElementById("task-edit-banner"),
  profilePreview: document.getElementById("profile-preview"),
  profileForm: document.getElementById("profile-form"),
  profileMessage: document.getElementById("profile-message"),
  manageProfileSelect: document.getElementById("manage-profile-select"),
  editProfileBtn: document.getElementById("edit-profile-btn"),
  deleteProfileBtn: document.getElementById("delete-profile-btn"),
  profileSubmitButton: document.getElementById("profile-submit-button"),
  profileCancelButton: document.getElementById("profile-cancel-button"),
  profileScanButton: document.getElementById("profile-scan-button"),
  discoverySelect: document.getElementById("discovery-select"),
  importDiscoveryBtn: document.getElementById("import-discovery-btn"),
  discoveryMessage: document.getElementById("discovery-message"),
  urgentQueueList: document.getElementById("urgent-queue-list"),
  queueList: document.getElementById("queue-list"),
  runningList: document.getElementById("running-list"),
  historyList: document.getElementById("history-list"),
  gpuList: document.getElementById("gpu-list"),
  gpuSettingsForm: document.getElementById("gpu-settings-form"),
  gpuAllowlistOptions: document.getElementById("gpu-allowlist-options"),
  gpuSettingsSummary: document.getElementById("gpu-settings-summary"),
  gpuSettingsMessage: document.getElementById("gpu-settings-message"),
  gpuAllowAllButton: document.getElementById("gpu-allow-all-button"),
  queueToggle: document.getElementById("queue-toggle"),
  refreshButton: document.getElementById("refresh-button"),
  serverIdentityValue: document.getElementById("server-identity-value"),
  logTerminal: document.getElementById("log-terminal"),
  logOutput: document.getElementById("log-output"),
  logTaskName: document.getElementById("log-task-name"),
  template: document.getElementById("task-card-template"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail || response.statusText);
  }
  return response.json().catch(() => ({}));
}

function parseEnv(text) {
  const env = {};
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const index = line.indexOf("=");
    if (index <= 0) {
      throw new Error(`环境变量格式错误: ${line}`);
    }
    const key = line.slice(0, index).trim();
    const value = line.slice(index + 1);
    env[key] = value;
  }
  return env;
}

function envToText(env) {
  return Object.entries(env || {})
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
}

function taskToPayload(task, queueName = task.queue_name || "normal") {
  return {
    name: normalizeText(task.name),
    command: task.command,
    cwd: normalizeText(task.cwd),
    notes: normalizeText(task.notes),
    env: task.env || {},
    is_urgent: queueName === "urgent",
    requested_gpu: task.requested_gpu ?? null,
    profile_id: task.profile_id ?? null,
  };
}

function normalizeText(value) {
  const text = String(value || "").trim();
  return text || null;
}

function hasSelectOption(selectNode, value) {
  return Array.from(selectNode?.options || []).some((option) => option.value === value);
}

function formatTime(iso) {
  if (!iso) return "未开始";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function taskMeta(task) {
  const meta = [];
  if (task.profile_name) meta.push(`环境: ${task.profile_name}`);
  if (task.queue_name === "urgent") meta.push("队列: 紧急");
  if (task.cwd) meta.push(`目录: ${task.cwd}`);
  if (task.requested_gpu !== null && task.requested_gpu !== undefined) {
    meta.push(`指定GPU: ${task.requested_gpu}`);
  }
  if ((task.attempt_count || 0) > 0) {
    meta.push(`尝试次数: ${task.attempt_count || 0}`);
  }
  if (task.next_retry_at) {
    meta.push(`下次重试: ${formatTime(task.next_retry_at)}`);
  }
  if (task.assigned_gpu !== null && task.assigned_gpu !== undefined) {
    meta.push(`GPU: ${task.assigned_gpu}`);
  }
  if (task.started_at) meta.push(`开始: ${formatTime(task.started_at)}`);
  if (task.finished_at) meta.push(`结束: ${formatTime(task.finished_at)}`);
  if (task.exit_code !== null && task.exit_code !== undefined) {
    meta.push(`退出码: ${task.exit_code}`);
  }
  return meta;
}

function renderTaskCard(task, kind, queueName = null) {
  const fragment = nodes.template.content.cloneNode(true);
  const card = fragment.querySelector(".task-card");
  const name = fragment.querySelector(".task-name");
  const command = fragment.querySelector(".task-command");
  const badge = fragment.querySelector(".task-badge");
  const meta = fragment.querySelector(".task-meta");
  const notes = fragment.querySelector(".task-notes");
  const actions = fragment.querySelector(".task-actions");

  card.dataset.taskId = task.id;
  if (task.queue_name === "urgent") {
    card.classList.add("urgent-task");
  }
  if (kind === "queue" && state.editingTaskId === task.id) {
    card.classList.add("editing-task");
  }
  name.textContent = task.name;
  command.textContent = task.command;
  badge.textContent = task.status;
  badge.dataset.status = task.status;
  notes.textContent = task.notes || "";
  taskMeta(task).forEach((item) => {
    const span = document.createElement("span");
    span.textContent = item;
    meta.appendChild(span);
  });

  if (kind === "queue") {
    const editButton = button("编辑", "mini-button", () => beginEditTask(task.id));
    const topButton = button("置顶", "mini-button", () => moveTaskToBoundary(task.id, queueName || "normal", "start"));
    const bottomButton = button("置底", "mini-button", () => moveTaskToBoundary(task.id, queueName || "normal", "end"));
    const deleteButton = button("删除", "mini-button danger-button", () => deleteTask(task.id));
    actions.append(editButton, topButton, bottomButton, deleteButton);
    enableDrag(card, queueName || "normal");
  } else {
    card.draggable = false;
  }

  if (kind === "running") {
    const cancelButton = button("取消任务", "mini-button danger-button", () => cancelTask(task.id));
    const logButton = button("查看日志", "mini-button", () => selectLogTask(task.id));
    actions.append(cancelButton, logButton);
    if (task.queue_name !== "urgent" && (state.tasks.urgent_queued || []).length) {
      const preemptButton = button(
        "抢占回普通队首",
        "mini-button danger-button",
        () => preemptTask(task.id),
      );
      actions.appendChild(preemptButton);
    }
  }

  if (kind === "history") {
    const duplicateButton = button("复用新建", "mini-button", () => beginDuplicateTask(task.id));
    const logButton = button("查看日志", "mini-button", () => selectLogTask(task.id));
    const deleteButton = button(
      "删除记录",
      "mini-button danger-button",
      () => deleteTask(task.id, {
        confirmMessage: `确认删除历史记录「${task.name}」吗？对应日志文件也会一并删除。`,
        successMessage: `已删除历史任务 #${task.id}。`,
      }),
    );
    actions.append(duplicateButton, logButton, deleteButton);
    if (["failed", "cancelled", "interrupted"].includes(task.status)) {
      const requeueButton = button("重新入队", "mini-button", () => requeueTask(task.id));
      actions.appendChild(requeueButton);
    }
  }

  return fragment;
}

function button(label, className, onClick) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = className;
  el.textContent = label;
  el.addEventListener("click", onClick);
  return el;
}

let draggedTaskId = null;
let draggedQueueName = null;

function clearDragIndicators() {
  document.querySelectorAll(".drag-over-card").forEach((node) => {
    node.classList.remove("drag-over-card");
  });
  document.querySelectorAll(".drag-over-zone").forEach((node) => {
    node.classList.remove("drag-over-zone");
  });
}

function queueItemsFor(queueName) {
  return queueName === "urgent" ? state.tasks.urgent_queued : state.tasks.queued;
}

function findQueuedTask(taskId) {
  return [...(state.tasks.urgent_queued || []), ...(state.tasks.queued || [])].find(
    (task) => task.id === taskId
  ) || null;
}

function findTask(taskId) {
  return [
    ...(state.tasks.urgent_queued || []),
    ...(state.tasks.queued || []),
    ...(state.tasks.running || []),
    ...(state.tasks.history || []),
  ].find((task) => task.id === taskId) || null;
}

function shouldUseTerminalView(task) {
  return Boolean(
    task
    && task.status === "running"
    && window.Terminal
    && window.FitAddon
    && window.FitAddon.FitAddon
  );
}

function isTextLogNearBottom() {
  const distanceFromBottom = nodes.logOutput.scrollHeight
    - nodes.logOutput.clientHeight
    - nodes.logOutput.scrollTop;
  return distanceFromBottom <= LOG_AUTO_FOLLOW_THRESHOLD;
}

function showTextLogView() {
  nodes.logTerminal.classList.add("hidden");
  nodes.logOutput.classList.remove("hidden");
}

function ensureTerminalView() {
  if (!shouldUseTerminalView({ status: "running" })) {
    return false;
  }
  if (!state.terminal) {
    state.terminal = new window.Terminal({
      allowProposedApi: false,
      convertEol: false,
      cursorBlink: false,
      disableStdin: true,
      fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, monospace',
      fontSize: 13,
      scrollback: 5000,
      theme: {
        background: "#050505",
        foreground: "#e4e4e7",
        cursor: "#f4f4f5",
        selectionBackground: "rgba(255,255,255,0.14)",
      },
    });
    state.terminalFitAddon = new window.FitAddon.FitAddon();
    state.terminal.loadAddon(state.terminalFitAddon);
    state.terminal.open(nodes.logTerminal);
    state.terminal.onScroll(() => {
      state.logAutoFollow = isTerminalNearBottom();
    });
    window.addEventListener("resize", () => {
      if (!nodes.logTerminal.classList.contains("hidden")) {
        window.requestAnimationFrame(fitTerminalToContainer);
      }
    });
  }
  return true;
}

function showTerminalLogView() {
  if (!ensureTerminalView()) {
    showTextLogView();
    return false;
  }
  nodes.logOutput.classList.add("hidden");
  nodes.logTerminal.classList.remove("hidden");
  window.requestAnimationFrame(fitTerminalToContainer);
  return true;
}

function fitTerminalToContainer() {
  if (!state.terminal || !state.terminalFitAddon || nodes.logTerminal.classList.contains("hidden")) {
    return;
  }
  try {
    state.terminalFitAddon.fit();
  } catch (_error) {}
  if (state.logAutoFollow) {
    state.terminal.scrollToBottom();
  }
}

function isTerminalNearBottom() {
  if (!state.terminal) return true;
  const buffer = state.terminal.buffer.active;
  return (buffer.baseY - buffer.viewportY) <= 1;
}

function resetTerminalBuffer() {
  if (!state.terminal) return;
  state.terminal.reset();
  state.terminalDecoder = new TextDecoder("utf-8");
  state.logAutoFollow = true;
}

function decodeBase64Bytes(encoded) {
  if (!encoded) return new Uint8Array();
  const binary = window.atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function writeTerminalPayload(encoded, options = {}) {
  if (!state.terminal) return;
  const { reset = false } = options;
  if (reset || !state.terminalDecoder) {
    resetTerminalBuffer();
  }
  const bytes = decodeBase64Bytes(encoded);
  const text = state.terminalDecoder.decode(bytes, { stream: true });
  const shouldFollow = state.logAutoFollow;
  if (!text && !bytes.length) {
    if (shouldFollow) {
      state.terminal.scrollToBottom();
    }
    return;
  }
  state.terminal.write(text, () => {
    if (shouldFollow) {
      state.terminal.scrollToBottom();
      state.logAutoFollow = true;
    }
  });
}

function renderTextLogContent(content, options = {}) {
  const { forceScrollToBottom = false } = options;
  const nextContent = content || "(日志为空)";
  const previousBottomOffset = nodes.logOutput.scrollHeight - nodes.logOutput.scrollTop;

  showTextLogView();
  if (nodes.logOutput.textContent !== nextContent) {
    nodes.logOutput.textContent = nextContent;
  }

  if (forceScrollToBottom || state.logAutoFollow) {
    nodes.logOutput.scrollTop = nodes.logOutput.scrollHeight;
    state.logAutoFollow = true;
    return;
  }

  nodes.logOutput.scrollTop = Math.max(0, nodes.logOutput.scrollHeight - previousBottomOffset);
}

async function moveTaskToBoundary(taskId, queueName, boundary) {
  const ids = queueItemsFor(queueName).map((task) => task.id);
  if (!ids.includes(taskId)) return;
  const nextIds = ids.filter((id) => id !== taskId);
  if (boundary === "start") {
    nextIds.unshift(taskId);
  } else {
    nextIds.push(taskId);
  }
  if (ids.join(",") === nextIds.join(",")) return;
  await reorderQueue(nextIds, queueName);
}

async function moveQueuedTask(taskId, targetQueueName, options = {}) {
  const { targetIndex = null } = options;
  const task = findQueuedTask(taskId);
  if (!task) {
    throw new Error("只能移动当前仍在队列中的任务。");
  }
  await api(`/api/tasks/${taskId}`, {
    method: "PUT",
    body: JSON.stringify(taskToPayload(task, targetQueueName)),
  });
  await refreshAll();
  if (targetIndex === null) return;
  const ids = queueItemsFor(targetQueueName)
    .map((item) => item.id)
    .filter((id) => id !== taskId);
  const normalizedIndex = Math.max(0, Math.min(targetIndex, ids.length));
  ids.splice(normalizedIndex, 0, taskId);
  await reorderQueue(ids, targetQueueName);
}

function enableDrag(card, queueName) {
  card.addEventListener("dragstart", () => {
    draggedTaskId = Number(card.dataset.taskId);
    draggedQueueName = queueName;
    clearDragIndicators();
    card.classList.add("dragging");
  });

  card.addEventListener("dragend", () => {
    draggedTaskId = null;
    draggedQueueName = null;
    clearDragIndicators();
    card.classList.remove("dragging");
  });

  card.addEventListener("dragover", (event) => {
    event.preventDefault();
    card.classList.add("drag-over-card");
  });

  card.addEventListener("dragleave", (event) => {
    if (card.contains(event.relatedTarget)) return;
    card.classList.remove("drag-over-card");
  });

  card.addEventListener("drop", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    clearDragIndicators();
    const targetId = Number(card.dataset.taskId);
    if (!draggedTaskId || draggedTaskId === targetId) return;
    try {
      const ids = queueItemsFor(queueName).map((task) => task.id);
      const toIndex = ids.indexOf(targetId);
      if (draggedQueueName === queueName) {
        const fromIndex = ids.indexOf(draggedTaskId);
        ids.splice(toIndex, 0, ids.splice(fromIndex, 1)[0]);
        await reorderQueue(ids, queueName);
      } else {
        await moveQueuedTask(draggedTaskId, queueName, { targetIndex: toIndex });
      }
    } catch (error) {
      nodes.formMessage.textContent = error.message;
    }
  });
}

function enableQueueDropZone(node, queueName) {
  if (!node) return;
  node.addEventListener("dragover", (event) => {
    if (!draggedTaskId) return;
    event.preventDefault();
    if (event.target.closest(".task-card")) return;
    node.classList.add("drag-over-zone");
  });
  node.addEventListener("dragleave", (event) => {
    if (node.contains(event.relatedTarget)) return;
    node.classList.remove("drag-over-zone");
  });
  node.addEventListener("drop", async (event) => {
    if (!draggedTaskId) return;
    event.preventDefault();
    if (event.target.closest(".task-card")) return;
    clearDragIndicators();
    try {
      const ids = queueItemsFor(queueName).map((task) => task.id);
      if (draggedQueueName === queueName) {
        const nextIds = ids.filter((id) => id !== draggedTaskId);
        nextIds.push(draggedTaskId);
        if (ids.join(",") !== nextIds.join(",")) {
          await reorderQueue(nextIds, queueName);
        }
      } else {
        await moveQueuedTask(draggedTaskId, queueName, { targetIndex: ids.length });
      }
    } catch (error) {
      nodes.formMessage.textContent = error.message;
    }
  });
}

function currentProfileId() {
  return nodes.taskProfileSelect.value ? Number(nodes.taskProfileSelect.value) : null;
}

function currentRequestedGpuId() {
  return nodes.taskRequestedGpuSelect.value
    ? Number(nodes.taskRequestedGpuSelect.value)
    : null;
}

function findProfile(profileId) {
  return state.profiles.find((profile) => profile.id === profileId) || null;
}

function renderProfileSelect() {
  const currentValue = nodes.taskProfileSelect.value;
  nodes.taskProfileSelect.innerHTML = "";
  const emptyOption = document.createElement("option");
  emptyOption.value = "";
  emptyOption.textContent = "不使用环境模板";
  nodes.taskProfileSelect.appendChild(emptyOption);
  state.profiles.forEach((profile) => {
    const option = document.createElement("option");
    option.value = String(profile.id);
    option.textContent = profile.name;
    nodes.taskProfileSelect.appendChild(option);
  });
  if (
    currentValue
    && state.profiles.some((profile) => String(profile.id) === currentValue)
  ) {
    nodes.taskProfileSelect.value = currentValue;
  }
  renderProfilePreview();
}

function renderTaskGpuSelect() {
  if (!nodes.taskRequestedGpuSelect) return;
  const currentValue = nodes.taskRequestedGpuSelect.value;
  nodes.taskRequestedGpuSelect.innerHTML = "";
  const autoOption = document.createElement("option");
  autoOption.value = "";
  autoOption.textContent = "自动分配";
  nodes.taskRequestedGpuSelect.appendChild(autoOption);
  state.gpus.forEach((gpu) => {
    const option = document.createElement("option");
    option.value = String(gpu.index);
    option.textContent = `GPU ${gpu.index} · ${gpu.name}`;
    nodes.taskRequestedGpuSelect.appendChild(option);
  });
  if (currentValue && state.gpus.some((gpu) => String(gpu.index) === currentValue)) {
    nodes.taskRequestedGpuSelect.value = currentValue;
  }
}

function gpuSettingsSummaryText() {
  if (!state.gpus.length) {
    return "未检测到 GPU";
  }
  if (state.settings.allowed_gpu_ids === null) {
    return "默认全部可用";
  }
  if (!state.settings.allowed_gpu_ids.length) {
    return "已禁用全部 GPU";
  }
  return `允许 GPU ${state.settings.allowed_gpu_ids.join(", ")}`;
}

function renderGpuSettings() {
  if (!nodes.gpuAllowlistOptions || !nodes.gpuSettingsSummary) return;
  nodes.gpuAllowlistOptions.innerHTML = "";
  nodes.gpuSettingsSummary.textContent = gpuSettingsSummaryText();
  if (!state.gpus.length) {
    const empty = document.createElement("div");
    empty.className = "subtle-note";
    empty.textContent = "当前没有检测到 GPU，稍后刷新即可。";
    nodes.gpuAllowlistOptions.appendChild(empty);
    return;
  }
  const allowedSet = state.settings.allowed_gpu_ids === null
    ? null
    : new Set(state.settings.allowed_gpu_ids);
  state.gpus.forEach((gpu) => {
    const label = document.createElement("label");
    label.className = "gpu-checkbox";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.name = "allowed_gpu_ids";
    checkbox.value = String(gpu.index);
    checkbox.checked = allowedSet ? allowedSet.has(gpu.index) : true;
    const text = document.createElement("span");
    text.textContent = `GPU ${gpu.index}`;
    label.append(checkbox, text);
    nodes.gpuAllowlistOptions.appendChild(label);
  });
}

function renderProfilePreview() {
  const profile = findProfile(currentProfileId());
  if (!profile) {
    nodes.profilePreview.textContent = "当前不使用环境模板；下面填写的工作目录和环境变量会直接作用于任务。";
    return;
  }
  const parts = [`已选择环境模板「${profile.name}」`];
  if (profile.cwd) parts.push(`默认目录：${profile.cwd}`);
  if (Object.keys(profile.env || {}).length) parts.push(`默认环境变量：${Object.keys(profile.env).length} 项`);
  if (profile.shell_setup) parts.push("会先执行激活命令");
  parts.push("任务里填写的工作目录和环境变量会覆盖模板默认值。");
  nodes.profilePreview.textContent = parts.join("；");
}

function renderQueue() {
  nodes.queueList.innerHTML = "";
  if (!state.tasks.queued.length) {
    nodes.queueList.className = "stack-list empty-state";
    nodes.queueList.textContent = "暂无普通排队任务";
    return;
  }
  nodes.queueList.className = "stack-list";
  state.tasks.queued.forEach((task) => nodes.queueList.appendChild(renderTaskCard(task, "queue", "normal")));
}

function renderUrgentQueue() {
  if (!nodes.urgentQueueList) return;
  nodes.urgentQueueList.innerHTML = "";
  if (!state.tasks.urgent_queued.length) {
    nodes.urgentQueueList.className = "stack-list empty-state";
    nodes.urgentQueueList.textContent = "暂无紧急任务";
    return;
  }
  nodes.urgentQueueList.className = "stack-list";
  state.tasks.urgent_queued.forEach((task) => {
    nodes.urgentQueueList.appendChild(renderTaskCard(task, "queue", "urgent"));
  });
}

function renderRunning() {
  nodes.runningList.innerHTML = "";
  if (!state.tasks.running.length) {
    nodes.runningList.className = "stack-list empty-state";
    nodes.runningList.textContent = "当前没有运行中的任务";
    if (!state.selectedLogTaskId) {
      nodes.logTaskName.textContent = "未选择任务";
      showTextLogView();
      nodes.logOutput.textContent = "请选择一个任务查看日志。";
    }
    return;
  }
  nodes.runningList.className = "stack-list";
  state.tasks.running.forEach((task) => nodes.runningList.appendChild(renderTaskCard(task, "running")));
  if (!state.selectedLogTaskId || !findTask(state.selectedLogTaskId)) {
    selectLogTask(state.tasks.running[0].id);
  }
}

function renderHistory() {
  nodes.historyList.innerHTML = "";
  if (!state.tasks.history.length) {
    nodes.historyList.className = "history-list empty-state";
    nodes.historyList.textContent = "暂无历史任务";
    return;
  }
  nodes.historyList.className = "history-list";
  state.tasks.history.forEach((task) => nodes.historyList.appendChild(renderTaskCard(task, "history")));
}

function renderGpus() {
  nodes.gpuList.innerHTML = "";
  state.gpus.forEach((gpu) => {
    const card = document.createElement("article");
    card.className = "gpu-card";
    let statusText = "空闲可调度";
    let statusClass = "";
    if (!gpu.globally_enabled) {
      card.classList.add("disabled-gpu");
      statusText = "全局禁用";
      statusClass = "disabled";
    } else if (!gpu.is_idle) {
      card.classList.add("busy-gpu");
      statusText = "忙碌";
      statusClass = "busy";
    }
    const title = document.createElement("h3");
    title.textContent = `GPU ${gpu.index} · ${gpu.name}`;
    const status = document.createElement("span");
    status.className = `gpu-status ${statusClass}`.trim();
    status.textContent = statusText;
    const meta = document.createElement("p");
    meta.textContent = `显存 ${gpu.memory_used_mb}/${gpu.memory_total_mb} MiB · 利用率 ${gpu.utilization_gpu}%`;
    const details = document.createElement("p");
    const reasons = [];
    if (!gpu.globally_enabled) reasons.push("未纳入全局可用列表");
    if (gpu.has_processes) reasons.push("检测到进程");
    if (gpu.scheduler_occupied) reasons.push("调度器已占用");
    details.textContent = reasons.length ? reasons.join(" / ") : "无额外阻塞";
    card.append(title, status, meta, details);
    nodes.gpuList.appendChild(card);
  });
}

function renderQueueToggle() {
  nodes.queueToggle.textContent = state.tasks.queue_paused ? "恢复调度" : "暂停调度";
}

function renderServerInfo() {
  if (!nodes.serverIdentityValue) return;
  if (!state.serverInfo) {
    nodes.serverIdentityValue.textContent = "读取中...";
    return;
  }
  const name = state.serverInfo.server_name || "unknown-host";
  const ip = state.serverInfo.server_ip || "127.0.0.1";
  nodes.serverIdentityValue.textContent = `${name} (${ip})`;
  document.title = `任务调度看板 · ${name}`;
}

function renderProfiles() {
  nodes.manageProfileSelect.innerHTML = '<option value="">＋ 新建模板 (保持下方表单为空新增)</option>';
  
  if (!state.profiles.length) {
    nodes.editProfileBtn.classList.add("hidden");
    nodes.deleteProfileBtn.classList.add("hidden");
    return;
  }
  
  state.profiles.forEach((profile) => {
    const option = document.createElement("option");
    option.value = String(profile.id);
    option.textContent = profile.name;
    nodes.manageProfileSelect.appendChild(option);
  });
}

function handleManageProfileSelect() {
  const profileId = nodes.manageProfileSelect.value;
  if (!profileId) {
    nodes.editProfileBtn.classList.add("hidden");
    nodes.deleteProfileBtn.classList.add("hidden");
    resetProfileForm();
  } else {
    nodes.editProfileBtn.classList.remove("hidden");
    nodes.deleteProfileBtn.classList.remove("hidden");
  }
}

function renderDiscoveryList() {
  const condaEnvs = state.discovery.conda_envs || [];
  const venvs = state.discovery.venvs || [];
  const allEnvs = [...condaEnvs, ...venvs];
  
  nodes.discoverySelect.innerHTML = "";
  
  if (!allEnvs.length) {
    nodes.discoverySelect.innerHTML = '<option value="">(未发现可导入环境)</option>';
    nodes.discoverySelect.disabled = true;
    nodes.importDiscoveryBtn.classList.add("hidden");
    return;
  }

  nodes.discoverySelect.disabled = false;
  nodes.importDiscoveryBtn.classList.remove("hidden");
  
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = `发现 ${allEnvs.length} 个环境，请选中以导入...`;
  nodes.discoverySelect.appendChild(defaultOption);

  condaEnvs.forEach((env, idx) => {
    const opt = document.createElement("option");
    opt.value = `conda_${idx}`;
    opt.textContent = `[Conda] ${env.display_name}`;
    nodes.discoverySelect.appendChild(opt);
  });

  venvs.forEach((env, idx) => {
    const opt = document.createElement("option");
    opt.value = `venv_${idx}`;
    opt.textContent = `[Venv] ${env.display_name}`;
    nodes.discoverySelect.appendChild(opt);
  });
}

async function loadTasks() {
  const payload = await api("/api/tasks");
  state.tasks = {
    queued: payload.queued || [],
    urgent_queued: payload.urgent_queued || [],
    running: payload.running || [],
    history: payload.history || [],
    queue_paused: Boolean(payload.queue_paused),
  };
  renderUrgentQueue();
  renderQueue();
  renderRunning();
  renderHistory();
  renderQueueToggle();
}

async function loadGpus() {
  const payload = await api("/api/gpus");
  state.gpus = payload.gpus || [];
  renderTaskGpuSelect();
  renderGpuSettings();
  renderGpus();
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  renderGpuSettings();
}

async function loadProfiles() {
  const payload = await api("/api/profiles");
  state.profiles = payload.profiles || [];
  renderProfiles();
  renderProfileSelect();
}

async function loadServerInfo() {
  state.serverInfo = await api("/api/server");
  renderServerInfo();
}

async function scanProfiles() {
  nodes.discoveryMessage.textContent = "正在扫描 conda env 和 venv...";
  try {
    const payload = await api("/api/profiles/discovery");
    state.discovery = {
      conda_envs: payload.conda_envs || [],
      venvs: payload.venvs || [],
      search_roots: payload.search_roots || [],
      conda_executable: payload.conda_executable || null,
    };
    renderDiscoveryList();
    const parts = [];
    if (state.discovery.conda_executable) {
      parts.push(`conda: ${state.discovery.conda_executable}`);
    }
    if (state.discovery.search_roots.length) {
      parts.push(`venv 扫描目录: ${state.discovery.search_roots.join(", ")}`);
    }
    parts.push(
      `共发现 ${state.discovery.conda_envs.length + state.discovery.venvs.length} 个环境`
    );
    nodes.discoveryMessage.textContent = parts.join("；");
  } catch (error) {
    nodes.discoveryMessage.textContent = error.message;
  }
}

async function refreshAll() {
  await Promise.all([loadTasks(), loadGpus(), loadProfiles(), loadSettings()]);
  syncTaskEditState();
  await syncLogSelectionState();
}

async function saveTask(event) {
  event.preventDefault();
  const formData = new FormData(nodes.taskForm);
  const editingTaskId = normalizeText(formData.get("task_id"));
  try {
    const env = parseEnv(formData.get("env") || "");
    const payload = {
      name: normalizeText(formData.get("name")),
      command: formData.get("command"),
      cwd: normalizeText(formData.get("cwd")),
      notes: normalizeText(formData.get("notes")),
      env,
      is_urgent: Boolean(nodes.taskIsUrgent?.checked),
      requested_gpu: currentRequestedGpuId(),
      profile_id: currentProfileId(),
    };
    if (editingTaskId) {
      await api(`/api/tasks/${editingTaskId}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      nodes.formMessage.textContent = `任务 #${editingTaskId} 已更新。`;
    } else {
      await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (state.duplicateSourceTaskId) {
        nodes.formMessage.textContent = `已根据任务 #${state.duplicateSourceTaskId} 创建新任务。`;
      } else {
        nodes.formMessage.textContent = "任务已加入队列。";
      }
    }
    resetTaskForm({ keepMessage: true });
    await refreshAll();
  } catch (error) {
    nodes.formMessage.textContent = error.message;
  }
}

async function saveGpuSettings(event) {
  event.preventDefault();
  try {
    const selected = Array.from(
      nodes.gpuAllowlistOptions.querySelectorAll('input[name="allowed_gpu_ids"]:checked')
    ).map((input) => Number(input.value));
    const allowedGpuIds = selected.length === state.gpus.length ? null : selected;
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ allowed_gpu_ids: allowedGpuIds }),
    });
    if (state.settings.allowed_gpu_ids === null) {
      nodes.gpuSettingsMessage.textContent = "已恢复全部 GPU 可调度。";
    } else if (!state.settings.allowed_gpu_ids.length) {
      nodes.gpuSettingsMessage.textContent = "已禁用全部 GPU 的新任务调度。";
    } else {
      nodes.gpuSettingsMessage.textContent = `已应用 GPU 白名单：${state.settings.allowed_gpu_ids.join(", ")}`;
    }
    renderGpuSettings();
    await refreshAll();
  } catch (error) {
    nodes.gpuSettingsMessage.textContent = error.message;
  }
}

async function allowAllGpus() {
  try {
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ allowed_gpu_ids: null }),
    });
    nodes.gpuSettingsMessage.textContent = "已恢复全部 GPU 可调度。";
    renderGpuSettings();
    await refreshAll();
  } catch (error) {
    nodes.gpuSettingsMessage.textContent = error.message;
  }
}

async function saveProfile(event) {
  event.preventDefault();
  const formData = new FormData(nodes.profileForm);
  const profileId = normalizeText(formData.get("profile_id"));
  try {
    const env = parseEnv(formData.get("env") || "");
    const payload = {
      name: String(formData.get("name") || "").trim(),
      cwd: normalizeText(formData.get("cwd")),
      shell_setup: normalizeText(formData.get("shell_setup")),
      notes: normalizeText(formData.get("notes")),
      env,
    };
    if (!payload.name) {
      throw new Error("环境配置名称不能为空");
    }
    if (profileId) {
      await api(`/api/profiles/${profileId}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      nodes.profileMessage.textContent = "环境配置已更新。";
    } else {
      await api("/api/profiles", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      nodes.profileMessage.textContent = "环境配置已创建。";
    }
    resetProfileForm();
    await refreshAll();
  } catch (error) {
    nodes.profileMessage.textContent = error.message;
  }
}

async function importDiscoveredProfile(item) {
  try {
    const payload = item.suggested_profile || {};
    const response = await api("/api/profiles/import", {
      method: "POST",
      body: JSON.stringify({
        name: payload.name,
        cwd: payload.cwd || null,
        env: payload.env || {},
        shell_setup: payload.shell_setup || null,
        notes: payload.notes || null,
      }),
    });
    const importedProfile = response.profile;
    const renamedMessage = response.renamed_from
      ? `，名称冲突，已自动改为「${importedProfile.name}」`
      : "";
    nodes.profileMessage.textContent = `已导入环境模板「${importedProfile.name}」${renamedMessage}`;
    await refreshAll();
  } catch (error) {
    nodes.profileMessage.textContent = error.message;
  }
}

async function deleteTask(taskId, options = {}) {
  const { confirmMessage = "", successMessage = "" } = options;
  if (confirmMessage && !window.confirm(confirmMessage)) {
    return;
  }
  try {
    await api(`/api/tasks/${taskId}`, { method: "DELETE" });
    if (state.selectedLogTaskId === taskId) {
      clearLogSelection("所选日志任务已删除。");
    }
    await refreshAll();
    if (successMessage) {
      nodes.formMessage.textContent = successMessage;
    }
  } catch (error) {
    nodes.formMessage.textContent = error.message;
  }
}

async function reorderQueue(taskIds, queueName = "normal") {
  await api("/api/tasks/reorder", {
    method: "POST",
    body: JSON.stringify({ task_ids: taskIds, queue_name: queueName }),
  });
  await refreshAll();
}

async function cancelTask(taskId) {
  await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
  await refreshAll();
}

async function preemptTask(taskId) {
  const confirmed = window.confirm(
    "这会终止当前运行中的任务，并把它放回普通队列队首，让紧急队列先执行。是否继续？"
  );
  if (!confirmed) return;
  await api(`/api/tasks/${taskId}/preempt`, { method: "POST" });
  await refreshAll();
}

async function requeueTask(taskId) {
  await api(`/api/tasks/${taskId}/requeue`, { method: "POST" });
  await refreshAll();
}

function beginEditTask(taskId) {
  const task = findQueuedTask(taskId);
  if (!task) {
    nodes.formMessage.textContent = "只能编辑当前仍在队列中的任务。";
    return;
  }
  state.duplicateSourceTaskId = null;
  state.editingTaskId = task.id;
  nodes.taskForm.elements.task_id.value = String(task.id);
  nodes.taskForm.elements.name.value = task.name || "";
  nodes.taskForm.elements.command.value = task.command || "";
  nodes.taskForm.elements.cwd.value = task.cwd || "";
  nodes.taskForm.elements.env.value = envToText(task.env);
  nodes.taskForm.elements.notes.value = task.notes || "";
  nodes.taskIsUrgent.checked = task.queue_name === "urgent";

  const profileValue = task.profile_id !== null && task.profile_id !== undefined
    ? String(task.profile_id)
    : "";
  nodes.taskProfileSelect.value = hasSelectOption(nodes.taskProfileSelect, profileValue)
    ? profileValue
    : "";

  const gpuValue = task.requested_gpu !== null && task.requested_gpu !== undefined
    ? String(task.requested_gpu)
    : "";
  nodes.taskRequestedGpuSelect.value = hasSelectOption(nodes.taskRequestedGpuSelect, gpuValue)
    ? gpuValue
    : "";

  if (nodes.taskSubmitButton) {
    nodes.taskSubmitButton.textContent = "保存任务修改";
  }
  if (nodes.taskCancelEditButton) {
    nodes.taskCancelEditButton.textContent = "取消编辑";
    nodes.taskCancelEditButton.classList.remove("hidden");
  }
  if (nodes.taskEditBanner) {
    nodes.taskEditBanner.textContent = `正在编辑队列任务 #${task.id}「${task.name}」`;
    nodes.taskEditBanner.classList.remove("hidden");
  }
  renderProfilePreview();
  renderUrgentQueue();
  renderQueue();
  nodes.formMessage.textContent = `已载入任务 #${task.id}，保存后会原地更新。`;
  nodes.taskForm.scrollIntoView({ behavior: "smooth", block: "start" });
}

function beginDuplicateTask(taskId) {
  const task = findTask(taskId);
  if (!task) {
    nodes.formMessage.textContent = "找不到要复用的任务。";
    return;
  }
  state.editingTaskId = null;
  state.duplicateSourceTaskId = task.id;
  nodes.taskForm.elements.task_id.value = "";
  nodes.taskForm.elements.name.value = task.name || "";
  nodes.taskForm.elements.command.value = task.command || "";
  nodes.taskForm.elements.cwd.value = task.cwd || "";
  nodes.taskForm.elements.env.value = envToText(task.env);
  nodes.taskForm.elements.notes.value = task.notes || "";
  nodes.taskIsUrgent.checked = task.queue_name === "urgent";

  const profileValue = task.profile_id !== null && task.profile_id !== undefined
    ? String(task.profile_id)
    : "";
  nodes.taskProfileSelect.value = hasSelectOption(nodes.taskProfileSelect, profileValue)
    ? profileValue
    : "";

  const gpuValue = task.requested_gpu !== null && task.requested_gpu !== undefined
    ? String(task.requested_gpu)
    : "";
  nodes.taskRequestedGpuSelect.value = hasSelectOption(nodes.taskRequestedGpuSelect, gpuValue)
    ? gpuValue
    : "";

  if (nodes.taskSubmitButton) {
    nodes.taskSubmitButton.textContent = "按当前内容创建新任务";
  }
  if (nodes.taskCancelEditButton) {
    nodes.taskCancelEditButton.textContent = "取消复用";
    nodes.taskCancelEditButton.classList.remove("hidden");
  }
  if (nodes.taskEditBanner) {
    nodes.taskEditBanner.textContent = `正在基于任务 #${task.id} 复用新建；保存后会创建一个新的任务。`;
    nodes.taskEditBanner.classList.remove("hidden");
  }
  renderProfilePreview();
  renderUrgentQueue();
  renderQueue();
  nodes.formMessage.textContent = `已从任务 #${task.id} 载入参数，你可以改一部分后直接新建。`;
  nodes.taskForm.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function deleteProfile(profileId) {
  const profile = findProfile(profileId);
  if (!window.confirm(`确认删除环境配置「${profile?.name || profileId}」吗？`)) {
    return;
  }
  await api(`/api/profiles/${profileId}`, { method: "DELETE" });
  if (String(profileId) === nodes.profileForm.elements.profile_id.value) {
    resetProfileForm();
  }
  if (String(profileId) === nodes.taskProfileSelect.value) {
    nodes.taskProfileSelect.value = "";
    renderProfilePreview();
  }
  nodes.profileMessage.textContent = "环境配置已删除。";
  await refreshAll();
}

async function toggleQueue() {
  const path = state.tasks.queue_paused ? "/api/queue/resume" : "/api/queue/pause";
  await api(path, { method: "POST" });
  await refreshAll();
}

async function selectLogTask(taskId) {
  state.selectedLogTaskId = taskId;
  state.logAutoFollow = true;
  const task = findTask(taskId);
  nodes.logTaskName.textContent = task ? task.name : `任务 ${taskId}`;
  await loadLog({ forceScrollToBottom: true });
}

function clearLogSelection(message = "请选择一个任务查看日志。") {
  closeTerminalStream();
  state.selectedLogTaskId = null;
  state.logAutoFollow = true;
  nodes.logTaskName.textContent = "未选择任务";
  showTextLogView();
  nodes.logOutput.textContent = message;
}

async function syncLogSelectionState() {
  if (!state.selectedLogTaskId) return;
  if (findTask(state.selectedLogTaskId)) return;
  if (state.tasks.running.length) {
    await selectLogTask(state.tasks.running[0].id);
    return;
  }
  clearLogSelection("所选日志任务已不存在。");
}

function closeTerminalStream() {
  if (state.terminalReconnectTimer) {
    clearTimeout(state.terminalReconnectTimer);
    state.terminalReconnectTimer = null;
  }
  state.terminalSource?.close();
  state.terminalSource = null;
  state.terminalStreamTaskId = null;
  state.terminalDecoder = null;
}

function scheduleTerminalReconnect(taskId) {
  if (state.terminalReconnectTimer) {
    clearTimeout(state.terminalReconnectTimer);
  }
  state.terminalReconnectTimer = setTimeout(() => {
    state.terminalReconnectTimer = null;
    if (state.selectedLogTaskId !== taskId) return;
    const task = findTask(taskId);
    if (!shouldUseTerminalView(task)) return;
    connectTerminalStream(taskId, { resetTerminal: false });
  }, 1500);
}

function connectTerminalStream(taskId, options = {}) {
  const { resetTerminal = false } = options;
  if (!showTerminalLogView()) {
    return false;
  }
  if (state.terminalStreamTaskId === taskId && state.terminalSource) {
    if (resetTerminal) {
      resetTerminalBuffer();
    }
    fitTerminalToContainer();
    return true;
  }

  closeTerminalStream();
  if (resetTerminal) {
    resetTerminalBuffer();
  }

  const source = new EventSource(`/api/tasks/${taskId}/terminal/stream`);
  state.terminalSource = source;
  state.terminalStreamTaskId = taskId;

  source.addEventListener("snapshot", (event) => {
    if (state.selectedLogTaskId !== taskId) return;
    const payload = JSON.parse(event.data);
    showTerminalLogView();
    writeTerminalPayload(payload.data, { reset: true });
    fitTerminalToContainer();
  });

  source.addEventListener("chunk", (event) => {
    if (state.selectedLogTaskId !== taskId) return;
    const payload = JSON.parse(event.data);
    writeTerminalPayload(payload.data);
  });

  source.addEventListener("exit", () => {
    closeTerminalStream();
  });

  source.onerror = () => {
    const task = findTask(taskId);
    closeTerminalStream();
    if (state.selectedLogTaskId === taskId && task && task.status === "running") {
      scheduleTerminalReconnect(taskId);
    }
  };

  return true;
}

async function loadTextLog(options = {}) {
  if (!state.selectedLogTaskId) return;
  closeTerminalStream();
  try {
    const payload = await api(`/api/tasks/${state.selectedLogTaskId}/log`);
    renderTextLogContent(payload.content, options);
  } catch (error) {
    renderTextLogContent(`日志加载失败: ${error.message}`, options);
  }
}

async function loadLog(options = {}) {
  if (!state.selectedLogTaskId) return;
  const task = findTask(state.selectedLogTaskId);
  nodes.logTaskName.textContent = task ? task.name : `任务 ${state.selectedLogTaskId}`;
  if (shouldUseTerminalView(task)) {
    const connected = connectTerminalStream(state.selectedLogTaskId, {
      resetTerminal: Boolean(options.forceScrollToBottom),
    });
    if (connected) {
      return;
    }
  }
  await loadTextLog(options);
}

function startLogPolling() {
  if (state.logTimer) clearInterval(state.logTimer);
  state.logTimer = setInterval(() => {
    if (state.selectedLogTaskId) {
      const task = findTask(state.selectedLogTaskId);
      if (!shouldUseTerminalView(task)) {
        loadLog();
      }
    }
  }, 2000);
}

function connectEvents() {
  state.eventSource?.close();
  const eventSource = new EventSource("/api/events");
  eventSource.addEventListener("update", async () => {
    await refreshAll();
    if (state.selectedLogTaskId) {
      await loadLog();
    }
  });
  eventSource.onerror = () => {
    setTimeout(connectEvents, 2000);
  };
  state.eventSource = eventSource;
}

function beginEditProfile(profileId) {
  const profile = findProfile(profileId);
  if (!profile) return;
  nodes.profileForm.elements.profile_id.value = String(profile.id);
  nodes.profileForm.elements.name.value = profile.name || "";
  nodes.profileForm.elements.cwd.value = profile.cwd || "";
  nodes.profileForm.elements.shell_setup.value = profile.shell_setup || "";
  nodes.profileForm.elements.env.value = envToText(profile.env);
  nodes.profileForm.elements.notes.value = profile.notes || "";
  nodes.profileSubmitButton.textContent = "更新现有配置";
  nodes.profileCancelButton.classList.remove("hidden");
  nodes.profileMessage.textContent = `正在编辑「${profile.name}」`;
  nodes.profileForm.scrollIntoView({ behavior: 'smooth' });
}

function resetTaskForm(options = {}) {
  const { keepMessage = false, message = "" } = options;
  nodes.taskForm.reset();
  nodes.taskForm.elements.task_id.value = "";
  state.editingTaskId = null;
  state.duplicateSourceTaskId = null;
  if (nodes.taskSubmitButton) {
    nodes.taskSubmitButton.textContent = "加入队列排队";
  }
  if (nodes.taskCancelEditButton) {
    nodes.taskCancelEditButton.textContent = "取消编辑";
    nodes.taskCancelEditButton.classList.add("hidden");
  }
  if (nodes.taskEditBanner) {
    nodes.taskEditBanner.textContent = "";
    nodes.taskEditBanner.classList.add("hidden");
  }
  renderTaskGpuSelect();
  renderProfilePreview();
  renderUrgentQueue();
  renderQueue();
  if (!keepMessage) {
    nodes.formMessage.textContent = message;
  }
}

function syncTaskEditState() {
  if (!state.editingTaskId) return;
  if (findQueuedTask(state.editingTaskId)) return;
  resetTaskForm({
    message: "正在编辑的任务已不在队列中，已退出编辑模式。",
  });
}

function resetProfileForm() {
  nodes.profileForm.reset();
  nodes.profileForm.elements.profile_id.value = "";
  nodes.profileSubmitButton.textContent = "💾 保存新模板";
  nodes.profileCancelButton.classList.add("hidden");
  nodes.manageProfileSelect.value = "";
  nodes.editProfileBtn.classList.add("hidden");
  nodes.deleteProfileBtn.classList.add("hidden");
  nodes.profileMessage.textContent = "";
}

nodes.taskForm.addEventListener("submit", saveTask);
nodes.taskCancelEditButton.addEventListener("click", () => {
  const message = state.duplicateSourceTaskId
    ? "已取消复用草稿。"
    : "已取消任务编辑。";
  resetTaskForm({ message });
});
nodes.profileForm.addEventListener("submit", saveProfile);
nodes.profileCancelButton.addEventListener("click", resetProfileForm);
nodes.profileScanButton.addEventListener("click", scanProfiles);
nodes.taskProfileSelect.addEventListener("change", renderProfilePreview);
enableQueueDropZone(nodes.urgentQueueList, "urgent");
enableQueueDropZone(nodes.queueList, "normal");
if (nodes.gpuSettingsForm) {
  nodes.gpuSettingsForm.addEventListener("submit", saveGpuSettings);
}
if (nodes.gpuAllowAllButton) {
  nodes.gpuAllowAllButton.addEventListener("click", allowAllGpus);
}
nodes.queueToggle.addEventListener("click", toggleQueue);
nodes.refreshButton.addEventListener("click", refreshAll);
nodes.logOutput.addEventListener("scroll", () => {
  state.logAutoFollow = isTextLogNearBottom();
});

if (nodes.manageProfileSelect) {
  nodes.manageProfileSelect.addEventListener("change", handleManageProfileSelect);
  nodes.editProfileBtn.addEventListener("click", () => {
    const pid = nodes.manageProfileSelect.value;
    if (pid) beginEditProfile(Number(pid));
  });
  nodes.deleteProfileBtn.addEventListener("click", () => {
    const pid = nodes.manageProfileSelect.value;
    if (pid) deleteProfile(Number(pid));
  });
}

if (nodes.importDiscoveryBtn) {
  nodes.importDiscoveryBtn.addEventListener("click", () => {
    const val = nodes.discoverySelect.value;
    if (!val) return;
    let item;
    if (val.startsWith("conda_")) item = state.discovery.conda_envs[parseInt(val.split("_")[1])];
    if (val.startsWith("venv_")) item = state.discovery.venvs[parseInt(val.split("_")[1])];
    if (item) importDiscoveredProfile(item);
  });
}

refreshAll()
  .then(loadServerInfo)
  .then(connectEvents)
  .then(startLogPolling)
  .catch((error) => {
    nodes.formMessage.textContent = error.message;
  });
