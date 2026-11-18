"""Serve the deployment wizard on loopback."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv/bin/python'
ASSETS = ROOT / 'tools/deployment-ui'
TOKEN = secrets.token_urlsafe(32)


def setup():
    python = shutil.which('python3.12')
    if not python:
        return {'ok': False, 'error': '请安装 Python 3.12，再点重新检测。'}
    if not PYTHON.is_file():
        subprocess.run([python, '-m', 'venv', str(ROOT / '.venv')], check=True, timeout=120)
    subprocess.run([str(PYTHON), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')],
                   check=True, timeout=600)
    return run_step('mac', {})


def run_step(action, data):
    result = subprocess.run([str(PYTHON), str(ROOT / 'tools/deploy.py'), action],
                            input=json.dumps(data), stdout=subprocess.PIPE,
                            text=True, check=True, timeout=240)
    return json.loads(result.stdout)


class Handler(BaseHTTPRequestHandler):
    def respond(self, data, mime, status=200):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def do_GET(self):
        assets = {'/': ('index.html', 'text/html'), '/style.css': ('style.css', 'text/css'),
                  '/app.js': ('app.js', 'text/javascript'), '/logo.png': ('logo.png', 'image/png')}
        if not self.valid_host() or self.path not in assets:
            self.send_error(404)
            return
        name, mime = assets[self.path]
        content = (ASSETS / name).read_bytes()
        self.respond(content.replace(b'__SESSION_TOKEN__', TOKEN.encode()) if name == 'index.html' else content,
                     mime if name == 'logo.png' else mime + '; charset=utf-8')

    def do_POST(self):
        if (not self.valid_host() or self.headers.get('X-Smart-Mic-Session') != TOKEN
                or self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}'):
            self.send_error(403)
            return
        if self.path not in ('/setup', '/device', '/pair', '/install', '/verify', '/close'):
            self.send_error(404)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise ValueError
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError
            action = self.path[1:]
            if action == 'close':
                self.server.finished = True
                result = {'ok': True}
            else:
                result = setup() if action == 'setup' else run_step(action, data)
        except (OSError, ValueError, subprocess.SubprocessError):
            result = {'ok': False, 'error': '没能完成这一步。请检查依赖、网络和 USB 连接后重新检测。'}
        self.respond(json.dumps(result).encode(), 'application/json; charset=utf-8')


def main():
    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        server.finished = False
        url = f'http://127.0.0.1:{server.server_port}/'
        print(f'Smart Mic 部署向导 {url}', flush=True)
        webbrowser.open(url)
        while not server.finished:
            server.handle_request()


if __name__ == '__main__':
    main()
