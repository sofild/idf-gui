#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP-IDF 可视化工具 —— 本地后端服务

功能：
  - 提供一个美观的网页界面（web/ 目录）
  - 通过按钮点击执行 idf.py 相关命令
  - 实时流式回显命令输出（SSE）
  - 自动探测项目目录与串口（COM 口）

仅依赖 Python 标准库，无需安装第三方包。
启动方式：python server.py
"""

import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, 'web')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

DEFAULT_CONFIG = {
    'idf_path': r'D:\esp\v6.0.3\esp-idf',
    'idf_tools_path': r'D:\esp\v6.0.3',
    'idf_python_env_path': r'D:\esp\v6.0.3\tools\python\v6.0.3\venv',
    'projects_root': r'D:\project\esp32-study',
    'port': 8000,
}

# 支持的目标芯片（set-target）
TARGETS = [
    'esp32', 'esp32s2', 'esp32s3', 'esp32c2', 'esp32c3', 'esp32c6',
    'esp32h2', 'esp32p4', 'esp32c5', 'esp32c61',
]

# 串口合法性校验（Windows COMx / Linux /dev/tty*）
PORT_RE = re.compile(r'^(COM\d+|/dev/tty[A-Za-z0-9]+|/dev/ttyACM\d+|/dev/ttyUSB\d+|/dev/cu\.[A-Za-z0-9]+)$')
# 工程/组件名合法性校验（避免命令注入）
NAME_RE = re.compile(r'^[A-Za-z0-9_\-]+$')

# 递归探测工程时需要排除的目录（编译产物、组件缓存等）
EXCLUDE_DIRS = {'build', 'managed_components', '.git', '.vs', '.vscode', '.idea', '__pycache__'}

# 全局任务表：job_id -> Job
JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 配置加载与自动探测
# ---------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def find_idf_path():
    """自动查找 ESP-IDF 框架目录（含 export.ps1 与 tools 子目录）。"""
    env = os.environ.get('IDF_PATH')
    if env and os.path.isfile(os.path.join(env, 'export.ps1')):
        return env
    roots = [
        r'D:\esp', r'D:\Espressif', r'C:\Espressif', r'C:\esp',
        os.path.join(os.path.expanduser('~'), 'esp'),
    ]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth > 3:
                dirnames[:] = []
                continue
            if 'export.ps1' in filenames and 'tools' in dirnames:
                found.append(dirpath)
    if found:
        found.sort()
        return found[0]
    return None


def find_python_env(tools_path):
    """查找 ESP-IDF Python 虚拟环境目录。"""
    # 新布局：tools/python/<ver>/venv
    p = os.path.join(tools_path, 'tools', 'python')
    if os.path.isdir(p):
        for ver in os.listdir(p):
            v = os.path.join(p, ver, 'venv')
            if os.path.isfile(os.path.join(v, 'Scripts', 'python.exe')):
                return v
    # 旧布局：python_env/idfX_pyY_env
    pe = os.path.join(tools_path, 'python_env')
    if os.path.isdir(pe):
        for d in os.listdir(pe):
            v = os.path.join(pe, d)
            if os.path.isfile(os.path.join(v, 'Scripts', 'python.exe')):
                return v
    return ''


def read_idf_version(idf_path):
    """从 version.cmake 读取 IDF 版本号。"""
    try:
        p = os.path.join(idf_path, 'tools', 'cmake', 'version.cmake')
        with open(p, 'r', encoding='utf-8') as f:
            txt = f.read()
        m = re.search(r'IDF_VERSION_MAJOR\s+(\d+)', txt)
        n = re.search(r'IDF_VERSION_MINOR\s+(\d+)', txt)
        o = re.search(r'IDF_VERSION_PATCH\s+(\d+)', txt)
        if m and n and o:
            return f'{m.group(1)}.{n.group(1)}.{o.group(1)}'
    except Exception:
        pass
    return ''


def auto_detect():
    """探测并返回配置；若探测失败则回退默认值。"""
    cfg = load_config()
    idf = cfg.get('idf_path')
    if not idf or not os.path.isfile(os.path.join(idf, 'export.ps1')):
        detected = find_idf_path()
        if detected:
            cfg['idf_path'] = detected
    idf = cfg.get('idf_path') or ''
    if idf:
        parent = os.path.dirname(idf)
        if not cfg.get('idf_tools_path'):
            cfg['idf_tools_path'] = parent
        if not cfg.get('idf_python_env_path'):
            cfg['idf_python_env_path'] = find_python_env(parent)
    return cfg


# ---------------------------------------------------------------------------
# 项目与串口探测
# ---------------------------------------------------------------------------
def is_idf_project(cmake_path):
    """判断目录是否为 ESP-IDF 工程：其 CMakeLists.txt 含 project(...) 声明。"""
    try:
        with open(cmake_path, 'r', encoding='utf-8', errors='replace') as f:
            txt = f.read(4096)
        return re.search(r'\bproject\s*\(', txt) is not None
    except Exception:
        return False


def detect_projects(root, max_depth=6):
    """在 root 下递归探测 ESP-IDF 工程（含 CMakeLists.txt 且声明 project 的目录）。"""
    projects = []
    root = root or ''
    if not os.path.isdir(root):
        return projects
    root_abs = os.path.abspath(root)
    try:
        for dirpath, dirnames, filenames in os.walk(root_abs):
            # 跳过排除目录
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith('.')]
            rel = os.path.relpath(dirpath, root_abs)
            depth = 0 if rel == '.' else rel.count(os.sep) + 1
            if depth > max_depth:
                dirnames[:] = []
                continue
            if 'CMakeLists.txt' in filenames and is_idf_project(os.path.join(dirpath, 'CMakeLists.txt')):
                projects.append(dirpath)
    except Exception:
        pass
    projects.sort()
    return projects


def detect_ports():
    """列出当前可用串口（Windows 为 COM 口）。"""
    ports = []
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             '[System.IO.Ports.SerialPort]::GetPortNames()'],
            capture_output=True, timeout=8,
        )
        for line in out.stdout.decode('utf-8', errors='replace').splitlines():
            p = line.strip()
            if p and PORT_RE.match(p):
                ports.append(p)
    except Exception:
        pass
    # 兜底：常见 COM 口（用于用户手动输入提示）
    return ports


# ---------------------------------------------------------------------------
# 命令构建
# ---------------------------------------------------------------------------
def validate(port=None, name=None, target=None):
    if port is not None and not PORT_RE.match(port):
        raise ValueError(f'串口格式不正确：{port}')
    if name is not None and not NAME_RE.match(name):
        raise ValueError(f'名称格式不正确（仅允许字母/数字/下划线/连字符）：{name}')
    if target is not None and target not in TARGETS:
        raise ValueError(f'未知芯片型号：{target}')


def build_idf_args(command, params):
    """根据命令 key 与参数，构造 idf.py 的参数列表。"""
    p = params or {}
    command = command or ''

    def port():
        return p.get('port', '').strip()

    def name(key='name'):
        return p.get(key, '').strip()

    if command == 'create-project':
        n = name()
        validate(name=n)
        if not n:
            raise ValueError('请输入工程名称')
        return ['create-project', n]

    if command == 'create-component':
        n = name('component')
        validate(name=n)
        if not n:
            raise ValueError('请输入组件名称')
        return ['-C', 'components', 'create-component', n]

    if command == 'set-target':
        t = p.get('target', '').strip()
        validate(target=t)
        return ['set-target', t]

    if command == 'menuconfig':
        return ['menuconfig']

    if command == 'build':
        return ['build']

    if command == 'clean':
        return ['clean']

    if command == 'fullclean':
        return ['fullclean']

    if command == 'reconfigure':
        return ['reconfigure']

    if command == 'python-clean':
        return ['python-clean']

    if command == 'flash':
        po = port()
        validate(port=po)
        return ['-p', po, 'flash']

    if command == 'monitor':
        po = port()
        validate(port=po)
        return ['-p', po, 'monitor']

    if command == 'flash-monitor':
        po = port()
        validate(port=po)
        return ['-p', po, 'flash', 'monitor']

    if command == 'size':
        return ['size']

    if command == 'size-components':
        return ['size-components']

    if command == 'size-files':
        return ['size-files']

    if command == 'uf2':
        return ['uf2']

    if command == 'read-otadata':
        return ['read-otadata']

    if command == 'docs':
        return ['docs']

    if command == 'merge-bin':
        out = p.get('output', '').strip()
        fmt = p.get('format', '').strip()
        args = ['merge-bin']
        if out:
            validate(name=out)
            args += ['-o', out]
        if fmt:
            validate(name=fmt)
            args += ['-f', fmt]
        return args

    raise ValueError(f'未知命令：{command}')


def ps_escape(s):
    """转义 PowerShell 单引号字符串。"""
    return s.replace("'", "''")


def ps_quote(s):
    return "'" + ps_escape(s) + "'"


def build_powershell_script(idf_args, cfg, pause_on_error=False):
    """生成完整的 PowerShell 脚本：加载环境 -> 执行 idf.py。"""
    export_ps1 = os.path.join(cfg['idf_path'], 'export.ps1')
    lines = [
        "$ErrorActionPreference = 'Continue'",
        "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}",
        "$OutputEncoding = [System.Text.Encoding]::UTF8",
        "$env:PYTHONIOENCODING = 'utf-8'",
        "$env:IDF_PATH = " + ps_quote(cfg['idf_path']),
        "$env:IDF_TOOLS_PATH = " + ps_quote(cfg['idf_tools_path']),
        "$env:IDF_PYTHON_ENV_PATH = " + ps_quote(cfg['idf_python_env_path']),
        ". " + ps_quote(export_ps1) + " *> $null",
        "idf.py " + ' '.join(ps_quote(a) for a in idf_args),
    ]
    if pause_on_error:
        # 新窗口执行时若失败，窗口会立刻关闭导致看不到错误，这里暂停一下
        lines += [
            "if ($LASTEXITCODE -ne 0) {",
            "  Write-Host ''",
            "  Write-Host ('命令失败（退出码 ' + $LASTEXITCODE + '），按回车关闭窗口...') -ForegroundColor Red",
            "  Read-Host | Out-Null",
            "}",
        ]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 任务执行
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, job_id, command, label, cwd):
        self.id = job_id
        self.command = command
        self.label = label
        self.cwd = cwd
        self.proc = None
        self.buf = collections.deque(maxlen=10000)
        self.cond = threading.Condition()
        self.done = False
        self.exit_code = None
        self.started_at = time.time()

    def append(self, item):
        with self.cond:
            self.buf.append(item)
            self.cond.notify_all()

    def finish(self, code):
        with self.cond:
            self.done = True
            self.exit_code = code
            self.cond.notify_all()


def run_job(job, idf_args, cfg, new_console=False):
    if new_console:
        # 交互式命令（如 menuconfig）：必须使用真实控制台，并且不能重定向
        # stdin/stdout/stderr，否则子进程拿不到键盘输入，TUI 也无法绘制。
        script = build_powershell_script(idf_args, cfg, pause_on_error=True)
        try:
            proc = subprocess.Popen(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                cwd=job.cwd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as e:
            job.append(('out', f'[启动失败] {e}\n'))
            job.finish(-1)
            return
        job.proc = proc
        proc.wait()
        job.finish(proc.returncode)
        return

    script = build_powershell_script(idf_args, cfg)
    try:
        proc = subprocess.Popen(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
            cwd=job.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        job.proc = proc
    except Exception as e:
        job.append(('out', f'[启动失败] {e}\n'))
        job.finish(-1)
        return

    # 逐行读取并推送到缓冲
    try:
        for raw in iter(proc.stdout.readline, b''):
            if not raw:
                break
            text = raw.decode('utf-8', errors='replace')
            job.append(('out', text))
    except Exception:
        pass
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    code = proc.wait()
    job.finish(code)


def kill_job(job):
    if not job.proc:
        return
    try:
        subprocess.run(
            ['taskkill', '/PID', str(job.proc.pid), '/T', '/F'],
            capture_output=True, timeout=8,
        )
    except Exception:
        try:
            job.proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = 'ESPIDFGUI/1.0'
    cfg = DEFAULT_CONFIG

    # -- 工具方法 ----------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        ctype = 'text/html; charset=utf-8'
        if path.endswith('.css'):
            ctype = 'text/css; charset=utf-8'
        elif path.endswith('.js'):
            ctype = 'application/javascript; charset=utf-8'
        elif path.endswith('.svg'):
            ctype = 'image/svg+xml'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _safe_static_path(self, rel):
        # 防止目录穿越
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)):
            return None
        return full

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/stream':
            self._handle_stream(parsed)
            return

        if path == '/api/config':
            self._handle_config()
            return

        if path == '/api/ports':
            self._handle_ports()
            return

        if path == '/api/jobs':
            self._handle_jobs()
            return

        # 静态文件
        if path == '/' or path == '':
            path = '/index.html'
        rel = path.lstrip('/')
        full = self._safe_static_path(rel)
        if full and os.path.isfile(full):
            self._send_file(full)
        else:
            self.send_error(404)

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/run':
            self._handle_run()
            return

        if path == '/api/kill':
            self._handle_kill()
            return

        if path == '/api/config':
            self._handle_config_post()
            return

        if path == '/api/redetect':
            self._handle_redetect()
            return

        self.send_error(404)

    # -- 各接口实现 --------------------------------------------------------
    def _config_payload(self):
        cfg = self.cfg
        return {
            'idf_path': cfg.get('idf_path', ''),
            'idf_tools_path': cfg.get('idf_tools_path', ''),
            'idf_python_env_path': cfg.get('idf_python_env_path', ''),
            'projects_root': cfg.get('projects_root', ''),
            'idf_version': read_idf_version(cfg.get('idf_path', '')),
            'projects': detect_projects(cfg.get('projects_root', '')),
            'ports': detect_ports(),
        }

    def _handle_config(self):
        self._send_json(self._config_payload())

    def _handle_config_post(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            req = json.loads(body.decode('utf-8'))
        except Exception:
            self._send_json({'error': '请求体解析失败'}, 400)
            return

        cfg = dict(self.cfg)
        for k in ('idf_path', 'idf_tools_path', 'idf_python_env_path', 'projects_root'):
            if k in req and isinstance(req[k], str) and req[k].strip():
                cfg[k] = req[k].strip()

        if not os.path.isfile(os.path.join(cfg['idf_path'], 'export.ps1')):
            self._send_json({'error': 'IDF 框架目录无效（缺少 export.ps1）'}, 400)
            return
        if not os.path.isdir(cfg['projects_root']):
            self._send_json({'error': '工程根目录不存在'}, 400)
            return

        self.cfg = cfg
        save_config(cfg)
        self._send_json({'ok': True, 'config': self._config_payload()})

    def _handle_redetect(self):
        cfg = auto_detect()
        save_config(cfg)
        self.cfg = cfg
        self._send_json({'ok': True, 'config': self._config_payload()})

    def _handle_ports(self):
        self._send_json({'ports': detect_ports()})

    def _handle_jobs(self):
        with JOBS_LOCK:
            data = [{
                'id': j.id,
                'command': j.command,
                'label': j.label,
                'cwd': j.cwd,
                'done': j.done,
                'exit_code': j.exit_code,
            } for j in JOBS.values()]
        self._send_json({'jobs': data})

    def _handle_run(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            req = json.loads(body.decode('utf-8'))
        except Exception:
            self._send_json({'error': '请求体解析失败'}, 400)
            return

        command = req.get('command', '')
        params = req.get('params', {}) or {}
        cwd = req.get('cwd', '').strip() or self.cfg.get('projects_root', '')

        try:
            idf_args = build_idf_args(command, params)
        except ValueError as e:
            self._send_json({'error': str(e)}, 400)
            return

        if not os.path.isdir(cwd):
            self._send_json({'error': f'工作目录不存在：{cwd}'}, 400)
            return

        new_console = (command == 'menuconfig')
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, command, req.get('label', command), cwd)
        with JOBS_LOCK:
            JOBS[job_id] = job

        t = threading.Thread(target=run_job, args=(job, idf_args, self.cfg, new_console), daemon=True)
        t.start()

        self._send_json({'job_id': job_id})

    def _handle_kill(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            req = json.loads(body.decode('utf-8'))
            job_id = req.get('job_id', '')
        except Exception:
            self._send_json({'error': '请求体解析失败'}, 400)
            return
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            self._send_json({'error': '任务不存在'}, 404)
            return
        kill_job(job)
        self._send_json({'ok': True})

    def _handle_stream(self, parsed):
        job_id = parse_qs(parsed.query).get('job', [''])[0]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        def write(event, data):
            payload = json.dumps(data, ensure_ascii=False)
            try:
                self.wfile.write(f'event: {event}\ndata: {payload}\n\n'.encode('utf-8'))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise

        try:
            # 先发送元信息
            write('meta', {'job_id': job.id, 'command': job.command, 'label': job.label, 'cwd': job.cwd})
            idx = 0
            sent_exit = False
            while True:
                with job.cond:
                    job.cond.wait(timeout=0.3)
                    items = list(job.buf)[idx:]
                    idx = len(job.buf)
                    done = job.done
                    code = job.exit_code
                for it in items:
                    if it[0] == 'out':
                        write('out', {'data': it[1]})
                if done and idx >= len(job.buf):
                    if not sent_exit:
                        write('exit', {'code': code})
                        sent_exit = True
                    break
                if not items:
                    # 心跳
                    try:
                        self.wfile.write(b': ping\n\n')
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        # 静默访问日志，避免刷屏
        pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    cfg = auto_detect()
    save_config(cfg)
    Handler.cfg = cfg

    host = '127.0.0.1'
    port = int(cfg.get('port') or 8000)

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f'http://{host}:{port}/'

    print('=' * 56)
    print('  ESP-IDF 可视化工具已启动')
    print(f'  访问地址: {url}')
    print(f'  IDF 路径: {cfg.get("idf_path")}')
    print(f'  工程目录: {cfg.get("projects_root")}')
    print('  按 Ctrl+C 停止服务')
    print('=' * 56)

    # 自动打开浏览器
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')


if __name__ == '__main__':
    main()
