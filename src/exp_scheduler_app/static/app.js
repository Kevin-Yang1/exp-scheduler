const state = {
  tasks: { queued: [], running: [], history: [], queue_paused: false },
  gpus: [],
  profiles: [],
  discovery: { conda_envs: [], venvs: [], search_roots: [], conda_executable: null },
  selectedLogTaskId: null,
  logTimer: null,
  eventSource: null,
};

const nodes = {
  taskForm: document.getElementById("task-form"),
  formMessage: document.getElementById("form-message"),
  taskProfileSelect: document.getElementById("task-profile-select"),
  profilePreview: document.getElementById("profile-preview"),
  profileForm: document.getElementById("profile-form"),
  profileMessage: document.getElementById("profile-message"),
  profileList: document.getElementById("profile-list"),
  profileSubmitButton: document.getElementById("profile-submit-button"),
  profileCancelButton: document.getElementById("profile-cancel-button"),
  profileScanButton: document.getElementById("profile-scan-button"),
  discoveryMessage: document.getElementById("discovery-message"),
  discoveryList: document.getElementById("discovery-list"),
  queueList: document.getElementById("queue-list"),
  runningList: document.getElementById("running-list"),
  historyList: document.getElementById("history-list"),
  gpuList: document.getElementById("gpu-list"),
  queueToggle: document.getElementById("queue-toggle"),
  refreshButton: document.getElementById("refresh-button"),
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

function normalizeText(value) {
  const text = String(value || "").trim();
  return text || null;
}

function formatTime(iso) {
  if (!iso) return "未开始";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function taskMeta(task) {
  const meta = [];
  if (task.profile_name) meta.push(`环境: ${task.profile_name}`);
  if (task.cwd) meta.push(`目录: ${task.cwd}`);
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

function renderTaskCard(task, kind) {
  const fragment = nodes.template.content.cloneNode(true);
  const card = fragment.querySelector(".task-card");
  const name = fragment.querySelector(".task-name");
  const command = fragment.querySelector(".task-command");
  const badge = fragment.querySelector(".task-badge");
  const meta = fragment.querySelector(".task-meta");
  const notes = fragment.querySelector(".task-notes");
  const actions = fragment.querySelector(".task-actions");

  card.dataset.taskId = task.id;
  name.textContent = task.name;
  command.textContent = task.command;
  badge.textContent = task.status;
  notes.textContent = task.notes || "";
  taskMeta(task).forEach((item) => {
    const span = document.createElement("span");
    span.textContent = item;
    meta.appendChild(span);
  });

  if (kind === "queue") {
    const deleteButton = button("删除", "mini-button danger-button", () => deleteTask(task.id));
    actions.appendChild(deleteButton);
    enableDrag(card);
  } else {
    card.draggable = false;
  }

  if (kind === "running") {
    const cancelButton = button("取消任务", "mini-button danger-button", () => cancelTask(task.id));
    const logButton = button("查看日志", "mini-button", () => selectLogTask(task.id));
    actions.append(cancelButton, logButton);
  }

  if (kind === "history") {
    const logButton = button("查看日志", "mini-button", () => selectLogTask(task.id));
    actions.appendChild(logButton);
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

function enableDrag(card) {
  card.addEventListener("dragstart", () => {
    draggedTaskId = Number(card.dataset.taskId);
    card.classList.add("dragging");
  });

  card.addEventListener("dragend", () => {
    draggedTaskId = null;
    card.classList.remove("dragging");
  });

  card.addEventListener("dragover", (event) => {
    event.preventDefault();
  });

  card.addEventListener("drop", async (event) => {
    event.preventDefault();
    const targetId = Number(card.dataset.taskId);
    if (!draggedTaskId || draggedTaskId === targetId) return;
    const ids = state.tasks.queued.map((task) => task.id);
    const fromIndex = ids.indexOf(draggedTaskId);
    const toIndex = ids.indexOf(targetId);
    ids.splice(toIndex, 0, ids.splice(fromIndex, 1)[0]);
    await reorderQueue(ids);
  });
}

function currentProfileId() {
  return nodes.taskProfileSelect.value ? Number(nodes.taskProfileSelect.value) : null;
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
    nodes.queueList.textContent = "暂无排队任务";
    return;
  }
  nodes.queueList.className = "stack-list";
  state.tasks.queued.forEach((task) => nodes.queueList.appendChild(renderTaskCard(task, "queue")));
}

function renderRunning() {
  nodes.runningList.innerHTML = "";
  if (!state.tasks.running.length) {
    nodes.runningList.className = "stack-list empty-state";
    nodes.runningList.textContent = "当前没有运行中的任务";
    if (!state.selectedLogTaskId) {
      nodes.logTaskName.textContent = "未选择任务";
      nodes.logOutput.textContent = "请选择一个运行中任务查看日志。";
    }
    return;
  }
  nodes.runningList.className = "stack-list";
  state.tasks.running.forEach((task) => nodes.runningList.appendChild(renderTaskCard(task, "running")));
  if (!state.selectedLogTaskId || !state.tasks.running.some((task) => task.id === state.selectedLogTaskId)) {
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
    const title = document.createElement("h3");
    title.textContent = `GPU ${gpu.index} · ${gpu.name}`;
    const status = document.createElement("span");
    status.className = `gpu-status ${gpu.is_idle ? "" : "busy"}`.trim();
    status.textContent = gpu.is_idle ? "空闲可调度" : "忙碌";
    const meta = document.createElement("p");
    meta.textContent = `显存 ${gpu.memory_used_mb}/${gpu.memory_total_mb} MiB · 利用率 ${gpu.utilization_gpu}%`;
    const details = document.createElement("p");
    const reasons = [];
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

function renderProfiles() {
  nodes.profileList.innerHTML = "";
  if (!state.profiles.length) {
    nodes.profileList.className = "stack-list empty-state";
    nodes.profileList.textContent = "暂无环境配置";
    return;
  }
  nodes.profileList.className = "stack-list";
  state.profiles.forEach((profile) => {
    const card = document.createElement("article");
    card.className = "profile-card";

    const top = document.createElement("div");
    top.className = "profile-topline";

    const titleWrap = document.createElement("div");
    const title = document.createElement("h3");
    title.className = "profile-name";
    title.textContent = profile.name;
    titleWrap.appendChild(title);

    const shell = document.createElement("p");
    shell.className = "profile-shell";
    shell.textContent = profile.shell_setup || "无额外激活命令";
    titleWrap.appendChild(shell);

    const badge = document.createElement("span");
    badge.className = "task-badge";
    badge.textContent = `${Object.keys(profile.env || {}).length} 个变量`;

    top.append(titleWrap, badge);

    const meta = document.createElement("div");
    meta.className = "profile-meta";
    const cwd = document.createElement("div");
    cwd.textContent = `默认目录: ${profile.cwd || "未设置"}`;
    const updatedAt = document.createElement("div");
    updatedAt.textContent = `更新于: ${formatTime(profile.updated_at)}`;
    meta.append(cwd, updatedAt);
    if (profile.notes) {
      const notes = document.createElement("div");
      notes.textContent = `备注: ${profile.notes}`;
      meta.appendChild(notes);
    }

    const actions = document.createElement("div");
    actions.className = "profile-actions";
    actions.append(
      button("用于新任务", "mini-button", () => applyProfileToTask(profile.id)),
      button("编辑", "mini-button", () => beginEditProfile(profile.id)),
      button("删除", "mini-button danger-button", () => deleteProfile(profile.id)),
    );

    card.append(top, meta, actions);
    nodes.profileList.appendChild(card);
  });
}

function renderDiscoveryList() {
  const condaEnvs = state.discovery.conda_envs || [];
  const venvs = state.discovery.venvs || [];
  const groups = [
    { title: "Conda 环境", items: condaEnvs },
    { title: "venv / virtualenv", items: venvs },
  ];

  nodes.discoveryList.innerHTML = "";
  if (!condaEnvs.length && !venvs.length) {
    nodes.discoveryList.className = "stack-list empty-state";
    nodes.discoveryList.textContent = "未发现可导入环境";
    return;
  }

  nodes.discoveryList.className = "stack-list";
  groups.forEach((group) => {
    if (!group.items.length) return;
    const groupWrap = document.createElement("section");
    groupWrap.className = "discovery-group";

    const title = document.createElement("h4");
    title.className = "discovery-group-title";
    title.textContent = `${group.title} (${group.items.length})`;
    groupWrap.appendChild(title);

    group.items.forEach((item) => {
      const card = document.createElement("article");
      card.className = "discovery-card";

      const heading = document.createElement("h4");
      heading.textContent = `${item.display_name} -> ${item.suggested_profile.name}`;

      const path = document.createElement("p");
      path.className = "discovery-path";
      path.textContent = item.path;

      const shell = document.createElement("p");
      shell.className = "discovery-shell";
      shell.textContent = item.suggested_profile.shell_setup || "无额外激活命令";

      const meta = document.createElement("div");
      meta.className = "discovery-meta";
      const pythonPath = document.createElement("span");
      pythonPath.textContent = `Python: ${item.python_path}`;
      meta.appendChild(pythonPath);
      if (item.suggested_profile.cwd) {
        const cwd = document.createElement("span");
        cwd.textContent = `默认目录: ${item.suggested_profile.cwd}`;
        meta.appendChild(cwd);
      }

      const actions = document.createElement("div");
      actions.className = "discovery-actions";
      actions.append(
        button("导入模板", "mini-button", () => importDiscoveredProfile(item)),
        button("导入并编辑", "mini-button", () => prefillProfileFormFromDiscovery(item)),
      );

      card.append(heading, path, shell, meta, actions);
      groupWrap.appendChild(card);
    });

    nodes.discoveryList.appendChild(groupWrap);
  });
}

async function loadTasks() {
  state.tasks = await api("/api/tasks");
  renderQueue();
  renderRunning();
  renderHistory();
  renderQueueToggle();
}

async function loadGpus() {
  const payload = await api("/api/gpus");
  state.gpus = payload.gpus || [];
  renderGpus();
}

async function loadProfiles() {
  const payload = await api("/api/profiles");
  state.profiles = payload.profiles || [];
  renderProfiles();
  renderProfileSelect();
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
  await Promise.all([loadTasks(), loadGpus(), loadProfiles()]);
}

async function createTask(event) {
  event.preventDefault();
  const formData = new FormData(nodes.taskForm);
  try {
    const env = parseEnv(formData.get("env") || "");
    await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        name: normalizeText(formData.get("name")),
        command: formData.get("command"),
        cwd: normalizeText(formData.get("cwd")),
        notes: normalizeText(formData.get("notes")),
        env,
        profile_id: currentProfileId(),
      }),
    });
    nodes.taskForm.reset();
    renderProfilePreview();
    nodes.formMessage.textContent = "任务已加入队列。";
    await refreshAll();
  } catch (error) {
    nodes.formMessage.textContent = error.message;
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

async function deleteTask(taskId) {
  await api(`/api/tasks/${taskId}`, { method: "DELETE" });
  await refreshAll();
}

async function reorderQueue(taskIds) {
  await api("/api/tasks/reorder", {
    method: "POST",
    body: JSON.stringify({ task_ids: taskIds }),
  });
  await refreshAll();
}

async function cancelTask(taskId) {
  await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
  await refreshAll();
}

async function requeueTask(taskId) {
  await api(`/api/tasks/${taskId}/requeue`, { method: "POST" });
  await refreshAll();
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
  const task = state.tasks.running.find((item) => item.id === taskId)
    || state.tasks.history.find((item) => item.id === taskId);
  nodes.logTaskName.textContent = task ? task.name : `任务 ${taskId}`;
  await loadLog();
}

async function loadLog() {
  if (!state.selectedLogTaskId) return;
  try {
    const payload = await api(`/api/tasks/${state.selectedLogTaskId}/log`);
    nodes.logOutput.textContent = payload.content || "(日志为空)";
    nodes.logOutput.scrollTop = nodes.logOutput.scrollHeight;
  } catch (error) {
    nodes.logOutput.textContent = `日志加载失败: ${error.message}`;
  }
}

function startLogPolling() {
  if (state.logTimer) clearInterval(state.logTimer);
  state.logTimer = setInterval(() => {
    if (state.selectedLogTaskId) {
      loadLog();
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
  nodes.profileSubmitButton.textContent = "更新环境配置";
  nodes.profileCancelButton.classList.remove("hidden");
  nodes.profileMessage.textContent = `正在编辑「${profile.name}」`;
}

function resetProfileForm() {
  nodes.profileForm.reset();
  nodes.profileForm.elements.profile_id.value = "";
  nodes.profileSubmitButton.textContent = "保存环境配置";
  nodes.profileCancelButton.classList.add("hidden");
}

function applyProfileToTask(profileId) {
  nodes.taskProfileSelect.value = String(profileId);
  renderProfilePreview();
}

function prefillProfileFormFromDiscovery(item) {
  const payload = item.suggested_profile || {};
  resetProfileForm();
  nodes.profileForm.elements.name.value = payload.name || "";
  nodes.profileForm.elements.cwd.value = payload.cwd || "";
  nodes.profileForm.elements.shell_setup.value = payload.shell_setup || "";
  nodes.profileForm.elements.env.value = envToText(payload.env || {});
  nodes.profileForm.elements.notes.value = payload.notes || "";
  nodes.profileMessage.textContent = `已把「${item.display_name}」填入环境配置表单`;
}

nodes.taskForm.addEventListener("submit", createTask);
nodes.profileForm.addEventListener("submit", saveProfile);
nodes.profileCancelButton.addEventListener("click", resetProfileForm);
nodes.profileScanButton.addEventListener("click", scanProfiles);
nodes.taskProfileSelect.addEventListener("change", renderProfilePreview);
nodes.queueToggle.addEventListener("click", toggleQueue);
nodes.refreshButton.addEventListener("click", refreshAll);

refreshAll()
  .then(connectEvents)
  .then(startLogPolling)
  .catch((error) => {
    nodes.formMessage.textContent = error.message;
  });
