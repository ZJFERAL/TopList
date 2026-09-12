"""本地 HTTP 服务：托管前端页面 + JSON API（替代 pywebview js_api 桥，
彻底摆脱 pywebviewready 事件与 evaluate_js 回传的时序/死锁问题）。"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.ico': 'image/x-icon',
}


class ApiHandler(BaseHTTPRequestHandler):
    api = None  # 启动时注入 Api 实例

    def log_message(self, *args):  # 静默访问日志
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            path = '/index.html'
        safe = os.path.normpath(path.lstrip('/'))
        full = os.path.join(WEB_DIR, safe)
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self._send(404, 'not found', 'text/plain')
            return
        with open(full, 'rb') as f:
            self._send(200, f.read(), MIME.get(os.path.splitext(full)[1].lower(),
                                               'application/octet-stream'))

    def do_POST(self):
        name = self.path.split('?')[0].lstrip('/')
        if name.startswith('api/'):
            name = name[4:]
        method = getattr(self.api, name, None)
        if not callable(method) or name.startswith('_'):
            self._send(404, json.dumps({'error': 'no such api: %s' % name}))
            return
        length = int(self.headers.get('Content-Length') or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b'{}')
            result = method(*payload.get('args', []))
            self._send(200, json.dumps(result, ensure_ascii=False, default=str))
        except Exception as e:
            self.api.log('api %s 异常: %r' % (name, e))
            self._send(500, json.dumps({'error': str(e)}))


def start_server(api):
    """启动 HTTP 服务，返回 (server, port)。"""
    ApiHandler.api = api
    server = ThreadingHTTPServer(('127.0.0.1', 0), ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]
