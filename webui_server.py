"""
BAAS WebUI 后端
运行方式: python webui_server.py [--config default] [--port 8765]
放在 blue_archive_auto_script/ 根目录
依赖: pip install fastapi uvicorn
"""
import argparse
import asyncio
import collections
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ── 全局状态 ─────────────────────────────────────────────────────────────────

_state = {
    'running': False,
    'currentTask': None,
    'resources': {'ap': 0, 'apMax': 180, 'pyroxene': 0, 'credits': 0},
}
_log_q: queue.Queue = queue.Queue()
_log_history: collections.deque = collections.deque(maxlen=500)  # 历史日志回放
_ws_clients: set[WebSocket] = set()
_proc: Optional[subprocess.Popen] = None
_oneshot_proc: Optional[subprocess.Popen] = None  # 一次性任务进程
_config_dir: str = 'default'

# ── 子进程 stdout 读取 ──────────────────────────────────────────────────────

ANSI = re.compile(r'\x1b\[[0-9;]*[mGKH]')

def _read_stdout(proc: subprocess.Popen) -> None:
    log_id = int(time.time() * 1000)
    for raw in proc.stdout:
        line = ANSI.sub('', raw).strip()
        if not line:
            continue
        if 'resource_tracker' in line or 'leaked shared_memory' in line:
            continue
        log_id += 1
        # 尝试解析 _webui_runner.py 输出的 JSON 行
        try:
            msg = json.loads(line)
            msg_type = msg.get('type')
            if msg_type == 'log':
                msg['data']['id'] = log_id
                _log_history.append(msg)
                _log_q.put(msg)
            elif msg_type == 'status':
                _state.update(msg.get('data', {}))
                _log_q.put(msg)
            elif msg_type == 'resource':
                _state['resources'].update(msg.get('data', {}))
                _log_q.put({'type': 'status', 'data': {'resources': _state['resources']}})
            continue
        except (json.JSONDecodeError, KeyError):
            pass
        # 回退：解析普通文本日志
        level = 'INFO'
        message = line
        for lvl in ('CRITICAL', 'ERROR', 'WARNING', 'INFO'):
            if f' {lvl} |' in line or line.startswith(lvl):
                level = lvl
                parts = line.split('|', 2)
                message = parts[2].strip() if len(parts) == 3 else line
                break
        if 'current_task:' in message:
            m = re.search(r'current_task:\s*(.+?)\s+start', message)
            if m:
                _state['currentTask'] = m.group(1)
                _log_q.put({'type': 'status', 'data': {'currentTask': _state['currentTask']}})
        log_msg = {'type': 'log', 'data': {
            'id': log_id, 'level': level,
            'time': datetime.now().strftime('%H:%M:%S'), 'message': message
        }}
        _log_history.append(log_msg)
        _log_q.put(log_msg)

    _state['running'] = False
    _state['currentTask'] = None
    _log_q.put({'type': 'status', 'data': {'running': False, 'currentTask': None}})

# ── WebSocket 广播 ───────────────────────────────────────────────────────────

async def _broadcast(msg: dict) -> None:
    dead = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)

async def _queue_broadcaster() -> None:
    while True:
        drained = 0
        while drained < 50:
            try:
                msg = _log_q.get_nowait()
                await _broadcast(msg)
                drained += 1
            except queue.Empty:
                break
        await asyncio.sleep(0.05)

# ── FastAPI 应用 ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时清理残留的 runner 进程（上次 webui_server 异常退出遗留）
    try:
        result = subprocess.run(['pgrep', '-f', '_webui_runner.py'], capture_output=True, text=True)
        for pid_str in result.stdout.split():
            try:
                os.kill(int(pid_str), 15)  # SIGTERM
            except (ProcessLookupError, ValueError):
                pass
    except FileNotFoundError:
        pass  # pgrep 不存在则跳过
    asyncio.create_task(_queue_broadcaster())
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'],
                   allow_headers=['*'], allow_credentials=True)

# ── 辅助：路径解析 ────────────────────────────────────────────────────────────

def _cfg_path(name: str) -> str:
    base = os.path.join(os.path.dirname(__file__), 'config', _config_dir)
    return os.path.join(base, name)

def _read_json(name: str) -> dict | list:
    p = _cfg_path(name)
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    with open(p, encoding='utf-8') as f:
        return json.load(f)

def _write_json(name: str, data: dict | list) -> None:
    p = _cfg_path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── REST API ─────────────────────────────────────────────────────────────────

@app.get('/api/status')
def get_status():
    return _state


@app.get('/api/tasks')
def get_tasks():
    try:
        return _read_json('event.json')
    except FileNotFoundError:
        return []

_TASK_REQUIRED = {'func_name', 'event_name', 'enabled', 'priority', 'interval', 'next_tick'}

@app.put('/api/tasks')
async def put_tasks(request: Request):
    from fastapi import HTTPException
    body = await request.json()
    if not isinstance(body, list) or not body:
        raise HTTPException(400, 'Body must be a non-empty list of tasks')
    for t in body:
        if not isinstance(t, dict) or not _TASK_REQUIRED.issubset(t.keys()):
            raise HTTPException(400, f'Each task must have fields: {_TASK_REQUIRED}')
    # ensure func_names match existing tasks (no phantom tasks)
    try:
        existing = {t['func_name'] for t in _read_json('event.json')}
        incoming = {t['func_name'] for t in body}
        if incoming != existing:
            raise HTTPException(400, f'func_names mismatch: cannot add or remove tasks via this endpoint')
    except FileNotFoundError:
        pass
    _write_json('event.json', body)
    return {'ok': True}

@app.get('/api/config')
def get_config():
    try:
        return _read_json('config.json')
    except FileNotFoundError:
        return {}

@app.put('/api/config')
async def put_config(body: dict):
    global _config_dir
    from fastapi import HTTPException
    if not body or 'server' not in body:
        raise HTTPException(400, 'Invalid config: must be a non-empty config object with at least a "server" field')
    config_dir = body.pop('_configDir', None)
    if config_dir:
        base = os.path.join(os.path.dirname(__file__), 'config')
        if os.path.isdir(os.path.join(base, config_dir)):
            _config_dir = config_dir
    _write_json('config.json', body)
    return {'ok': True}

@app.post('/api/start')
async def start_script(body: dict = {}):
    global _proc, _config_dir
    proc_alive = _proc is not None and _proc.poll() is None
    if proc_alive:
        return {'ok': False, 'error': '已在运行中'}
    if _state['running'] and not proc_alive:
        _state['running'] = False
        _state['currentTask'] = None
        _log_q.put({'type': 'status', 'data': {'running': False, 'currentTask': None}})
    cfg = body.get('configDir', _config_dir)
    _config_dir = cfg
    runner = os.path.join(os.path.dirname(__file__), '_webui_runner.py')
    env = {**os.environ, 'BAAS_CONFIG_DIR': cfg,
           'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1', 'FORCE_COLOR': '0',
           'QT_QPA_PLATFORM': 'offscreen'}
    _proc = subprocess.Popen(
        [sys.executable, runner],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env, cwd=os.path.dirname(__file__)
    )
    _state['running'] = True
    _state['currentTask'] = None
    threading.Thread(target=_read_stdout, args=(_proc,), daemon=True).start()
    _log_q.put({'type': 'status', 'data': {'running': True}})
    return {'ok': True}

@app.post('/api/stop')
async def stop_script():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
    _state['running'] = False
    _state['currentTask'] = None
    _log_q.put({'type': 'status', 'data': {'running': False, 'currentTask': None}})
    return {'ok': True}

_ALLOWED_TASKS = {
    'explore_activity_story', 'explore_activity_mission', 'explore_activity_challenge',
    'explore_normal_task', 'explore_hard_task',
}

@app.post('/api/run-task')
async def run_task(body: dict):
    global _oneshot_proc
    from fastapi import HTTPException
    task = body.get('task', '').strip()
    if task not in _ALLOWED_TASKS:
        raise HTTPException(400, f'Unknown task: {task}')
    if _proc and _proc.poll() is None:
        raise HTTPException(409, '主调度器正在运行，请先停止')
    if _oneshot_proc and _oneshot_proc.poll() is None:
        raise HTTPException(409, '已有单次任务在运行')
    runner = os.path.join(os.path.dirname(__file__), '_webui_runner.py')
    env = {**os.environ, 'BAAS_CONFIG_DIR': _config_dir, 'BAAS_TASK': task,
           'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1', 'FORCE_COLOR': '0',
           'QT_QPA_PLATFORM': 'offscreen'}
    _oneshot_proc = subprocess.Popen(
        [sys.executable, runner],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env, cwd=os.path.dirname(__file__)
    )
    threading.Thread(target=_read_stdout, args=(_oneshot_proc,), daemon=True).start()
    _log_q.put({'type': 'status', 'data': {'currentTask': task}})
    return {'ok': True}

@app.get('/api/activity')
def get_activity():
    static_path = os.path.join(os.path.dirname(__file__),
                                'core', 'ocr', 'baas_ocr_client', 'bin', 'resource', 'static.json')
    try:
        with open(static_path, encoding='utf-8') as f:
            static = json.load(f)
        activity_map: dict = static.get('current_game_activity', {})
    except Exception:
        return {'CN': None, 'JP': None, 'Global': None}
    result = {}
    for server, name in activity_map.items():
        if not name:
            result[server] = None
            continue
        stage_path = os.path.join(os.path.dirname(__file__),
                                   'src', 'explore_task_data', 'activities', f'{name}.json')
        try:
            with open(stage_path, encoding='utf-8') as f:
                stages = json.load(f)
        except Exception:
            stages = {}
        result[server] = {'name': name, 'stages': stages}
    return result

@app.get('/api/configs')
def list_configs():
    base = os.path.join(os.path.dirname(__file__), 'config')
    dirs = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    return {'configs': dirs, 'active': _config_dir}

@app.post('/api/configs')
async def create_config(body: dict):
    name = body.get('name', '').strip()
    if not name or any(c in name for c in r'/\..'):
        from fastapi import HTTPException
        raise HTTPException(400, 'Invalid config name')
    base = os.path.join(os.path.dirname(__file__), 'config')
    new_dir = os.path.join(base, name)
    if os.path.exists(new_dir):
        from fastapi import HTTPException
        raise HTTPException(409, 'Config already exists')
    os.makedirs(new_dir)
    default_dir = os.path.join(base, 'default')
    for fname in ('config.json', 'event.json', 'switch.json'):
        src = os.path.join(default_dir, fname)
        if os.path.exists(src):
            shutil.copy(src, new_dir)
    return {'ok': True, 'name': name}

@app.delete('/api/configs/{name}')
async def delete_config(name: str):
    from fastapi import HTTPException
    if name == _config_dir:
        raise HTTPException(400, 'Cannot delete the active config')
    base = os.path.join(os.path.dirname(__file__), 'config')
    target = os.path.join(base, name)
    if not os.path.isdir(target):
        raise HTTPException(404, 'Config not found')
    shutil.rmtree(target)
    return {'ok': True}

@app.put('/api/active-config')
async def set_active_config(body: dict):
    global _config_dir
    name = body.get('configDir', '').strip()
    base = os.path.join(os.path.dirname(__file__), 'config')
    if not name or not os.path.isdir(os.path.join(base, name)):
        from fastapi import HTTPException
        raise HTTPException(404, 'Config not found')
    _config_dir = name
    return {'ok': True, 'active': _config_dir}

@app.post('/api/adb/connect')
async def adb_connect(body: dict):
    serial = body.get('serial', '')
    if not serial:
        return {'ok': False, 'error': '未提供设备地址'}
    try:
        r = subprocess.run(['adb', 'connect', serial], capture_output=True, text=True, timeout=10)
        output = (r.stdout + r.stderr).strip()
        ok = 'connected' in output.lower() or 'already' in output.lower()
        return {'ok': ok, 'output': output}
    except FileNotFoundError:
        return {'ok': False, 'error': 'adb 未安装，请先安装 Android Platform Tools'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'ADB 连接超时'}

# ── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    # 连接时校正运行状态：进程已退出但状态未清除时修正
    if _state['running'] and not (_proc is not None and _proc.poll() is None):
        _state['running'] = False
        _state['currentTask'] = None
    # 发送当前状态
    await ws.send_json({'type': 'init', 'data': _state})
    # 回放历史日志（最多 500 条）
    if _log_history:
        await ws.send_json({'type': 'history', 'data': list(_log_history)})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(ws)

# ── 静态文件（可选：部署时使用构建后的前端）───────────────────────────────────

_static_dir = os.path.join(os.path.dirname(__file__), '..', 'baas-webui', 'dist')
if os.path.isdir(_static_dir):
    app.mount('/', StaticFiles(directory=_static_dir, html=True), name='static')

# ── 入口 ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='default', help='config dir name under config/')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default='0.0.0.0')
    args = parser.parse_args()
    _config_dir = args.config
    print(f'BAAS WebUI Server  http://{args.host}:{args.port}')
    print(f'Config dir: config/{args.config}')
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')
