"""
BAAS 子进程运行器 —— 由 webui_server.py 启动，不要直接运行
stdout 输出 JSON Lines，webui_server.py 解析后转发给 WebSocket 客户端
"""
import json
import os
import atexit
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

# 在任何 BAAS 类被导入前注入 virtual_scrcpy 兼容补丁
import core.virtual_scrcpy_compat
core.virtual_scrcpy_compat.apply()

from core.device.adb_path import adb_executable
from core.device.display_backend import DISPLAY_METHODS, display_backend, release_display_backends


def _display_client(baas):
    return display_backend(baas.config.screenshot_method).get_instance(baas.serial)


def _cleanup_virtual_scrcpy():
    try:
        release_display_backends()
    except Exception:
        pass


def _handle_exit_signal(signum, _frame):
    _cleanup_virtual_scrcpy()
    sys.exit(128 + signum)


atexit.register(_cleanup_virtual_scrcpy)
signal.signal(signal.SIGTERM, _handle_exit_signal)
signal.signal(signal.SIGINT, _handle_exit_signal)

# ── 自定义信号类（替代 PyQt 信号）────────────────────────────────────────────

class _LogSignal:
    _LEVELS = {1: 'INFO', 2: 'WARNING', 3: 'ERROR', 4: 'CRITICAL'}

    def emit(self, level: int, message: str) -> None:
        print(json.dumps({
            'type': 'log',
            'data': {
                'level': self._LEVELS.get(level, 'INFO'),
                'time': datetime.now().strftime('%H:%M:%S'),
                'message': message,
            }
        }), flush=True)

class _ButtonSignal:
    def emit(self, text: str) -> None:
        running = text in ('停止', 'Stop')
        print(json.dumps({'type': 'status', 'data': {'running': running}}), flush=True)

class _UpdateSignal:
    def emit(self, data: list) -> None:
        if not data:
            return
        item = data[0]
        task = item if isinstance(item, str) else item.get('event_name', str(item))
        print(json.dumps({'type': 'status', 'data': {'currentTask': task}}), flush=True)


def _emit_status(data: dict) -> None:
    print(json.dumps({'type': 'status', 'data': data}), flush=True)


def _emit_virtual_display(data: dict) -> None:
    print(json.dumps({'type': 'virtual_display', 'data': data}), flush=True)


def _emit_managed_mode(**data) -> None:
    status = {
        'running': False,
        'configs': [],
        'currentConfig': None,
        'nextWakeAt': None,
        'phase': 'idle',
    }
    status.update(data)
    _emit_status({'managedMode': status})


def _apply_task_options(baas, options):
    previous = {
        'activity_use_generic_entry': getattr(baas, 'activity_use_generic_entry', False),
        'activity_generic_total_story': getattr(baas, 'activity_generic_total_story', None),
        'activity_generic_start_at_menu': getattr(baas, 'activity_generic_start_at_menu', False),
    }
    options = options or {}
    baas.activity_use_generic_entry = bool(options.get('activity_use_generic_entry', False))
    baas.activity_generic_start_at_menu = bool(options.get('activity_generic_start_at_menu', False))
    total_story = options.get('activity_generic_total_story')
    try:
        baas.activity_generic_total_story = max(1, min(99, int(total_story)))
    except (TypeError, ValueError):
        baas.activity_generic_total_story = None
    return previous


def _restore_task_options(baas, previous):
    for key, value in previous.items():
        setattr(baas, key, value)


def _virtual_display_data(baas=None, config_set=None, running=True) -> dict:
    data = {'running': running, 'starting': False}
    try:
        if config_set is not None:
            data['configDir'] = os.path.basename(config_set.config_dir)
        if running and baas is not None and getattr(baas.config, 'screenshot_method', '') in DISPLAY_METHODS:
            cli = _display_client(baas)
            data['displayId'] = cli.display_id
    except Exception:
        pass
    return data


def _start_resource_reporter(config_set):
    def _resource_reporter():
        while True:
            time.sleep(30)
            try:
                ap_data = config_set.get('ap') or {}
                pyr_data = config_set.get('pyroxene') or {}
                crd_data = config_set.get('creditpoints') or {}
                print(json.dumps({'type': 'resource', 'data': {
                    'ap':       ap_data.get('count', 0),
                    'apMax':    ap_data.get('max', 180),
                    'pyroxene': pyr_data.get('count', 0),
                    'credits':  crd_data.get('count', 0),
                }}), flush=True)
            except Exception:
                pass

    threading.Thread(target=_resource_reporter, daemon=True).start()


def _launch_game_on_virtual_display(baas, log_signal):
    if getattr(baas.config, 'screenshot_method', '') not in DISPLAY_METHODS:
        return
    activity_name = baas.activity_name
    cli = _display_client(baas)
    log_signal.emit(1, 'Launch game on virtual display')
    cli.launch_app(baas.package_name, activity_name)


def _wake_unlock_and_turn_screen_off(baas, config_set, log_signal):
    if getattr(baas.config, 'screenshot_method', '') not in DISPLAY_METHODS:
        log_signal.emit(3, 'The current configuration does not use a virtual display backend')
        return False
    try:
        serial = baas.serial
        display_state = _physical_display_state(serial)
        log_signal.emit(1, f'Physical display state before wake: {display_state}')
        if display_state == 'OFF':
            subprocess.run(
                [adb_executable(), '-s', serial, 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP'],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                [adb_executable(), '-s', serial, 'shell', 'wm', 'dismiss-keyguard'],
                capture_output=True,
                timeout=5,
            )
            time.sleep(0.2)

        cli = _display_client(baas)
        cli.turn_screen_off()
        log_signal.emit(1, 'Wake/unlock device and turn physical screen off')
        _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))
        return True
    except Exception as e:
        log_signal.emit(3, f'Failed to wake/unlock the device and turn off the physical screen: {e}')
        return False


def _physical_display_state(serial):
    result = subprocess.run(
        [adb_executable(), '-s', serial, 'shell', 'dumpsys', 'display'],
        capture_output=True,
        text=True,
        timeout=5,
    )
    screen_state = None
    internal_state = None
    for line in result.stdout.splitlines():
        state_match = re.search(r'\bDisplay State=(ON|OFF)\b', line)
        if state_match:
            return state_match.group(1)

        screen_match = re.search(r'\bmScreenState=(ON|OFF)\b', line)
        if screen_match:
            screen_state = screen_match.group(1)

        if 'DisplayDeviceInfo{' in line and 'type INTERNAL' in line:
            internal_match = re.search(r'\bstate (ON|OFF)\b', line)
            if internal_match:
                internal_state = internal_match.group(1)

    return screen_state or internal_state or 'UNKNOWN'


def _send_power_key(serial, log_signal):
    try:
        subprocess.run(
            [adb_executable(), '-s', serial, 'shell', 'input', 'keyevent', 'KEYCODE_POWER'],
            capture_output=True,
            timeout=5,
        )
        log_signal.emit(1, 'Send power key before virtual display shutdown')
    except Exception as e:
        log_signal.emit(2, f'Failed to send the power key: {e}')


def _turn_physical_screen_off(serial, log_signal):
    state = _physical_display_state(serial)
    if state == 'OFF':
        log_signal.emit(1, 'Physical screen already off for managed idle')
        return True
    try:
        subprocess.run(
            [adb_executable(), '-s', serial, 'shell', 'input', 'keyevent', 'KEYCODE_POWER'],
            capture_output=True,
            timeout=5,
        )
        log_signal.emit(1, 'Turn physical screen off for managed idle')
        return True
    except Exception as e:
        log_signal.emit(2, f'Failed to turn off the physical screen for managed idle: {e}')
        return False


def _config_event_path(config_dir):
    return os.path.join(os.path.dirname(__file__), 'config', config_dir, 'event.json')


def _read_config_events(config_dir):
    with open(_config_event_path(config_dir), encoding='utf-8') as f:
        return json.load(f)


def _seconds_since_midnight():
    now = datetime.now()
    return now.hour * 3600 + now.minute * 60 + now.second


def _event_disabled_now(event):
    current = _seconds_since_midnight()
    for period in event.get('disabled_time_range', []):
        if len(period) != 2:
            continue
        start = period[0][0] * 3600 + period[0][1] * 60 + period[0][2]
        end = period[1][0] * 3600 + period[1][1] * 60 + period[1][2]
        if start <= current <= end:
            return True
    return False


def _due_events(config_dir):
    now = time.time()
    events = [
        event for event in _read_config_events(config_dir)
        if event.get('enabled') and event.get('next_tick', 0) <= now and not _event_disabled_now(event)
    ]
    return sorted(events, key=lambda event: event.get('priority', 0))


def _next_enabled_tick(config_dirs):
    now = time.time()
    ticks = []
    for config_dir in config_dirs:
        try:
            for event in _read_config_events(config_dir):
                if not event.get('enabled') or _event_disabled_now(event):
                    continue
                tick = int(event.get('next_tick', 0))
                if tick > now:
                    ticks.append(tick)
                elif tick > 0:
                    ticks.append(int(now))
        except Exception:
            continue
    return min(ticks) if ticks else None


def _find_due_config(config_dirs, start_idx):
    if not config_dirs:
        return None, None, start_idx
    for offset in range(len(config_dirs)):
        idx = (start_idx + offset) % len(config_dirs)
        config_dir = config_dirs[idx]
        try:
            events = _due_events(config_dir)
        except Exception:
            events = []
        if events:
            return config_dir, events[0], idx
    return None, None, start_idx


def _config_package(config_dir, log_signal):
    try:
        from core.config.default_config import STATIC_DEFAULT_CONFIG
        static_config = json.loads(STATIC_DEFAULT_CONFIG)
        config_path = os.path.join(os.path.dirname(__file__), 'config', config_dir, 'config.json')
        with open(config_path, encoding='utf-8') as f:
            server = json.load(f).get('server')
        return static_config.get('package_name', {}).get(server)
    except Exception as e:
        log_signal.emit(2, f'Failed to read the managed configuration package: {config_dir}: {e}')
        return None


def _managed_config_packages(config_dirs, log_signal):
    packages = []
    for config_dir in config_dirs:
        package = _config_package(config_dir, log_signal)
        if package and package not in packages:
            packages.append(package)
    return packages


def _virtual_foreground_package(baas, log_signal):
    try:
        return _display_client(baas).get_foreground_package()
    except Exception as e:
        log_signal.emit(2, f'Failed to read the foreground package for managed idle: {e}')
        return 'unknown'


def _force_stop_game(baas, log_signal, extra_packages=None):
    packages = [baas.package_name]
    for package in extra_packages or []:
        if package and package not in packages:
            packages.append(package)

    ok = True
    for _ in range(3):
        foreground = _virtual_foreground_package(baas, log_signal)
        if foreground not in ('unknown', 'com.android.launcher') and foreground not in packages:
            packages.append(foreground)

        for package in packages:
            try:
                subprocess.run(
                    [adb_executable(), '-s', baas.serial, 'shell', 'am', 'force-stop', package],
                    capture_output=True,
                    timeout=8,
                )
                log_signal.emit(1, f'Exit game package for managed idle: {package}')
            except Exception as e:
                ok = False
                log_signal.emit(2, f'Failed to exit the game for managed idle: {package}: {e}')

        time.sleep(0.5)
        foreground = _virtual_foreground_package(baas, log_signal)
        if foreground in ('unknown', 'com.android.launcher'):
            break
    return ok


def _stop_game_on_config_switch(previous, target, log_signal):
    previous_session = (previous.serial, previous.package_name, previous.config.screenshot_method)
    target_session = (target.serial, target.package_name, target.config.screenshot_method)
    if previous_session == target_session:
        return
    subprocess.run(
        [adb_executable(), '-s', previous.serial, 'shell', 'am', 'force-stop', previous.package_name],
        capture_output=True,
        timeout=8,
        check=True,
    )
    log_signal.emit(1, f'Exit previous game before configuration switch: {previous.package_name}')


def _command_loop(baas, config_set, log_signal, button_signal, update_signal):
    worker = None
    worker_lock = threading.Lock()

    def worker_alive():
        return worker is not None and worker.is_alive()

    def ensure_config(config_dir, allow_worker=False):
        nonlocal baas, config_set
        if not config_dir:
            config_dir = os.path.basename(config_set.config_dir)
        if worker_alive() and not allow_worker:
            log_signal.emit(2, 'A task is already running; stop it first')
            return False
        current_dir = os.path.basename(config_set.config_dir)
        if config_dir != current_dir:
            try:
                from core.config.config_set import ConfigSet
                log_signal.emit(1, f'Persistent mode switching configuration: {current_dir} -> {config_dir}')
                new_config_set = ConfigSet(config_dir=config_dir)
                new_baas = baas.__class__(new_config_set, log_signal, button_signal, update_signal, None)
                new_baas.set_ocr(baas.ocr)
                if not new_baas.init_all_data():
                    log_signal.emit(3, f'Failed to initialize configuration {config_dir}')
                    return False
                _stop_game_on_config_switch(baas, new_baas, log_signal)
                baas = new_baas
                config_set = new_config_set
                _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))
            except Exception as e:
                log_signal.emit(3, f'Failed to switch configuration: {e}')
                return False
        if getattr(baas.config, 'screenshot_method', '') not in DISPLAY_METHODS:
            log_signal.emit(3, f'Configuration {config_dir} does not use a virtual display backend')
            return False
        return ensure_virtual_display_package()

    def ensure_virtual_display_package():
        try:
            cli = _display_client(baas)
            current_package = cli.get_foreground_package()
            log_signal.emit(1, f'Virtual display foreground package: {current_package}')
            if current_package != baas.package_name:
                log_signal.emit(
                    1,
                    f'Launch target package on current virtual display: {baas.package_name}'
                )
                cli.launch_app(baas.package_name, baas.activity_name)
                time.sleep(1)
            _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))
            return True
        except Exception as e:
            log_signal.emit(3, f'Failed to check the virtual display package: {e}')
            return False

    def start_job(name, label, current_task, action):
        nonlocal worker

        def _target():
            try:
                log_signal.emit(1, f'--- Persistent mode: {label} started ---')
                _emit_status({'running': True, 'currentTask': current_task})
                action()
                log_signal.emit(1, f'--- Persistent mode: {label} finished ---')
            except Exception as e:
                log_signal.emit(3, f'Persistent mode task failed: {e}')
            finally:
                _emit_status({'running': False, 'currentTask': None})
                if name == 'baas-managed':
                    _emit_managed_mode()

        with worker_lock:
            if worker_alive():
                log_signal.emit(2, 'A task is already running; stop it first')
                return False
            baas.flag_run = True
            worker = threading.Thread(target=_target, name=name, daemon=True)
            worker.start()
            return True

    def run_with_task_options(task, options=None):
        previous = _apply_task_options(baas, options)
        try:
            return baas.solve(task)
        finally:
            _restore_task_options(baas, previous)

    def run_one_scheduled_event(config_dir, event_hint):
        if not ensure_config(config_dir, allow_worker=True):
            return False
        _wake_unlock_and_turn_screen_off(baas, config_set, log_signal)
        if not ensure_virtual_display_package():
            return False

        baas.flag_run = True
        if not baas.solve('restart'):
            log_signal.emit(3, f'Managed mode failed to initialize the main page: {config_dir}')
            return False

        next_task = baas.scheduler.heartbeat()
        if not next_task:
            return True

        event_label = event_hint.get('event_name', next_task['current_task']) if event_hint else next_task['current_task']
        _emit_status({
            'running': True,
            'currentTask': f'{config_dir} · {event_label}',
            'managedMode': {
                'running': True,
                'configs': managed_configs,
                'currentConfig': config_dir,
                'nextWakeAt': None,
                'phase': 'running',
            }
        })
        baas.task_finish_to_main_page = True
        baas.daily_config_refresh()
        if time.time() - baas.last_refresh_u2_time > 10800:
            if not baas.solve('refresh_uiautomator2'):
                return False
        baas.genScheduleLog(next_task)

        task_with_log_info = []
        for task in next_task['pre_task']:
            task_with_log_info.append((task, 'pre_task'))
        task_with_log_info.append((next_task['current_task'], 'current_task'))
        for task in next_task['post_task']:
            task_with_log_info.append((task, 'post_task'))

        current_task_next_time = 0
        for task, task_type in task_with_log_info:
            if not baas.flag_run:
                return False
            current_task = task_type == 'current_task'
            log_signal.emit(1, f'{task_type}: [ {task} ] start')
            if current_task:
                baas.next_time = 0
            if not baas.solve(task):
                return False
            if current_task:
                current_task_next_time = baas.next_time

        if baas.flag_run:
            next_tick = baas.scheduler.systole(next_task['current_task'], current_task_next_time)
            log_signal.emit(1, next_task['current_task'] + ' next_time : ' + str(next_tick))

        if baas.task_finish_to_main_page and baas.flag_run:
            log_signal.emit(1, 'managed event finished, return to main page')
            baas.to_main_page()
            baas.main_page_update_data()
            baas.task_finish_to_main_page = False
        return baas.flag_run

    def enter_managed_idle(config_dirs):
        _force_stop_game(baas, log_signal, _managed_config_packages(config_dirs, log_signal))
        _turn_physical_screen_off(baas.serial, log_signal)

    managed_configs = []

    def managed_loop(config_dirs):
        nonlocal managed_configs
        managed_configs = list(config_dirs)
        cursor = 0
        idle_low_power = False
        log_signal.emit(1, f'--- Managed mode started: {managed_configs} ---')
        _emit_managed_mode(running=True, configs=managed_configs, phase='starting')
        while baas.flag_run:
            config_dir, event, idx = _find_due_config(managed_configs, cursor)
            if config_dir:
                idle_low_power = False
                log_signal.emit(1, f'Managed mode processing configuration: {config_dir}')
                while baas.flag_run:
                    try:
                        due = _due_events(config_dir)
                    except Exception:
                        due = []
                    if not due:
                        break
                    event = due[0]
                    event_name = event.get('event_name', event.get('func_name', 'Scheduled tasks'))
                    log_signal.emit(1, f'Managed mode executing: {config_dir} · {event_name}')
                    ok = run_one_scheduled_event(config_dir, event)
                    if not ok:
                        log_signal.emit(3, 'Managed task failed; stopping managed mode')
                        baas.flag_run = False
                        break
                cursor = (idx + 1) % len(managed_configs)
                continue

            next_wake = _next_enabled_tick(managed_configs)
            _emit_status({
                'running': True,
                'currentTask': 'Managed idle',
                'managedMode': {
                    'running': True,
                    'configs': managed_configs,
                    'currentConfig': None,
                    'nextWakeAt': next_wake,
                    'phase': 'idle',
                }
            })
            if not idle_low_power:
                log_signal.emit(1, 'No managed tasks are due; exit the game and turn off the physical screen for idle')
                enter_managed_idle(managed_configs)
                idle_low_power = True

            wait_time = 30
            if next_wake:
                wait_time = max(1, min(30, int(next_wake - time.time())))
            for _ in range(wait_time):
                if not baas.flag_run:
                    break
                time.sleep(1)

        log_signal.emit(1, '--- Managed mode finished ---')

    log_signal.emit(1, '--- Persistent mode: virtual display started; waiting for task commands ---')
    _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log_signal.emit(2, f'Ignoring invalid command: {raw}')
            continue

        cmd = msg.get('cmd')
        if cmd == 'start_scheduler':
            if ensure_config(msg.get('configDir')):
                start_job('baas-scheduler', 'Scheduled tasks', None, baas.thread_starter)
        elif cmd == 'start_managed':
            configs = msg.get('configs') or []
            configs = [str(config).strip() for config in configs if str(config).strip()]
            configs = list(dict.fromkeys(configs))
            if not configs:
                log_signal.emit(2, 'No configurations selected for managed mode')
                continue
            _emit_status({
                'running': True,
                'currentTask': 'Managed idle',
                'managedMode': {
                    'running': True,
                    'configs': configs,
                    'currentConfig': None,
                    'nextWakeAt': None,
                    'phase': 'starting',
                }
            })
            if not start_job('baas-managed', 'Managed mode', 'Managed idle', lambda: managed_loop(configs)):
                _emit_managed_mode()
        elif cmd == 'run_task':
            task = msg.get('task', '').strip()
            options = msg.get('options') or {}
            if task and ensure_config(msg.get('configDir')):
                start_job(f'baas-task-{task}', f'One-shot task: {task}', task, lambda: run_with_task_options(task, options))
        elif cmd == 'stop_task':
            if worker_alive():
                log_signal.emit(1, 'Stop request sent for the current task')
                baas.flag_run = False
            else:
                log_signal.emit(1, 'No task is currently running')
            _emit_status({'running': False, 'currentTask': None})
            _emit_managed_mode()
        elif cmd == 'virtual_status':
            _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))
        elif cmd == 'wake_unlock_screen_off':
            _wake_unlock_and_turn_screen_off(baas, config_set, log_signal)
        elif cmd == 'shutdown_virtual_display':
            if worker_alive():
                log_signal.emit(2, 'A task is running; stop it before closing the virtual display')
                continue
            log_signal.emit(1, '--- Persistent mode: closing the virtual display ---')
            if baas.config.screenshot_method == 'virtual_scrcpy':
                _send_power_key(baas.serial, log_signal)
            _emit_virtual_display(_virtual_display_data(baas, config_set, running=False))
            _cleanup_virtual_scrcpy()
            return
        else:
            log_signal.emit(2, f'Unknown command: {cmd}')

# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    config_dir = os.environ.get('BAAS_CONFIG_DIR', 'default')
    persistent = os.environ.get('BAAS_PERSISTENT', '') == '1'

    log_signal    = _LogSignal()
    button_signal = _ButtonSignal()
    update_signal = _UpdateSignal()

    # 检查 config 目录
    cfg_path = os.path.join(os.path.dirname(__file__), 'config', config_dir)
    if not os.path.exists(os.path.join(cfg_path, 'config.json')):
        log_signal.emit(3, f'config/{config_dir}/config.json does not exist; create the configuration first')
        sys.exit(1)

    try:
        from main import Main
        from core.Baas_thread import Baas_thread
        from core.config.config_set import ConfigSet
        from core.config.default_config import STATIC_DEFAULT_CONFIG
    except ImportError as e:
        log_signal.emit(3, f'Failed to import BAAS: {e}')
        log_signal.emit(3, 'Ensure dependencies are installed: pip install -r requirements-linux.txt')
        sys.exit(1)

    # 用最新的 default_config 刷新 config/static.json（GUI 启动时由 window.py 完成，WebUI 需手动触发）
    try:
        static_json_path = os.path.join(os.path.dirname(__file__), 'config', 'static.json')
        with open(static_json_path, 'w', encoding='utf-8') as _f:
            _f.write(STATIC_DEFAULT_CONFIG)
    except Exception as e:
        log_signal.emit(2, f'Failed to refresh static.json (event data may be outdated): {e}')

    log_signal.emit(1, '--- BAAS WebUI Runner started ---')

    try:
        import json as _json
        _cfg_file = os.path.join(cfg_path, 'config.json')
        _server_raw = 'CN'
        try:
            with open(_cfg_file, encoding='utf-8') as _f:
                _server_raw = _json.load(_f).get('server', 'CN')
        except Exception:
            pass
        _cn_to_server = {
            '官服': 'CN', 'B服': 'CN',
            '日服': 'JP', '日服PC端': 'JP',
            '国际服': 'Global', '国际服青少年': 'Global',
            '韩国ONE': 'Global', 'Steam国际服': 'Global',
        }
        _server = _cn_to_server.get(_server_raw, _server_raw)
        _server_lang = {
            'CN':     ['zh-cn', 'en-us'],
            'JP':     ['ja-jp', 'en-us'],
            'Global': ['en-us', 'ko-kr', 'zh-tw', 'ja-jp'],
        }
        if persistent:
            ocr_needed = ['zh-cn', 'en-us', 'ja-jp', 'ko-kr', 'zh-tw']
        else:
            ocr_needed = _server_lang.get(_server, ['zh-cn', 'en-us'])
        log_signal.emit(1, f'Server: {_server}; loading OCR languages: {ocr_needed}')
        main_instance = Main(logger_signal=log_signal, ocr_needed=ocr_needed)
    except Exception as e:
        log_signal.emit(3, f'OCR initialization failed: {e}')
        sys.exit(1)

    try:
        config_set = ConfigSet(config_dir=config_dir)
    except Exception as e:
        log_signal.emit(3, f'Failed to load configuration: {e}')
        sys.exit(1)

    baas = Baas_thread(config_set, log_signal, button_signal, update_signal, None)
    baas.set_ocr(main_instance.ocr)

    if not baas.init_all_data():
        log_signal.emit(3, 'Initialization failed; check the device connection and configuration')
        sys.exit(1)

    if persistent:
        _start_resource_reporter(config_set)
        _launch_game_on_virtual_display(baas, log_signal)
        _command_loop(baas, config_set, log_signal, button_signal, update_signal)
        return

    # 一次性任务模式：BAAS_TASK 指定任务名，执行完即退出
    one_shot = os.environ.get('BAAS_TASK', '').strip()
    if one_shot:
        log_signal.emit(1, f'--- One-shot task mode: {one_shot} ---')
        try:
            task_options = json.loads(os.environ.get('BAAS_TASK_OPTIONS', '{}'))
        except json.JSONDecodeError:
            task_options = {}
        previous = _apply_task_options(baas, task_options)
        try:
            baas.solve(one_shot)
        finally:
            _restore_task_options(baas, previous)
        log_signal.emit(1, f'--- Task finished: {one_shot} ---')
        return

    # 定期汇报资源数据
    _start_resource_reporter(config_set)

    baas.thread_starter()

if __name__ == '__main__':
    main()
