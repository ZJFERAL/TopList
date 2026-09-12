"""TopList 入口：创建置顶窗口。前端与后端通过本地 HTTP JSON API 通信
（server.py 托管页面与 /api/*，不再使用 pywebview js_api 桥）。"""
import ctypes
import os
import sys

import webview

from backend import Api
from server import start_server


def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def main():
    api = Api(os.path.join(
        os.environ.get('APPDATA', os.path.expanduser('~')), 'TopList', 'config.json'))

    # 未捕获异常写日志，防止 pythonw 静默崩溃
    import traceback
    def _excepthook(t, v, tb):
        api.log('未捕获异常: %s' % ''.join(traceback.format_exception(t, v, tb)))
    sys.excepthook = _excepthook

    server, port = start_server(api)
    api.log('HTTP 服务启动: 127.0.0.1:%d' % port)

    win_cfg = api.config.get('window') or {}
    window = webview.create_window(
        'TopList',
        'http://127.0.0.1:%d/index.html' % port,
        width=win_cfg.get('width', 420),
        height=win_cfg.get('height', 640),
        x=win_cfg.get('x'),
        y=win_cfg.get('y'),
        on_top=bool(api.config.get('on_top', True)),
        min_size=(300, 380),
        background_color='#14161b',
        frameless=True,
        easy_drag=False,  # 默认 True 会注入全局 mousedown 拖动窗口，与边缘缩放热区冲突
    )
    api.window = window

    def ensure_win_style(*_):
        # on_shown 在 UI 线程触发。
        # 不要子类化 WndProc 拦 WM_NCCALCSIZE——会与 WebView2 初始化冲突导致启动卡死。
        try:
            hwnd = window.native.Handle.ToInt32()
            api.set_hwnd(hwnd)
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, -16)
            if not style & 0x00040000:
                user32.SetWindowLongW(hwnd, -16, style | 0x00040000)
                api.log('WS_THICKFRAME 已添加, style=0x%08X'
                        % user32.GetWindowLongW(hwnd, -16))
            # Win11: 边框颜色设为 NONE，DWM 不再画灰色边框
            val = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(val), 4)
            api.log('DWMWA_BORDER_COLOR 结果=%s' % res)
            # 顶部残留的灰线是 DWM 的 caption 区域绘制，把标题区颜色设为背景色
            bg = ctypes.c_uint(0x1b1614)  # COLORREF 0x00BBGGRR = #14161b
            res2 = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(bg), 4)
            api.log('DWMWA_CAPTION_COLOR 结果=%s' % res2)
            # NC 空隙用界面背景色填充
            from System.Drawing import ColorTranslator
            window.native.BackColor = ColorTranslator.FromHtml('#14161b')
            api.log('窗口样式初始化完成')
        except Exception as e:
            api.log('设置窗口样式失败: %r' % e)

    window.events.shown += ensure_win_style

    def on_closing():
        try:
            api.save_window(window.x, window.y, window.width, window.height)
        except Exception:
            pass

    window.events.closing += on_closing
    try:
        webview.start(debug=os.environ.get('TOPLIST_DEBUG') == '1')
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
