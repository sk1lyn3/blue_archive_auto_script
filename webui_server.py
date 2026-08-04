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

from core.device.adb_path import adb_directory, adb_executable

VIRTUAL_DISPLAY_IDLE = {'running': False, 'starting': False, 'displayId': None, 'configDir': None}
MANAGED_MODE_IDLE = {
    'running': False,
    'configs': [],
    'currentConfig': None,
    'nextWakeAt': None,
    'phase': 'idle',
}

# ── 全局状态 ─────────────────────────────────────────────────────────────────

_state = {
    'running': False,
    'currentTask': None,
    'resources': {'ap': 0, 'apMax': 180, 'pyroxene': 0, 'credits': 0},
    'virtualDisplay': VIRTUAL_DISPLAY_IDLE.copy(),
    'managedMode': MANAGED_MODE_IDLE.copy(),
}
_log_q: queue.Queue = queue.Queue()
_log_history: collections.deque = collections.deque(maxlen=500)  # 历史日志回放
_ws_clients: set[WebSocket] = set()
_proc: Optional[subprocess.Popen] = None
_oneshot_proc: Optional[subprocess.Popen] = None  # 一次性任务进程
_config_dir: str = 'default'

def _proc_alive(proc: Optional[subprocess.Popen]) -> bool:
    return proc is not None and proc.poll() is None

def _any_runner_alive() -> bool:
    return _proc_alive(_proc) or _proc_alive(_oneshot_proc)

def _managed_mode_running() -> bool:
    return bool((_state.get('managedMode') or {}).get('running'))

def _terminate_proc(proc: Optional[subprocess.Popen]) -> None:
    if not _proc_alive(proc):
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

def _set_status(**data) -> None:
    _state.update(data)
    _log_q.put({'type': 'status', 'data': data})

def _set_idle_status() -> None:
    _set_status(running=False, currentTask=None)

def _set_virtual_display(**data) -> None:
    _state['virtualDisplay'].update(data)
    _log_q.put({'type': 'status', 'data': {'virtualDisplay': _state['virtualDisplay']}})

def _reset_virtual_display() -> None:
    _state['virtualDisplay'] = VIRTUAL_DISPLAY_IDLE.copy()
    _log_q.put({'type': 'status', 'data': {'virtualDisplay': _state['virtualDisplay']}})

def _set_managed_mode(**data) -> None:
    _state['managedMode'].update(data)
    _log_q.put({'type': 'status', 'data': {'managedMode': _state['managedMode']}})

def _reset_managed_mode() -> None:
    _state['managedMode'] = MANAGED_MODE_IDLE.copy()
    _log_q.put({'type': 'status', 'data': {'managedMode': _state['managedMode']}})

def _runner_env(**extra: str) -> dict:
    env = {**os.environ, **extra,
           'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1', 'FORCE_COLOR': '0',
           'QT_QPA_PLATFORM': 'offscreen'}
    adb_dir = adb_directory()
    if adb_dir:
        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')
    return env

def _start_runner(env: dict, persistent: bool = False) -> subprocess.Popen:
    runner = os.path.join(os.path.dirname(__file__), '_webui_runner.py')
    return subprocess.Popen(
        [sys.executable, runner],
        stdin=subprocess.PIPE if persistent else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, env=env, cwd=os.path.dirname(__file__),
        bufsize=1 if persistent else -1,
    )

def _active_screenshot_method() -> str:
    try:
        return str(_read_json('config.json').get('screenshot_method', ''))
    except Exception:
        return ''

def _config_screenshot_method(config_dir: str) -> str:
    try:
        with open(_cfg_path_for(config_dir, 'config.json'), encoding='utf-8') as f:
            return str(json.load(f).get('screenshot_method', ''))
    except Exception:
        return ''

def _uses_virtual_scrcpy() -> bool:
    return _active_screenshot_method() == 'virtual_scrcpy'

def _virtual_display_alive() -> bool:
    vd = _state.get('virtualDisplay') or {}
    return _proc_alive(_proc) and bool(vd.get('running') or vd.get('starting'))

def _virtual_display_running() -> bool:
    vd = _state.get('virtualDisplay') or {}
    return _proc_alive(_proc) and bool(vd.get('running')) and not bool(vd.get('starting'))

async def _wait_virtual_display_running(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _virtual_display_running():
            return True
        await asyncio.sleep(0.5)
    return False

def _runner_cmd(cmd: dict) -> tuple[bool, str | None]:
    if not _proc_alive(_proc) or _proc.stdin is None:
        return False, '虚拟屏幕未启动'
    try:
        _proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + '\n')
        _proc.stdin.flush()
        return True, None
    except (BrokenPipeError, OSError) as e:
        return False, str(e)

# ── 子进程 stdout 读取 ──────────────────────────────────────────────────────

ANSI = re.compile(r'\x1b\[[0-9;]*[mGKH]')

def _read_stdout(proc: subprocess.Popen, role: str = 'task') -> None:
    global _proc, _oneshot_proc
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
            elif msg_type == 'virtual_display':
                _state['virtualDisplay'].update(msg.get('data', {}))
                _log_q.put({'type': 'status', 'data': {'virtualDisplay': _state['virtualDisplay']}})
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

    if role == 'persistent' and _proc is proc:
        _proc = None
        _reset_virtual_display()
        _reset_managed_mode()
    elif role == 'oneshot' and _oneshot_proc is proc:
        _oneshot_proc = None

    if not _any_runner_alive():
        _set_idle_status()

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
            except (ProcessLookupError, PermissionError, ValueError):
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

def _cfg_path_for(config_dir: str, name: str) -> str:
    base = os.path.join(os.path.dirname(__file__), 'config', config_dir)
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
    if _managed_mode_running():
        return {'ok': False, 'error': '托管模式正在运行，请先停止托管模式'}
    cfg = body.get('configDir', _config_dir)
    if cfg:
        _config_dir = cfg
    if _uses_virtual_scrcpy():
        ok, err = _runner_cmd({'cmd': 'start_scheduler', 'configDir': _config_dir})
        if not ok:
            return {'ok': False, 'error': err or '请先启动虚拟屏幕'}
        _set_status(running=True, currentTask=None)
        return {'ok': True}
    if _any_runner_alive():
        return {'ok': False, 'error': '已在运行中'}
    if _state['running']:
        _set_idle_status()
    _proc = _start_runner(_runner_env(BAAS_CONFIG_DIR=cfg))
    _set_status(running=True, currentTask=None)
    threading.Thread(target=_read_stdout, args=(_proc,), daemon=True).start()
    return {'ok': True}

@app.post('/api/stop')
async def stop_script():
    global _proc, _oneshot_proc
    if _virtual_display_alive():
        ok, err = _runner_cmd({'cmd': 'stop_task'})
        if not ok:
            return {'ok': False, 'error': err or '停止任务失败'}
        _terminate_proc(_oneshot_proc)
        _oneshot_proc = None
        _set_idle_status()
        _reset_managed_mode()
        return {'ok': True}
    _terminate_proc(_proc)
    _terminate_proc(_oneshot_proc)
    _proc = None
    _oneshot_proc = None
    _set_idle_status()
    _reset_managed_mode()
    return {'ok': True}

_ALLOWED_TASKS = {
    'explore_activity_story', 'explore_activity_mission', 'explore_activity_challenge',
    'explore_normal_task', 'explore_hard_task',
    'main_story', 'group_story', 'mini_story',
    'de_clothes',
}

@app.post('/api/run-task')
async def run_task(body: dict):
    global _oneshot_proc, _config_dir
    from fastapi import HTTPException
    if _managed_mode_running():
        raise HTTPException(409, '托管模式正在运行，请先停止托管模式')
    cfg = body.get('configDir', _config_dir)
    if cfg:
        _config_dir = cfg
    task = body.get('task', '').strip()
    if task not in _ALLOWED_TASKS:
        raise HTTPException(400, f'Unknown task: {task}')
    options = body.get('options') or {}
    if _uses_virtual_scrcpy():
        ok, err = _runner_cmd({'cmd': 'run_task', 'task': task, 'configDir': _config_dir, 'options': options})
        if not ok:
            raise HTTPException(409, err or '请先启动虚拟屏幕')
        _set_status(running=True, currentTask=task)
        return {'ok': True}
    if _proc_alive(_proc):
        raise HTTPException(409, '主调度器正在运行，请先停止')
    if _proc_alive(_oneshot_proc):
        raise HTTPException(409, '已有单次任务在运行')
    _oneshot_proc = _start_runner(_runner_env(
        BAAS_CONFIG_DIR=_config_dir,
        BAAS_TASK=task,
        BAAS_TASK_OPTIONS=json.dumps(options, ensure_ascii=False),
    ))
    threading.Thread(target=_read_stdout, args=(_oneshot_proc, 'oneshot'), daemon=True).start()
    _set_status(running=True, currentTask=task)
    return {'ok': True}

@app.post('/api/virtual-display/start')
async def start_virtual_display(body: dict = {}):
    global _proc, _config_dir
    from fastapi import HTTPException
    cfg = body.get('configDir', _config_dir)
    if cfg:
        _config_dir = cfg
    if not _uses_virtual_scrcpy():
        raise HTTPException(400, '当前配置未使用 virtual_scrcpy')
    if _proc_alive(_proc):
        ok, err = _runner_cmd({'cmd': 'virtual_status'})
        if not ok:
            return {'ok': False, 'error': err or '虚拟屏幕状态查询失败'}
        return {'ok': True}
    if _proc_alive(_oneshot_proc):
        raise HTTPException(409, '已有单次任务在运行')

    _state['virtualDisplay'] = VIRTUAL_DISPLAY_IDLE.copy()
    _set_virtual_display(starting=True, configDir=_config_dir)
    _proc = _start_runner(_runner_env(BAAS_CONFIG_DIR=_config_dir, BAAS_PERSISTENT='1'), persistent=True)
    threading.Thread(target=_read_stdout, args=(_proc, 'persistent'), daemon=True).start()
    return {'ok': True}

@app.post('/api/virtual-display/stop')
async def stop_virtual_display():
    global _proc
    if _managed_mode_running():
        return {'ok': False, 'error': '托管模式运行中，请先停止托管模式'}
    if _state.get('running'):
        return {'ok': False, 'error': '任务运行中，请先停止任务'}
    if _proc_alive(_proc):
        ok, err = _runner_cmd({'cmd': 'shutdown_virtual_display'})
        if not ok:
            return {'ok': False, 'error': err or '关闭虚拟屏幕失败'}
        try:
            _proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _terminate_proc(_proc)
        _proc = None
    _reset_virtual_display()
    return {'ok': True}

@app.get('/api/managed-mode/status')
def managed_mode_status():
    return _state['managedMode']

@app.post('/api/managed-mode/start')
async def start_managed_mode(body: dict):
    global _proc, _config_dir
    from fastapi import HTTPException
    if _managed_mode_running():
        raise HTTPException(409, '托管模式已在运行')
    configs = body.get('configs') or []
    if not isinstance(configs, list):
        raise HTTPException(400, 'configs must be a list')
    configs = [str(c).strip() for c in configs if str(c).strip()]
    configs = list(dict.fromkeys(configs))
    if not configs:
        raise HTTPException(400, '至少选择一个配置文件')
    base = os.path.join(os.path.dirname(__file__), 'config')
    for cfg in configs:
        if not os.path.isdir(os.path.join(base, cfg)):
            raise HTTPException(404, f'配置不存在: {cfg}')
        if _config_screenshot_method(cfg) != 'virtual_scrcpy':
            raise HTTPException(400, f'配置 {cfg} 未使用 virtual_scrcpy')
    if _proc_alive(_oneshot_proc):
        raise HTTPException(409, '已有单次任务在运行')
    if _state.get('running') and not _managed_mode_running():
        raise HTTPException(409, '已有任务在运行')

    _config_dir = configs[0]
    started_runner = False
    if not _proc_alive(_proc):
        _state['virtualDisplay'] = VIRTUAL_DISPLAY_IDLE.copy()
        _set_virtual_display(starting=True, configDir=_config_dir)
        _proc = _start_runner(_runner_env(BAAS_CONFIG_DIR=_config_dir, BAAS_PERSISTENT='1'), persistent=True)
        threading.Thread(target=_read_stdout, args=(_proc, 'persistent'), daemon=True).start()
        started_runner = True

    if started_runner or not _virtual_display_running():
        if not await _wait_virtual_display_running():
            raise HTTPException(504, '虚拟屏幕启动超时')

    ok, err = _runner_cmd({'cmd': 'start_managed', 'configs': configs})
    if not ok:
        raise HTTPException(409, err or '托管模式启动失败')
    _set_status(running=True, currentTask='托管待机')
    _set_managed_mode(running=True, configs=configs, currentConfig=None, nextWakeAt=None, phase='starting')
    return {'ok': True}

@app.post('/api/managed-mode/stop')
async def stop_managed_mode():
    if not _managed_mode_running() and not _virtual_display_alive():
        _reset_managed_mode()
        return {'ok': True}
    ok, err = _runner_cmd({'cmd': 'stop_task'})
    if not ok:
        return {'ok': False, 'error': err or '托管模式停止失败'}
    _set_idle_status()
    _reset_managed_mode()
    return {'ok': True}

@app.post('/api/virtual-display/wake-unlock-screen-off')
async def wake_unlock_screen_off():
    from fastapi import HTTPException
    if not _uses_virtual_scrcpy():
        raise HTTPException(400, '当前配置未使用 virtual_scrcpy')
    if not _virtual_display_alive():
        raise HTTPException(409, '虚拟屏幕未启动')
    ok, err = _runner_cmd({'cmd': 'wake_unlock_screen_off'})
    if not ok:
        raise HTTPException(409, err or '唤醒解锁并关屏失败')
    return {'ok': True}

@app.get('/api/activity')
def get_activity():
    try:
        from core.config.default_config import STATIC_DEFAULT_CONFIG
        activity_map: dict = json.loads(STATIC_DEFAULT_CONFIG).get('current_game_activity', {})
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


@app.get('/api/static-config')
def get_static_config():
    try:
        with open(os.path.join(os.path.dirname(__file__), 'config', 'static.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


@app.get('/api/help')
def get_help(lang: str = 'zh_CN'):
    lang = re.sub(r'[^A-Za-z0-9_-]', '', lang) or 'zh_CN'
    base = os.path.join(os.path.dirname(__file__), 'src', 'descriptions')
    docs_dir = os.path.join(base, lang)
    if not os.path.isdir(docs_dir):
        docs_dir = os.path.join(base, 'zh_CN')
    docs = []
    try:
        for file_name in sorted(os.listdir(docs_dir)):
            if not file_name.endswith('.html'):
                continue
            path = os.path.join(docs_dir, file_name)
            with open(path, encoding='utf-8') as f:
                docs.append({
                    'title': os.path.splitext(file_name)[0],
                    'content': f.read(),
                })
    except Exception:
        return {'docs': []}
    return {'docs': docs}

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
        r = subprocess.run([adb_executable(), 'connect', serial], capture_output=True, text=True, timeout=10)
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
    if _state['running'] and not _any_runner_alive():
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
