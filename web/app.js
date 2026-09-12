/* TopList 前端逻辑：与 Python 后端通过 pywebview js_api 桥接。 */
'use strict';

let state = null;      // { folder, files, current, onTop, compact }
let windowMaximized = false;
let current = null;    // 当前打开文件的解析结果

const $ = (id) => document.getElementById(id);

/* HTTP JSON API：替代 pywebview js_api 桥 */
async function api(method, ...args) {
  const res = await fetch('/api/' + method, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ args }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || ('API ' + method + ' 失败: ' + res.status));
  }
  return res.json();
}

/* ---------- 启动 ---------- */

function boot() {
  bindToolbar();
  refreshState().then(() => {
    if (state.current) {
      openFile(state.current);
    } else {
      renderEmpty(state.folder ? '此文件夹下没有 .md 文件' : '点击左上角 📁 选择一个文件夹');
    }
  });
  // 轮询目录指纹检测外部修改，替代 watcher/evaluate_js 链路（消除跨线程死锁源）
  setInterval(pollFsChange, 2000);
  window.__onFsChange = onFsChange;  // 兼容保留
}

let lastDirStamp = null;
let polling = false;

async function pollFsChange() {
  if (polling) return;
  polling = true;
  try {
    const s = await api('get_state');
    if (lastDirStamp === null) {
      lastDirStamp = s.dirStamp;
      return;
    }
    if (s.dirStamp !== lastDirStamp) {
      lastDirStamp = s.dirStamp;
      state = s;
      renderFileList();
      if (current && state.files.includes(current.name)) {
        await openFile(current.name);
      } else if (state.current) {
        await openFile(state.current);
      } else {
        renderEmpty('此文件夹下没有 .md 文件');
      }
    }
  } catch (e) { /* 后端忙时静默跳过本轮 */ }
  finally { polling = false; }
}

async function onFsChange() {
  await refreshState();
  if (current && state.files.includes(current.name)) {
    const scroll = $('taskArea').scrollTop;
    await openFile(current.name);
    $('taskArea').scrollTop = scroll;
  } else if (state.current) {
    await openFile(state.current);
  } else {
    renderEmpty('此文件夹下没有 .md 文件');
  }
}

async function refreshState() {
  state = await api('get_state');
  applyWindowFlags();
  renderFileList();
  renderFolderName();
}

function applyWindowFlags() {
  document.body.classList.toggle('compact', !!state.compact);
  $('pinBtn').classList.toggle('active', !!state.onTop);
  $('compactBtn').classList.toggle('active', !!state.compact);
}

/* ---------- 顶栏 ---------- */

function bindToolbar() {
  $('folderBtn').addEventListener('click', async () => {
    const s = await api('choose_folder');
    if (!s) return;
    state = s;
    applyWindowFlags();
    renderFolderName();
    if (state.current) {
      openFile(state.current);
    } else {
      renderFileList();
      renderEmpty('此文件夹下没有 .md 文件');
    }
  });

  $('pinBtn').addEventListener('click', async () => {
    state.onTop = await api('set_on_top', !state.onTop);
    applyWindowFlags();
  });

  $('compactBtn').addEventListener('click', async () => {
    state.compact = !state.compact;
    await api('set_compact', state.compact);
    applyWindowFlags();
  });

  $('minBtn').addEventListener('click', async () => {
    await api('minimize_window');
  });

  $('maxBtn').addEventListener('click', async () => {
    if (windowMaximized) {
      await api('restore_window');
      $('maxBtn').textContent = '□';
    } else {
      await api('maximize_window');
      $('maxBtn').textContent = '❐';
    }
    windowMaximized = !windowMaximized;
    document.body.classList.toggle('maximized', windowMaximized);
  });

  $('closeBtn').addEventListener('click', async () => {
    await api('close_window');
  });

  // 无边框窗口：顶栏拖拽移动（WebView2 不支持 CSS app-region，走系统 HTCAPTION）
  $('titlebar').addEventListener('mousedown', (e) => {
    if (e.button !== 0 || e.target.closest('button')) return;
    api('begin_move');
  });

  // 无边框窗口：边缘热区拖拽调整大小
  document.querySelectorAll('.rz').forEach((el) => {
    el.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      api('begin_resize', el.dataset.edge);
    });
  });
}

function renderFolderName() {
  const el = $('folderName');
  el.textContent = state.folder || '选择文件夹…';
  el.title = state.folder || '';
}

/* ---------- 文件列表 ---------- */

function renderFileList() {
  const nav = $('fileList');
  nav.innerHTML = '';
  for (const name of state.files) {
    const item = document.createElement('div');
    item.className = 'file-item' + (name === current?.name ? ' current' : '');
    item.textContent = name.replace(/\.md$/i, '');
    item.title = name;
    item.addEventListener('click', () => openFile(name));
    nav.appendChild(item);
  }
}

/* ---------- 任务区 ---------- */

function renderEmpty(text) {
  current = null;
  $('meta').innerHTML = '';
  $('tasks').innerHTML = '';
  const div = document.createElement('div');
  div.className = 'empty';
  div.textContent = text;
  $('tasks').appendChild(div);
}

async function openFile(name) {
  const data = await api('open_file', name);
  if (data.error) { renderEmpty(data.error); return; }
  current = data;
  renderFileList();
  renderMeta(data.frontmatter);
  renderTasks(data.groups);
}

function renderMeta(fm) {
  const meta = $('meta');
  meta.innerHTML = '';
  for (const [key, value] of Object.entries(fm || {})) {
    if (key === '_raw') { addChip(meta, value, false); continue; }
    if (key === 'tags' || key === 'Tags' || key === '标签') {
      (Array.isArray(value) ? value : [value]).forEach(
        (t) => t != null && addChip(meta, '#' + t, true));
      continue;
    }
    if (value !== null && typeof value !== 'object') {
      addChip(meta, `${key}: ${value}`, false);
    }
  }
}

function addChip(container, text, isTag) {
  const chip = document.createElement('span');
  chip.className = 'chip' + (isTag ? ' tag' : '');
  chip.textContent = text;
  container.appendChild(chip);
}

function renderTasks(groups) {
  const area = $('tasks');
  area.innerHTML = '';
  let taskCount = 0;

  for (const group of groups) {
    if (group.title) {
      const h = document.createElement('div');
      h.className = 'group-title';
      h.textContent = group.title;
      area.appendChild(h);
    }
    for (const item of group.items) {
      if (item.type === 'prose') {
        const p = document.createElement('div');
        p.className = 'prose';
        p.textContent = item.text;
        area.appendChild(p);
      } else {
        taskCount++;
        area.appendChild(taskRow(item));
      }
    }
  }

  if (!taskCount && !area.querySelector('.prose')) {
    renderEmpty('没有任务，在 md 文件里写 "- [ ] 事情" 即可');
  }
}

function taskRow(item) {
  const row = document.createElement('div');
  row.className = 'task' + (item.checked ? ' checked' : '');
  row.style.paddingLeft = (8 + item.indent * 18) + 'px';

  const box = document.createElement('div');
  box.className = 'box';
  const label = document.createElement('div');
  label.className = 'label';
  label.textContent = item.text;

  row.appendChild(box);
  row.appendChild(label);

  row.addEventListener('click', () => toggle(row, item));
  return row;
}

async function toggle(row, item) {
  // item.checked 是 DOM 渲染时的快照，再次点击时已不准；改用 row 当前 class 判断
  const isNowChecked = !row.classList.contains('checked');
  row.classList.toggle('checked', isNowChecked);
  // 同步 item 对象，以便后续切换正确
  item.checked = isNowChecked;
  const res = await api('toggle_task', current.name, item.line);
  if (!res.ok) {
    row.classList.toggle('checked', !isNowChecked);
    item.checked = !isNowChecked;
    console.error(res.error);
  }
}

boot().catch((e) => {
  const div = document.createElement('div');
  div.className = 'empty';
  div.style.color = '#ff7b7b';
  div.textContent = '启动出错: ' + (e && e.message ? e.message : e);
  $('tasks').appendChild(div);
});
