"""watchdog 监听当前文件夹（不递归），md 文件变化时防抖后回调。"""
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

DEBOUNCE_SECONDS = 0.4


class _Handler(FileSystemEventHandler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self._timer = None
        self._lock = threading.Lock()

    def on_any_event(self, event):
        path = getattr(event, 'src_path', '') or getattr(event, 'dest_path', '')
        if not path.lower().endswith('.md') or '~$' in path:
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        self._timer = None
        try:
            self.callback()
        except Exception:
            pass


class MdWatcher:
    def __init__(self):
        self._observer = None

    def watch(self, folder, callback):
        self.stop()
        if not folder:
            return
        observer = Observer(timeout=0.2)
        observer.schedule(_Handler(callback), folder, recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def stop(self):
        if self._observer:
            self._observer.stop()
            self._observer = None
