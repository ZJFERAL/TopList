"""后端 API：暴露给前端 JS 的方法都在 Api 类上（经本地 HTTP 服务调用）。"""
import codecs
import ctypes
import json
import os
import subprocess
import sys
import time

import webview

import mdparser

READ_CHUNK = 1 << 20
UNDO_LIMIT = 20  # 撤销/重做的步数上限（快照为文件全文，内存开销可忽略）


class Api:
    def __init__(self, config_path):
        self.config_path = config_path
        self.window = None            # main.py 注入（仅用于 create_file_dialog）
        self.on_folder_change = None  # 兼容保留，现在无人调用
        self._last_self_write = 0.0   # 自己写回触发的文件变更，不需要前端刷新
        self._hwnd = None             # shown 后缓存，供 ctypes 直调
        self._undo_stacks = {}        # 文件名 -> [操作前全文快照]，整个运行期保留
        self._redo_stacks = {}        # 文件名 -> [被撤销的全文快照]
        self._last_logged_files = None  # get_state 日志去重：只在文件列表变化时记录
        self.config = self._load_config()

    def set_hwnd(self, hwnd):
        self._hwnd = hwnd

    # ---------- 配置 ----------

    def _load_config(self):
        defaults = {
            'folder': None,
            'current': None,
            'on_top': True,
            'compact': False,
            'window': {},
        }
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                defaults.update(data)
        except (OSError, json.JSONDecodeError):
            pass
        return defaults

    def _save_config(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    # ---------- 状态 ----------

    def get_state(self):
        try:
            folder = self.config.get('folder')
            browse = self._browse_dir()
            entries, dirs = self._list_dir(browse) if browse else ([], [])
            files = [name for name, _ in entries]
            cwd = self.config.get('cwd') or ''
            current = self.config.get('current')
            if current not in files:
                current = files[0] if files else None
            # 目录指纹：cwd + 子文件夹 + 文件名+修改时间，前端轮询对比用
            stamp = hash((cwd, tuple(dirs),
                          tuple((name, int(mtime * 10)) for name, mtime in entries)))
            state = {
                'folder': folder,
                'cwd': cwd,
                'dirs': dirs,
                'files': files,
                'current': current,
                'onTop': bool(self.config.get('on_top', True)),
                'compact': bool(self.config.get('compact', False)),
                'dirStamp': stamp,
            }
            if files != self._last_logged_files:
                self._last_logged_files = files
                self.log('get_state ok: cwd=%s files=%s dirs=%s'
                         % (cwd or '.', files, dirs))
            return state
        except Exception as e:
            self.log('get_state 异常: %r' % e)
            raise

    def _browse_dir(self):
        """当前浏览目录 = 根文件夹 + cwd（根内相对路径），防越界。"""
        folder = self.config.get('folder')
        if not folder or not os.path.isdir(folder):
            return None
        cwd = os.path.normpath(self.config.get('cwd') or '')
        path = folder if cwd in ('.', '') else os.path.normpath(
            os.path.join(folder, cwd))
        if path != folder and not path.startswith(folder + os.sep):
            path = folder
        return path if os.path.isdir(path) else None

    def _list_dir(self, folder):
        """只扫描目录一层，不递归；返回 ([(name, mtime)] 按修改时间倒序, [子文件夹名] 按名称排序)。
        跳过隐藏项（. 开头、~$ 临时文件）。"""
        files, dirs = [], []
        for entry in os.scandir(folder):
            name = entry.name
            if name.startswith('~$') or name.startswith('.'):
                continue
            try:
                if entry.is_dir():
                    dirs.append(name)
                elif entry.is_file() and name.lower().endswith('.md'):
                    files.append((name, entry.stat().st_mtime))
            except OSError:
                continue
        files.sort(key=lambda x: -x[1])
        dirs.sort()
        return files, dirs

    # ---------- 文件夹 ----------

    def choose_folder(self):
        self.log('choose_folder 调用')
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        self.log('choose_folder 结果: %r' % (result,))
        if result:
            self.set_folder(result[0])
            return self.get_state()
        return None

    def set_folder(self, path):
        path = os.path.normpath(str(path))
        if not os.path.isdir(path):
            return
        self.config['folder'] = path
        self.config['cwd'] = ''      # 换根文件夹时回到顶层
        entries, _dirs = self._list_dir(path)
        self.config['current'] = entries[0][0] if entries else None
        self._save_config()
        if self.on_folder_change:
            self.on_folder_change()

    # ---------- 文件夹导航 ----------

    def open_folder(self, name):
        """进入当前浏览目录下的子文件夹，返回新的 get_state()。"""
        folder = self._browse_dir()
        if not folder:
            return None
        name = str(name)
        # 只允许单级普通目录名：拒绝路径分隔符、.. 和隐藏项
        if (not name or name.startswith('.') or name.startswith('~$')
                or '..' in name or os.path.sep in name or (os.path.altsep and os.path.altsep in name)):
            return None
        target = os.path.normpath(os.path.join(folder, name))
        root = os.path.normpath(self.config['folder'])
        if not target.startswith(root + os.sep) or not os.path.isdir(target):
            return None
        self.config['cwd'] = os.path.relpath(target, root)
        self.config['current'] = None
        self._save_config()
        return self.get_state()

    def up_folder(self):
        """返回上一级（最多回到所选根文件夹），返回新的 get_state()。"""
        cwd = os.path.normpath(self.config.get('cwd') or '')
        if cwd in ('.', ''):
            return self.get_state()
        parent = os.path.dirname(cwd)
        self.config['cwd'] = '' if parent in ('.', '') else parent
        self.config['current'] = None
        self._save_config()
        return self.get_state()

    # ---------- 文件管理 ----------

    _INVALID_NAME_CHARS = '\\/:*?"<>|'

    def _safe_md_name(self, name):
        """校验文件名并补 .md 后缀；非法返回 None。"""
        name = str(name).strip()
        if name.lower().endswith('.md'):
            name = name[:-3]
        if not name or any(c in self._INVALID_NAME_CHARS for c in name):
            return None
        if name != name.strip('.') or name.startswith('~$'):
            return None
        return name + '.md'

    def rename_file(self, old_name, new_name):
        """重命名当前浏览目录下的 md 文件。"""
        new_name = self._safe_md_name(new_name)
        if not new_name:
            return {'ok': False, 'error': '文件名不合法'}
        path = self._resolve(old_name)
        if not path:
            return {'ok': False, 'error': '文件不存在'}
        new_path = os.path.join(os.path.dirname(path), new_name)
        if os.path.exists(new_path):
            return {'ok': False, 'error': '同名文件已存在'}
        try:
            os.rename(path, new_path)
        except OSError as e:
            return {'ok': False, 'error': str(e)}
        if self.config.get('current') == old_name:
            self.config['current'] = new_name
            self._save_config()
        self.log('rename_file: %s -> %s' % (old_name, new_name))
        return {'ok': True}

    def _to_recycle_bin(self, path):
        """移入回收站（SHFileOperationW + FOF_ALLOWUNDO）。返回错误信息，成功返回 None。"""
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ('hwnd', wintypes.HWND),
                ('wFunc', ctypes.c_uint),
                ('pFrom', ctypes.c_wchar_p),
                ('pTo', ctypes.c_wchar_p),
                ('fFlags', ctypes.c_ushort),
                ('fAnyOperationsAborted', wintypes.BOOL),
                ('hNameMappings', ctypes.c_void_p),
                ('lpszProgressTitle', ctypes.c_wchar_p),
            ]

        op = SHFILEOPSTRUCTW()
        op.hwnd = self._hwnd or None
        op.wFunc = 3  # FO_DELETE
        # pFrom 要求双 null 结尾：字符串里自带一个，c_wchar_p 再补一个
        op.pFrom = ctypes.c_wchar_p(os.path.abspath(path) + '\0')
        op.fFlags = 0x40 | 0x10  # FOF_ALLOWUNDO | FOF_NOCONFIRMATION
        if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0:
            return '移入回收站失败'
        if op.fAnyOperationsAborted:
            return '操作已取消'
        return None

    def delete_file(self, name):
        """删除当前浏览目录下的 md 文件（移入回收站，可从回收站还原）。"""
        path = self._resolve(name)
        if not path:
            return {'ok': False, 'error': '文件不存在'}
        err = self._to_recycle_bin(path)
        if err:
            return {'ok': False, 'error': err}
        if self.config.get('current') == name:
            self.config['current'] = None
            self._save_config()
        self.log('delete_file(回收站): %s' % name)
        return {'ok': True}

    def reveal_in_explorer(self, name):
        """在资源管理器中定位该文件。"""
        path = self._resolve(name)
        if not path:
            return {'ok': False, 'error': '文件不存在'}
        subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
        return {'ok': True}

    def create_todo(self, name):
        """以 template.md（软件目录下）为模板新建 TODO 文件；模板缺失时用内置默认。"""
        new_name = self._safe_md_name(name)
        if not new_name:
            return {'ok': False, 'error': '文件名不合法'}
        folder = self._browse_dir()
        if not folder:
            return {'ok': False, 'error': '未选择文件夹'}
        path = os.path.join(folder, new_name)
        if os.path.exists(path):
            return {'ok': False, 'error': '同名文件已存在'}
        base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        tpl_path = os.path.join(base, 'template.md')
        try:
            if os.path.isfile(tpl_path):
                with open(tpl_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            else:
                content = '---\ntags:\n  - TODO\n时间: {date}\n---\n---\n\n- [ ] \n'
            content = content.replace('{date}', time.strftime('%Y-%m-%d'))
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        except OSError as e:
            return {'ok': False, 'error': str(e)}
        self.config['current'] = new_name
        self._save_config()
        self.log('create_todo: %s' % new_name)
        return {'ok': True}

    # ---------- 文件读写 ----------

    def _resolve(self, name):
        """限制在当前浏览目录内，防止路径穿越。"""
        folder = self._browse_dir()
        if not folder or not name:
            return None
        path = os.path.normpath(os.path.join(folder, str(name)))
        if os.path.dirname(path) != folder or not os.path.isfile(path):
            return None
        return path

    def _read(self, path):
        with open(path, 'rb') as f:
            raw = f.read(READ_CHUNK)
        had_bom = raw.startswith(codecs.BOM_UTF8)
        if had_bom:
            raw = raw[len(codecs.BOM_UTF8):]
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('gbk', errors='replace')
        eol = '\r\n' if '\r\n' in text else '\n'
        return text.replace('\r\n', '\n').replace('\r', '\n'), had_bom, eol

    def _write(self, path, lines, had_bom, eol):
        data = eol.join(lines)
        encoding = 'utf-8-sig' if had_bom else 'utf-8'
        with open(path, 'w', encoding=encoding, newline='') as f:
            f.write(data)
        self._last_self_write = time.time()

    def open_file(self, name):
        path = self._resolve(name)
        if not path:
            return {'error': '文件不存在或不可读'}
        text, had_bom, eol = self._read(path)
        data = mdparser.parse_markdown(text)
        data['name'] = name
        data['eol'] = eol
        data['hadBom'] = had_bom
        if self.config.get('current') != name:
            self.config['current'] = name
            self._save_config()
        return data

    def toggle_task(self, name, line):
        path = self._resolve(name)
        if not path:
            return {'ok': False, 'error': '文件不存在'}
        text, had_bom, eol = self._read(path)
        lines = text.split('\n')
        if not (0 <= line < len(lines)):
            return {'ok': False, 'error': '行号越界（文件可能已被修改）'}
        t = mdparser.TASK_RE.match(lines[line])
        if not t:
            return {'ok': False, 'error': '该行不是任务'}
        mark = 'x' if t.group('mark') == ' ' else ' '
        lines[line] = '%s- [%s] %s' % (t.group('indent'), mark, t.group('text') or '')
        try:
            self._write(path, lines, had_bom, eol)
        except OSError as e:
            return {'ok': False, 'error': str(e)}
        return {'ok': True}

    def _task_lines(self, name):
        """读文件并返回 (path, lines, had_bom, eol, text)，供行级编辑类 API 共用。"""
        path = self._resolve(name)
        if not path:
            return None, None, None, None, None
        text, had_bom, eol = self._read(path)
        return path, text.split('\n'), had_bom, eol, text

    def _push_undo(self, name, text_before):
        """记录一次操作前的全文快照（按文件独立，最多 UNDO_LIMIT 步），并清空重做栈。"""
        stack = self._undo_stacks.setdefault(name, [])
        stack.append(text_before)
        if len(stack) > UNDO_LIMIT:
            del stack[0]
        self._redo_stacks.pop(name, None)

    def _restore_snapshot(self, name, src, dst, empty_msg):
        path, _lines, had_bom, eol, text = self._task_lines(name)
        if not path:
            return {'ok': False, 'error': '文件不存在'}
        stack = src.get(name) or []
        if not stack:
            return {'ok': False, 'error': empty_msg}
        snapshot = stack.pop()
        try:
            self._write(path, snapshot.split('\n'), had_bom, eol)
        except OSError as e:
            stack.append(snapshot)
            return {'ok': False, 'error': str(e)}
        dst.setdefault(name, []).append(text)
        return {'ok': True}

    def undo(self, name):
        """撤销编辑模式下最近一步增/删/改。"""
        return self._restore_snapshot(name, self._undo_stacks,
                                      self._redo_stacks, '没有可撤销的操作')

    def redo(self, name):
        """恢复最近一次被撤销的操作。"""
        return self._restore_snapshot(name, self._redo_stacks,
                                      self._undo_stacks, '没有可恢复的操作')

    def edit_task(self, name, line, text):
        """编辑任务行文字：只改 "- [ ] " 之后的部分，缩进与勾选状态保持不变。"""
        path, lines, had_bom, eol, before = self._task_lines(name)
        if not path:
            return {'ok': False, 'error': '文件不存在'}
        if not (0 <= line < len(lines)):
            return {'ok': False, 'error': '行号越界（文件可能已被修改）'}
        t = mdparser.TASK_RE.match(lines[line])
        if not t:
            return {'ok': False, 'error': '该行不是任务'}
        lines[line] = '%s- [%s] %s' % (t.group('indent'), t.group('mark'),
                                       str(text).strip())
        try:
            self._write(path, lines, had_bom, eol)
        except OSError as e:
            return {'ok': False, 'error': str(e)}
        self._push_undo(name, before)
        return {'ok': True}

    def delete_task(self, name, line):
        """删除一行任务行（仅限 "- [ ]" 样式行）。"""
        path, lines, had_bom, eol, before = self._task_lines(name)
        if not path:
            return {'ok': False, 'error': '文件不存在'}
        if not (0 <= line < len(lines)):
            return {'ok': False, 'error': '行号越界（文件可能已被修改）'}
        if not mdparser.TASK_RE.match(lines[line]):
            return {'ok': False, 'error': '该行不是任务'}
        del lines[line]
        try:
            self._write(path, lines, had_bom, eol)
        except OSError as e:
            return {'ok': False, 'error': str(e)}
        self._push_undo(name, before)
        return {'ok': True}

    def add_task(self, name, after_line):
        """在 after_line 之后插入一行未勾选任务 "- [ ] "。"""
        path, lines, had_bom, eol, before = self._task_lines(name)
        if not path:
            return {'ok': False, 'error': '文件不存在'}
        if not (0 <= after_line < len(lines)):
            return {'ok': False, 'error': '插入位置越界（文件可能已被修改）'}
        lines.insert(after_line + 1, '- [ ] ')
        try:
            self._write(path, lines, had_bom, eol)
        except OSError as e:
            return {'ok': False, 'error': str(e)}
        self._push_undo(name, before)
        return {'ok': True, 'line': after_line + 1}

    def _ui_exec(self, fn):
        """把 fn 投递到 UI 线程执行。所有窗口操作必须走这里：
        Win32 模态循环（移动/缩放）从工作线程触发会导致 UI 崩溃（已实测）。"""
        from System import Func, Type
        form = self.window.native
        form.BeginInvoke(Func[Type](fn))

    def recently_self_written(self):
        return time.time() - self._last_self_write < 1.0

    def log(self, msg):
        """轻量日志：写入配置目录 log.txt，便于排查偶发问题。"""
        try:
            with open(os.path.join(os.path.dirname(self.config_path), 'log.txt'),
                      'a', encoding='utf-8') as f:
                f.write(time.strftime('[%Y-%m-%d %H:%M:%S] ') + str(msg) + '\n')
        except OSError:
            pass

    # ---------- 窗口设置 ----------

    def set_on_top(self, flag):
        self.log('set_on_top: %s' % flag)
        self.config['on_top'] = bool(flag)
        self._save_config()
        if self.window and self.window.native:
            def _do():
                try:
                    self.window.native.TopMost = bool(flag)  # 与 pywebview 内部一致
                    self.log('TopMost 已设为 %s' % flag)
                except Exception as e:
                    self.log('TopMost 失败: %r' % e)
            self._ui_exec(_do)
        return bool(flag)

    def set_compact(self, flag):
        self.log('set_compact: %s' % flag)
        self.config['compact'] = bool(flag)
        self._save_config()
        return bool(flag)

    def save_window(self, x, y, width, height):
        self.config['window'] = {'x': x, 'y': y, 'width': width, 'height': height}
        self._save_config()
    def minimize_window(self):
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, 6)  # SW_MINIMIZE
        return True

    def maximize_window(self):
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, 3)  # SW_MAXIMIZE
        return True

    def restore_window(self):
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, 9)  # SW_RESTORE
        return True

    def close_window(self):
        if self._hwnd:
            ctypes.windll.user32.SendMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return True

    def begin_move(self):
        """无边框窗口顶栏拖拽移动：转发 HTCAPTION 让系统接管。"""
        import ctypes
        import ctypes.wintypes as wintypes

        WM_NCLBUTTONDOWN = 0xA1
        HTCAPTION = 2
        if not self._hwnd:
            return False
        api_ref = {'api': self, '_busy': False, '_hwnd': self._hwnd}

        def _do():
            try:
                if api_ref['_busy']:
                    return
                api_ref['_busy'] = True
                try:
                    hwnd = api_ref['_hwnd']
                    user32 = ctypes.windll.user32
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    lparam = (pt.y << 16) | (pt.x & 0xFFFF)
                    user32.ReleaseCapture()
                    # HTCAPTION: 进入系统移动模态循环，直到松开鼠标
                    user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, lparam)
                finally:
                    api_ref['_busy'] = False
            except Exception as e:
                api_ref['api'].log('begin_move 失败: %r' % e)
                api_ref['_busy'] = False

        self._ui_exec(_do)
        return True

    def begin_resize(self, edge):
        """无边框窗口边缘拖拽调整大小：转发 WM_NCLBUTTONDOWN 让系统接管。"""
        import ctypes
        import ctypes.wintypes as wintypes

        WM_NCLBUTTONDOWN = 0xA1
        hit = {'top': 12, 'top-right': 14, 'right': 11, 'bottom-right': 17,
               'bottom': 15, 'bottom-left': 16, 'left': 10, 'top-left': 13}.get(edge)
        if not hit or not self._hwnd:
            return False
        api_ref = {'api': self, '_busy': False, '_hwnd': self._hwnd}

        def _do():
            try:
                if api_ref['_busy']:
                    return
                api_ref['_busy'] = True
                try:
                    hwnd = api_ref['_hwnd']
                    user32 = ctypes.windll.user32
                    # 幂等补样式：WinForms 可能在 shown 后重建句柄导致丢失
                    style = user32.GetWindowLongW(hwnd, -16)
                    if not style & 0x00040000:
                        user32.SetWindowLongW(hwnd, -16, style | 0x00040000)
                    # lParam 必须是当前光标坐标 (y<<16 | x)，否则系统判断错缩放方向
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    lparam = (pt.y << 16) | (pt.x & 0xFFFF)
                    user32.ReleaseCapture()
                    user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, hit, lparam)
                finally:
                    api_ref['_busy'] = False
            except Exception as e:
                api_ref['api'].log('begin_resize 失败: %r' % e)
                api_ref['_busy'] = False

        # BeginInvoke 投递到 UI 线程且不等待：SendMessage 会阻塞到松开鼠标，
        # 若用同步 Invoke 会死锁
        self._ui_exec(_do)
        return True
