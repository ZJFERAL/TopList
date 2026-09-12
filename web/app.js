/* TopList 前端逻辑：与 Python 后端通过 pywebview js_api 桥接。 */
'use strict';

let state = null;      // { folder, files, current, onTop, compact }
let windowMaximized = false;
let current = null;    // 当前打开文件的解析结果
let editMode = false;  // 编辑模式：仅前端状态，不持久化

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
  const toggle = $('sidebarToggle');
  toggle.textContent = state.compact ? '›' : '‹';
  toggle.title = state.compact ? '展开侧栏' : '收起侧栏';
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

  $('sidebarToggle').addEventListener('click', async () => {
    state.compact = !state.compact;
    await api('set_compact', state.compact);
    applyWindowFlags();
  });

  $('editBtn').addEventListener('click', () => {
    editMode = !editMode;
    document.body.classList.toggle('edit-mode', editMode);
    $('editBtn').classList.toggle('active', editMode);
    // 撤销/重做历史整个运行期保留，进出编辑模式不清空
    if (!editMode && current) {
      // 退出编辑模式时丢弃未提交的编辑框（整体重渲染）
      rerenderTasks();
    }
  });

  $('undoBtn').addEventListener('click', undoOp);
  $('redoBtn').addEventListener('click', redoOp);

  // 编辑模式快捷键：Ctrl+Z 撤销 / Ctrl+Y 恢复。
  // 焦点在编辑输入框内时不拦截，保留浏览器原生的文字级撤销
  document.addEventListener('keydown', (e) => {
    if (!editMode || !e.ctrlKey || e.shiftKey) return;
    const key = e.key.toLowerCase();
    if (key === 'z') {
      if (e.target && e.target.tagName === 'INPUT') return;
      e.preventDefault();
      undoOp();
    } else if (key === 'y') {
      e.preventDefault();
      redoOp();
    }
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
  const base = state.folder || '选择文件夹…';
  const text = state.cwd ? base + ' \\ ' + state.cwd : base;
  el.textContent = text;
  el.title = text;
}

/* ---------- 文件列表（含文件夹导航） ---------- */

// 导航类 API 返回新的 state：更新指纹、按需打开该目录的当前文件
async function applyNavState(s) {
  state = s;
  applyWindowFlags();
  renderFolderName();
  lastDirStamp = state.dirStamp;
  if (state.current) {
    await openFile(state.current);
  } else {
    current = null;
    renderFileList();
    renderEmpty('此文件夹下没有 .md 文件');
  }
}

function renderFileList() {
  const nav = $('fileList');
  nav.innerHTML = '';

  // 返回上级（根文件夹时隐藏）
  if (state.cwd) {
    const up = document.createElement('div');
    up.className = 'file-item dir-item';
    up.textContent = '↩ ..';
    up.title = '返回上级文件夹';
    up.addEventListener('click', async () => {
      const s = await api('up_folder');
      if (s) await applyNavState(s);
    });
    nav.appendChild(up);
  }

  // 子文件夹
  for (const d of (state.dirs || [])) {
    const item = document.createElement('div');
    item.className = 'file-item dir-item';
    item.textContent = '📁 ' + d;
    item.title = d;
    item.addEventListener('click', async () => {
      const s = await api('open_folder', d);
      if (s) await applyNavState(s);
    });
    nav.appendChild(item);
  }

  for (const name of state.files) {
    const item = document.createElement('div');
    item.className = 'file-item' + (name === current?.name ? ' current' : '');
    item.title = name;
    item.dataset.name = name;

    const label = document.createElement('span');
    label.className = 'file-name';
    label.textContent = name.replace(/\.md$/i, '');

    const more = document.createElement('button');
    more.className = 'file-more';
    more.textContent = '⋮';
    more.title = '文件操作';
    more.addEventListener('mousedown', (e) => e.preventDefault());
    more.addEventListener('click', (e) => {
      e.stopPropagation();
      openFileMenu(more, name);
    });

    item.appendChild(label);
    item.appendChild(more);
    item.addEventListener('click', () => openFile(name));
    nav.appendChild(item);
  }

  // 末尾：新增 TODO（以 template.md 为模板）
  const add = document.createElement('div');
  add.className = 'file-add';
  add.textContent = '＋ 新增TODO';
  add.title = '新建 TODO 文件';
  add.addEventListener('click', startCreateTodo);
  nav.appendChild(add);
}

/* ---------- 文件管理 ---------- */

// 文件增删改后刷新列表；当前文件没了就按后端指示打开别的或显示空态
async function refreshAfterFileOp() {
  const s = await api('get_state');
  state = s;
  applyWindowFlags();
  renderFolderName();
  lastDirStamp = s.dirStamp;
  if (current && state.files.includes(current.name)) {
    renderFileList();
  } else if (state.current) {
    await openFile(state.current);
  } else {
    current = null;
    renderFileList();
    renderEmpty('此文件夹下没有 .md 文件');
  }
}

let menuOutsideHandler = null;

function closeFileMenu() {
  $('fileMenu').classList.remove('open');
  if (menuOutsideHandler) {
    document.removeEventListener('click', menuOutsideHandler, true);
    menuOutsideHandler = null;
  }
}

function openFileMenu(anchor, name) {
  const menu = $('fileMenu');
  menu.innerHTML = '';
  const mk = (text, cls, fn) => {
    const b = document.createElement('button');
    b.className = 'menu-item' + (cls ? ' ' + cls : '');
    b.textContent = text;
    b.addEventListener('click', (e) => { e.stopPropagation(); fn(b); });
    menu.appendChild(b);
    return b;
  };

  mk('重命名', '', () => { closeFileMenu(); startRename(name); });
  mk('打开文件夹', '', async () => {
    closeFileMenu();
    const r = await api('reveal_in_explorer', name);
    if (!r.ok) alert(r.error);
  });
  // 删除：标红 + 二次确认（第一次点变成"确认删除"并出现取消按钮，再点才执行）
  mk('删除', 'danger', (b) => {
    if (b.classList.contains('armed')) {
      closeFileMenu();
      api('delete_file', name).then((r) => {
        if (r.ok) refreshAfterFileOp();
        else alert(r.error);
      });
    } else {
      b.classList.add('armed');
      b.textContent = '确认删除？';
      const cancel = mk('取消', '', () => {
        b.classList.remove('armed');
        b.textContent = '删除';
        cancel.remove();
      });
    }
  });

  // 定位到 ⋮ 按钮下方，超出窗口底部时改到上方
  menu.classList.add('open');
  const rect = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let x = Math.min(rect.right - mw, window.innerWidth - mw - 4);
  let y = rect.bottom + 2;
  if (y + mh > window.innerHeight - 4) y = rect.top - mh - 2;
  menu.style.left = Math.max(4, x) + 'px';
  menu.style.top = y + 'px';
  // 点击菜单外部才关闭（捕获阶段判断 target，菜单内的点击交给按钮自身处理）
  menuOutsideHandler = (e) => {
    if (!menu.contains(e.target)) closeFileMenu();
  };
  // 下一个事件循环再监听，避免本次打开菜单的点击立即关闭
  setTimeout(() => document.addEventListener('click', menuOutsideHandler, true), 0);
}

function startRename(name) {
  const item = $('fileList')
    .querySelector('.file-item[data-name="' + CSS.escape(name) + '"]');
  if (!item) return;
  const label = item.querySelector('.file-name');
  const input = document.createElement('input');
  input.className = 'rename-input';
  input.value = name.replace(/\.md$/i, '');
  label.style.display = 'none';
  item.insertBefore(input, label);
  input.focus();
  input.select();

  let closed = false;
  const close = async (commit) => {
    if (closed) return;
    closed = true;
    const value = input.value.trim();
    input.remove();
    label.style.display = '';
    if (!commit || !value || value === name.replace(/\.md$/i, '')) return;
    const res = await api('rename_file', name, value);
    if (res.ok) {
      await refreshAfterFileOp();
    } else {
      alert(res.error);
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); close(true); }
    else if (e.key === 'Escape') { e.preventDefault(); close(false); }
  });
  input.addEventListener('blur', () => close(true));
  input.addEventListener('click', (e) => e.stopPropagation());
  input.addEventListener('mousedown', (e) => e.stopPropagation());
}

function startCreateTodo() {
  const add = $('fileList').querySelector('.file-add');
  if (!add || add.querySelector('input')) return;
  const input = document.createElement('input');
  input.className = 'rename-input';
  input.placeholder = '输入文件名';
  add.textContent = '';
  add.appendChild(input);
  add.classList.add('editing');
  input.focus();

  let closed = false;
  const close = async (commit) => {
    if (closed) return;
    closed = true;
    const value = input.value.trim();
    add.classList.remove('editing');
    renderFileList();  // 恢复 ＋ 按钮
    if (!commit || !value) return;
    const res = await api('create_todo', value);
    if (res.ok) {
      await refreshAfterFileOp();  // 后端把新文件设为 current，会自动打开
    } else {
      alert(res.error);
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); close(true); }
    else if (e.key === 'Escape') { e.preventDefault(); close(false); }
  });
  input.addEventListener('blur', () => close(true));
  input.addEventListener('click', (e) => e.stopPropagation());
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
        area.appendChild(taskRow(item));
        taskCount++;
      }
    }
    // 组末尾"＋ 新增任务"按钮：仅编辑模式显示（CSS 控制）。
    // 插入点 = 组内最后一行的行号；空组则用标题行行号
    const anchorLine = group.items.length
      ? group.items[group.items.length - 1].line
      : group.line;
    if (anchorLine != null) {
      const add = document.createElement('div');
      add.className = 'group-add';
      add.textContent = '＋';
      add.title = '新增任务';
      add.addEventListener('click', () => addTaskAt(anchorLine));
      area.appendChild(add);
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
  row.dataset.line = item.line;

  const box = document.createElement('div');
  box.className = 'box';
  const label = document.createElement('div');
  label.className = 'label';
  label.textContent = item.text;
  row.title = item.text;

  row.appendChild(box);
  row.appendChild(label);

  // 行尾删除按钮：仅编辑模式显示（CSS 控制）
  const del = document.createElement('button');
  del.className = 'del';
  del.textContent = '✕';
  del.title = '删除该行';
  // 阻止按钮抢焦点：编辑框失焦保存与本行删除请求会竞争，导致行号错位
  del.addEventListener('mousedown', (e) => e.preventDefault());
  del.addEventListener('click', async (e) => {
    e.stopPropagation();
    const res = await api('delete_task', current.name, item.line);
    if (res.ok) {
      await refreshCurrent();
    } else {
      console.error(res.error);
      alert(res.error);
    }
  });
  row.appendChild(del);

  row.addEventListener('click', (e) => {
    if (!editMode) {
      toggle(row, item);
      return;
    }
    // 编辑模式：复选框禁用，点文字进入原地编辑
    if (e.target.closest('.box') || e.target.closest('.del')) return;
    startEdit(row, item);
  });
  return row;
}

/* ---------- 编辑模式 ---------- */

// 重渲染任务区并保持滚动位置（无 API 调用，用于编辑模式切换）
function rerenderTasks() {
  const area = $('taskArea');
  const top = area.scrollTop, left = area.scrollLeft;
  renderTasks(current.groups);
  area.scrollTop = top;
  area.scrollLeft = left;
}

// 自己写回文件后同步目录指纹，避免 2s 轮询把本次写回误判为外部修改而重渲染
async function syncStamp() {
  try {
    const s = await api('get_state');
    lastDirStamp = s.dirStamp;
  } catch (e) { /* 下一轮轮询会再同步 */ }
}

// 写回文件后重渲染，保持滚动位置
async function refreshCurrent() {
  const area = $('taskArea');
  const top = area.scrollTop, left = area.scrollLeft;
  await openFile(current.name);
  area.scrollTop = top;
  area.scrollLeft = left;
  syncStamp();
}

// 在组末尾新增任务行，并自动进入该行的编辑框
async function addTaskAt(anchorLine) {
  const res = await api('add_task', current.name, anchorLine);
  if (!res.ok) {
    console.error(res.error);
    alert(res.error);
    return;
  }
  await refreshCurrent();
  const row = document.querySelector('#tasks .task[data-line="' + res.line + '"]');
  if (row) startEdit(row, { line: res.line, text: '' });
}

async function undoOp() {
  if (!current) return;
  const res = await api('undo', current.name);
  if (res.ok) await refreshCurrent();
}

async function redoOp() {
  if (!current) return;
  const res = await api('redo', current.name);
  if (res.ok) await refreshCurrent();
}

function startEdit(row, item) {
  const label = row.querySelector('.label');
  if (row.querySelector('.edit-input')) return;
  const input = document.createElement('input');
  input.className = 'edit-input';
  input.value = item.text;
  input.style.width = Math.max(label.offsetWidth + 24, 160) + 'px';
  label.style.display = 'none';
  row.insertBefore(input, label);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);

  let closed = false;
  const close = (commit) => {
    if (closed) return;
    closed = true;
    const value = input.value;
    input.remove();
    label.style.display = '';
    if (commit && value.trim() !== item.text) {
      const newText = value.trim();
      api('edit_task', current.name, item.line, newText)
        .then((res) => {
          if (res.ok) {
            // 只原地更新该行，不重建整个列表（避免卡顿和滚动跳动）
            item.text = newText;
            label.textContent = newText;
            row.title = newText;
            syncStamp();
          } else {
            console.error(res.error);
            alert(res.error);
          }
        });
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); close(true); }
    else if (e.key === 'Escape') { e.preventDefault(); close(false); }
  });
  // 退出编辑框（点击别处/失焦）自动保存；Esc 才是不保存取消
  input.addEventListener('blur', () => close(true));
  input.addEventListener('click', (e) => e.stopPropagation());
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
  } else {
    syncStamp();
  }
}

boot().catch((e) => {
  const div = document.createElement('div');
  div.className = 'empty';
  div.style.color = '#ff7b7b';
  div.textContent = '启动出错: ' + (e && e.message ? e.message : e);
  $('tasks').appendChild(div);
});
