"""后端 API：暴露给前端 JS 的方法都在 Api 类上（经本地 HTTP 服务调用）。"""
import codecs
import ctypes
import json
import os
import time

import webview

import mdparser

READ_CHUNK = 1 << 20


class Api:
    def __init__(self, config_path):
        self.config_path = config_path
        self.window = None            # main.py 注入（仅用于 create_file_dialog）
        self.on_folder_change = None  # 兼容保留，现在无人调用
        self._last_self_write = 0.0   # 自己写回触发的文件变更，不需要前端刷新
        self._hwnd = None             # shown 后缓存，供 ctypes 直调
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
            entries = self._list_md(folder)  # [(name, mtime)]
            files = [name for name, _ in entries]
            current = self.config.get('current')
            if current not in files:
                current = files[0] if files else None
            # 目录指纹：文件名+修改时间，前端轮询对比用（取代 watcher/evaluate_js 链路）
            stamp = hash(tuple((name, int(mtime * 10)) for name, mtime in entries))
            state = {
                'folder': folder,
                'files': files,
                'current': current,
                'onTop': bool(self.config.get('on_top', True)),
                'compact': bool(self.config.get('compact', False)),
                'dirStamp': stamp,
            }
            self.log('get_state ok: files=%s' % files)
            return state
        except Exception as e:
            self.log('get_state 异常: %r' % e)
            raise

    def _list_md(self, folder):
        """只扫描当前目录一层，不递归；按修改时间倒序。返回 [(name, mtime)]。"""
        if not folder or not os.path.isdir(folder):
            return []
        files = []
        for entry in os.scandir(folder):
            name = entry.name
            if not name.lower().endswith('.md'):
                continue
            if name.startswith('~$') or name.startswith('.'):
                continue
            try:
                if entry.is_file():
                    files.append((name, entry.stat().st_mtime))
            except OSError:
                continue
        files.sort(key=lambda x: -x[1])
        return files

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
        entries = self._list_md(path)
        self.config['current'] = entries[0][0] if entries else None
        self._save_config()
        if self.on_folder_change:
            self.on_folder_change()

    # ---------- 文件读写 ----------

    def _resolve(self, name):
        """限制在当前文件夹内，防止路径穿越。"""
        folder = self.config.get('folder')
        if not folder or not name:
            return None
        path = os.path.normpath(os.path.join(folder, name))
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
        lines[line] = '%s- [%s] %s' % (t.group('indent'), mark, t.group('text'))
        try:
            self._write(path, lines, had_bom, eol)
        except OSError as e:
            return {'ok': False, 'error': str(e)}
        return {'ok': True}

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
