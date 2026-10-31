'use strict';

/* ============================================================
 * ESP-IDF 可视化工具 —— 前端逻辑
 * ============================================================ */

const TARGETS = [
  'esp32', 'esp32s2', 'esp32s3', 'esp32c2', 'esp32c3', 'esp32c6',
  'esp32h2', 'esp32p4', 'esp32c5', 'esp32c61',
];

// 命令元信息：需要哪些参数、弹窗字段定义
const COMMANDS = {
  'create-project': {
    label: '创建工程',
    fields: [{ key: 'name', label: '工程名称', type: 'text', placeholder: 'my_project' }],
  },
  'create-component': {
    label: '创建组件',
    fields: [{ key: 'component', label: '组件名称', type: 'text', placeholder: 'my_component' }],
  },
  'set-target': {
    label: '设置芯片',
    fields: [{ key: 'target', label: '目标芯片', type: 'select', options: TARGETS }],
  },
  'flash': {
    label: '烧录',
    fields: [{ key: 'port', label: '串口', type: 'port' }],
  },
  'monitor': {
    label: '监视输出',
    fields: [{ key: 'port', label: '串口', type: 'port' }],
  },
  'flash-monitor': {
    label: '烧录并监视',
    fields: [{ key: 'port', label: '串口', type: 'port' }],
  },
  'merge-bin': {
    label: '合并 Bin',
    fields: [
      { key: 'output', label: '输出文件', type: 'text', placeholder: 'merged.bin' },
      { key: 'format', label: '格式', type: 'text', placeholder: 'hex' },
    ],
  },
};

const NO_PARAM = new Set(['menuconfig', 'build', 'clean', 'fullclean', 'reconfigure',
  'python-clean', 'size', 'size-components', 'size-files', 'uf2', 'read-otadata', 'docs']);

// ---------- 状态 ----------
let state = {
  config: null,
  projects: [],
  ports: [],
  currentJob: null,
  es: null,
};

// ---------- DOM ----------
const $ = (sel) => document.querySelector(sel);
const el = {
  idfVersion: $('#idf-version'),
  serverStatus: $('#server-status'),
  projectSelect: $('#project-select'),
  portSelect: $('#port-select'),
  btnRefreshProjects: $('#btn-refresh-projects'),
  btnRefreshPorts: $('#btn-refresh-ports'),
  configPanel: $('#config-panel'),
  btnConfigToggle: $('#btn-config-toggle'),
  btnConfigClose: $('#btn-config-close'),
  btnConfigSave: $('#btn-config-save'),
  btnConfigRedetect: $('#btn-config-redetect'),
  cfgIdfPath: $('#cfg-idf-path'),
  cfgToolsPath: $('#cfg-tools-path'),
  cfgPythonEnv: $('#cfg-python-env'),
  cfgProjectsRoot: $('#cfg-projects-root'),
  terminal: $('#terminal'),
  terminalStatus: $('#terminal-status'),
  runningDot: $('#running-dot'),
  runningLabel: $('#running-label'),
  btnStop: $('#btn-stop'),
  btnClear: $('#btn-clear'),
  modalMask: $('#modal-mask'),
  modalTitle: $('#modal-title'),
  modalBody: $('#modal-body'),
  btnModalOk: $('#btn-modal-ok'),
  btnModalCancel: $('#btn-modal-cancel'),
  btnModalClose: $('#btn-modal-close'),
};

let pendingCommand = null;

// ---------- 工具 ----------
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}

function setStatus(ok, text) {
  el.serverStatus.classList.toggle('ok', ok === true);
  el.serverStatus.classList.toggle('err', ok === false);
  el.serverStatus.querySelector('em').textContent = text;
}

// ---------- 终端输出 ----------
function terminalClear() {
  el.terminal.innerHTML = '';
}
function terminalAppend(html, cls) {
  const div = document.createElement('div');
  div.className = cls || 't-line';
  div.textContent = html;
  el.terminal.appendChild(div);
  el.terminal.scrollTop = el.terminal.scrollHeight;
}
function terminalWelcome() {
  terminalClear();
  const div = document.createElement('div');
  div.className = 'terminal-welcome';
  div.innerHTML = '<p>欢迎使用 ESP-IDF 可视化工具 👋</p>' +
    '<p>选择左侧命令按钮即可执行，输出将实时显示在这里。</p>' +
    '<p class="muted">提示：menuconfig 会在新窗口打开；flash / monitor 需先选择串口。</p>';
  el.terminal.appendChild(div);
}

// ---------- 加载配置 / 项目 / 串口 ----------
async function loadConfig() {
  try {
    const cfg = await api('/api/config');
    state.config = cfg;
    state.projects = cfg.projects || [];
    state.ports = cfg.ports || [];
    el.idfVersion.textContent = cfg.idf_version ? `ESP-IDF v${cfg.idf_version}` : 'ESP-IDF（版本未知）';
    fillProjects();
    fillPorts();
    setStatus(true, '已连接');
    return true;
  } catch (e) {
    setStatus(false, '未连接');
    return false;
  }
}

function fillProjects() {
  const sel = el.projectSelect;
  sel.innerHTML = '';
  if (!state.projects.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = '（未检测到工程）';
    sel.appendChild(o);
    return;
  }
  state.projects.forEach((p) => {
    const o = document.createElement('option');
    o.value = p;
    o.textContent = relPath(p);
    o.title = p;
    sel.appendChild(o);
  });
}

function fillPorts() {
  const sel = el.portSelect;
  sel.innerHTML = '';
  if (!state.ports.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = '（未检测到串口）';
    sel.appendChild(o);
    return;
  }
  state.ports.forEach((p) => {
    const o = document.createElement('option');
    o.value = p;
    o.textContent = p;
    sel.appendChild(o);
  });
}

function basename(p) {
  const s = p.replace(/[\\/]+$/, '');
  const i = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
  return i >= 0 ? s.slice(i + 1) : s;
}

// 显示为相对于工程根目录的路径，便于区分同名深层项目
function relPath(p) {
  const root = (state.config && state.config.projects_root) || '';
  if (root) {
    const r = root.replace(/[\\/]+$/, '') + '\\';
    if (p.toLowerCase().startsWith(r.toLowerCase())) {
      const rel = p.slice(r.length);
      return rel || basename(p);
    }
  }
  return basename(p);
}

async function refreshPorts() {
  try {
    const r = await api('/api/ports');
    state.ports = r.ports || [];
    fillPorts();
  } catch (e) { /* ignore */ }
}

async function refreshProjects() {
  if (!state.config) return;
  try {
    const cfg = await api('/api/config');
    state.projects = cfg.projects || [];
    fillProjects();
  } catch (e) { /* ignore */ }
}

// ---------- 配置面板 ----------
function toggleConfig(show) {
  el.configPanel.hidden = show === undefined ? !el.configPanel.hidden : !show;
}
function fillConfigForm() {
  if (!state.config) return;
  el.cfgIdfPath.value = state.config.idf_path || '';
  el.cfgToolsPath.value = state.config.idf_tools_path || '';
  el.cfgPythonEnv.value = state.config.idf_python_env_path || '';
  el.cfgProjectsRoot.value = state.config.projects_root || '';
}

// ---------- 运行命令 ----------
async function runCommand(command, params) {
  const cwd = el.projectSelect.value || '';
  if (!cwd && command !== 'create-project') {
    // 除创建工程外，都需要在工程目录内执行
    if (!state.config || !state.config.projects_root) {
      alert('请先设置工程根目录，并选择一个工程。');
      return;
    }
  }
  // create-project 需要工程根目录作为 cwd
  const effectiveCwd = cwd || (state.config && state.config.projects_root) || '';

  const label = (COMMANDS[command] && COMMANDS[command].label) || command;

  try {
    const r = await api('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command, params, cwd: effectiveCwd, label }),
    });
    startStream(r.job_id, command, label, effectiveCwd);
  } catch (e) {
    terminalAppend(`[错误] ${e.message}`, 't-err');
    setRunning(false);
  }
}

function startStream(jobId, command, label, cwd) {
  if (state.es) { try { state.es.close(); } catch (e) {} }
  state.currentJob = jobId;
  terminalClear();
  terminalAppend(`$ 在 ${cwd} 下执行：${command}`, 't-meta');
  terminalAppend('──────────────────────────────────────────', 't-meta');
  setRunning(true, `${label} 运行中…`);

  const es = new EventSource(`/api/stream?job=${jobId}`);
  state.es = es;

  es.addEventListener('out', (ev) => {
    const data = JSON.parse(ev.data);
    terminalAppend(data.data || '', 't-line');
  });
  es.addEventListener('exit', (ev) => {
    const data = JSON.parse(ev.data);
    const ok = data.code === 0;
    terminalAppend('──────────────────────────────────────────', 't-meta');
    terminalAppend(ok ? `✔ 完成（退出码 0）` : `✘ 结束（退出码 ${data.code}）`,
      ok ? 't-exit-ok' : 't-exit-fail');
    setRunning(false);
    es.close();
    if (state.es === es) state.es = null;
  });
  es.onerror = () => {
    // SSE 连接断开（可能是任务被停止或服务重启）
  };
}

function setRunning(running, text) {
  el.runningDot.hidden = !running;
  el.btnStop.disabled = !running;
  el.terminalStatus.classList.toggle('running', running);
  el.runningLabel.textContent = running ? (text || '运行中') : '';
  el.terminalStatus.textContent = running ? '运行中…' : '就绪';
}

async function stopJob() {
  if (!state.currentJob) return;
  try {
    await api('/api/kill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: state.currentJob }),
    });
    terminalAppend('已发送停止指令', 't-err');
  } catch (e) {
    terminalAppend(`[停止失败] ${e.message}`, 't-err');
  }
  // 稍后重置状态
  setTimeout(() => setRunning(false), 400);
}

// ---------- 弹窗 ----------
function openModal(command) {
  const meta = COMMANDS[command];
  if (!meta) return;
  pendingCommand = command;
  el.modalTitle.textContent = meta.label;
  el.modalBody.innerHTML = '';

  meta.fields.forEach((f) => {
    const label = document.createElement('label');
    label.textContent = f.label;
    let input;
    if (f.type === 'select') {
      input = document.createElement('select');
      f.options.forEach((opt) => {
        const o = document.createElement('option');
        o.value = opt; o.textContent = opt;
        input.appendChild(o);
      });
    } else if (f.type === 'port') {
      input = document.createElement('select');
      const current = el.portSelect.value;
      const ports = state.ports.slice();
      if (current && !ports.includes(current)) ports.unshift(current);
      if (!ports.length) ports.push('');
      ports.forEach((p) => {
        const o = document.createElement('option');
        o.value = p;
        o.textContent = p || '（未检测到串口，请手动输入）';
        if (p === current) o.selected = true;
        input.appendChild(o);
      });
    } else {
      input = document.createElement('input');
      input.type = f.type || 'text';
      input.placeholder = f.placeholder || '';
    }
    input.dataset.key = f.key;
    label.appendChild(input);
    el.modalBody.appendChild(label);
  });

  el.modalMask.hidden = false;
}

function closeModal() {
  el.modalMask.hidden = true;
  pendingCommand = null;
}

function collectParams(command) {
  const meta = COMMANDS[command];
  const params = {};
  meta.fields.forEach((f) => {
    const input = el.modalBody.querySelector(`[data-key="${f.key}"]`);
    params[f.key] = input ? input.value : '';
  });
  return params;
}

// ---------- 事件绑定 ----------
function bindEvents() {
  el.btnRefreshProjects.addEventListener('click', refreshProjects);
  el.btnRefreshPorts.addEventListener('click', refreshPorts);

  el.btnConfigToggle.addEventListener('click', () => { fillConfigForm(); toggleConfig(); });
  el.btnConfigClose.addEventListener('click', () => toggleConfig(false));
  el.btnConfigSave.addEventListener('click', async () => {
    try {
      const body = {
        idf_path: el.cfgIdfPath.value.trim(),
        idf_tools_path: el.cfgToolsPath.value.trim(),
        idf_python_env_path: el.cfgPythonEnv.value.trim(),
        projects_root: el.cfgProjectsRoot.value.trim(),
      };
      const r = await api('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      state.config = r.config;
      state.projects = r.config.projects || [];
      state.ports = r.config.ports || [];
      el.idfVersion.textContent = r.config.idf_version
        ? `ESP-IDF v${r.config.idf_version}` : 'ESP-IDF（版本未知）';
      fillProjects();
      fillPorts();
      terminalAppend('✔ 配置已保存', 't-exit-ok');
    } catch (e) {
      terminalAppend(`[配置保存失败] ${e.message}`, 't-err');
    }
  });
  el.btnConfigRedetect.addEventListener('click', async () => {
    try {
      const r = await api('/api/redetect', { method: 'POST' });
      state.config = r.config;
      state.projects = r.config.projects || [];
      state.ports = r.config.ports || [];
      el.idfVersion.textContent = r.config.idf_version
        ? `ESP-IDF v${r.config.idf_version}` : 'ESP-IDF（版本未知）';
      fillProjects();
      fillPorts();
      fillConfigForm();
      terminalAppend('✔ 已重新自动检测', 't-exit-ok');
    } catch (e) {
      terminalAppend(`[自动检测失败] ${e.message}`, 't-err');
    }
  });

  el.btnClear.addEventListener('click', () => {
    if (state.currentJob && !el.btnStop.disabled) return; // 运行中禁止清空
    terminalWelcome();
  });

  el.btnStop.addEventListener('click', stopJob);

  el.btnModalOk.addEventListener('click', () => {
    const cmd = pendingCommand;
    if (!cmd) return;
    const params = collectParams(cmd);
    closeModal();
    runCommand(cmd, params);
  });
  el.btnModalCancel.addEventListener('click', closeModal);
  el.btnModalClose.addEventListener('click', closeModal);
  el.modalMask.addEventListener('click', (e) => { if (e.target === el.modalMask) closeModal(); });

  // 命令按钮
  document.querySelectorAll('.cmd').forEach((btn) => {
    btn.addEventListener('click', () => {
      const command = btn.dataset.command;
      if (NO_PARAM.has(command)) {
        runCommand(command, {});
      } else {
        openModal(command);
      }
    });
  });
}

// ---------- 初始化 ----------
async function init() {
  bindEvents();
  await loadConfig();
  fillConfigForm();
}

init();
