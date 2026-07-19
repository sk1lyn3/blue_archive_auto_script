"""
BAAS 子进程运行器 —— 由 webui_server.py 启动，不要直接运行
stdout 输出 JSON Lines，webui_server.py 解析后转发给 WebSocket 客户端
"""
import json
import os
import atexit
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

# 在任何 BAAS 类被导入前注入 virtual_scrcpy 兼容补丁
import core.virtual_scrcpy_compat
core.virtual_scrcpy_compat.apply()


def _cleanup_virtual_scrcpy():
    try:
        from core.device.virtual_scrcpy_client import VirtualScrcpyClient
        VirtualScrcpyClient.release_all()
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


def _virtual_display_data(baas=None, config_set=None, running=True) -> dict:
    data = {'running': running, 'starting': False}
    try:
        if config_set is not None:
            data['configDir'] = os.path.basename(config_set.config_dir)
        if running and baas is not None and getattr(baas.config, 'screenshot_method', '') == 'virtual_scrcpy':
            from core.device.virtual_scrcpy_client import VirtualScrcpyClient
            cli = VirtualScrcpyClient.get_instance(baas.serial)
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
    if getattr(baas.config, 'screenshot_method', '') != 'virtual_scrcpy':
        return
    from core.device.virtual_scrcpy_client import VirtualScrcpyClient

    activity_name = baas.activity_name
    cli = VirtualScrcpyClient.get_instance(baas.serial)
    log_signal.emit(1, 'Launch game on virtual display')
    cli.launch_app(baas.package_name, activity_name)


def _wake_unlock_and_turn_screen_off(baas, config_set, log_signal):
    if getattr(baas.config, 'screenshot_method', '') != 'virtual_scrcpy':
        log_signal.emit(3, '当前配置未使用 virtual_scrcpy')
        return False
    try:
        serial = baas.serial
        subprocess.run(
            ['adb', '-s', serial, 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP'],
            capture_output=True,
            timeout=5,
        )
        subprocess.run(
            ['adb', '-s', serial, 'shell', 'wm', 'dismiss-keyguard'],
            capture_output=True,
            timeout=5,
        )
        time.sleep(0.2)

        from core.device.virtual_scrcpy_client import VirtualScrcpyClient
        cli = VirtualScrcpyClient.get_instance(serial)
        cli.turn_screen_off()
        log_signal.emit(1, 'Wake/unlock device and turn physical screen off')
        _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))
        return True
    except Exception as e:
        log_signal.emit(3, f'唤醒解锁并关屏失败: {e}')
        return False


def _send_power_key(serial, log_signal):
    try:
        subprocess.run(
            ['adb', '-s', serial, 'shell', 'input', 'keyevent', 'KEYCODE_POWER'],
            capture_output=True,
            timeout=5,
        )
        log_signal.emit(1, 'Send power key before virtual display shutdown')
    except Exception as e:
        log_signal.emit(2, f'发送电源键失败: {e}')


def _command_loop(baas, config_set, log_signal, button_signal, update_signal):
    worker = None
    worker_lock = threading.Lock()

    def worker_alive():
        return worker is not None and worker.is_alive()

    def ensure_config(config_dir):
        nonlocal baas, config_set
        if not config_dir:
            config_dir = os.path.basename(config_set.config_dir)
        if worker_alive():
            log_signal.emit(2, '已有任务在运行，请先停止当前任务')
            return False
        current_dir = os.path.basename(config_set.config_dir)
        if config_dir != current_dir:
            try:
                from core.config.config_set import ConfigSet
                log_signal.emit(1, f'常驻模式切换配置: {current_dir} -> {config_dir}')
                new_config_set = ConfigSet(config_dir=config_dir)
                new_baas = baas.__class__(new_config_set, log_signal, button_signal, update_signal, None)
                new_baas.set_ocr(baas.ocr)
                if not new_baas.init_all_data():
                    log_signal.emit(3, f'配置 {config_dir} 初始化失败')
                    return False
                baas = new_baas
                config_set = new_config_set
                _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))
            except Exception as e:
                log_signal.emit(3, f'切换配置失败: {e}')
                return False
        if getattr(baas.config, 'screenshot_method', '') != 'virtual_scrcpy':
            log_signal.emit(3, f'配置 {config_dir} 未使用 virtual_scrcpy')
            return False
        return ensure_virtual_display_package()

    def ensure_virtual_display_package():
        try:
            from core.device.virtual_scrcpy_client import VirtualScrcpyClient
            cli = VirtualScrcpyClient.get_instance(baas.serial)
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
            log_signal.emit(3, f'虚拟屏幕包名检查失败: {e}')
            return False

    def start_job(name, label, current_task, action):
        nonlocal worker

        def _target():
            try:
                log_signal.emit(1, f'--- 常驻模式: {label}启动 ---')
                _emit_status({'running': True, 'currentTask': current_task})
                action()
                log_signal.emit(1, f'--- 常驻模式: {label}结束 ---')
            finally:
                _emit_status({'running': False, 'currentTask': None})

        with worker_lock:
            if worker_alive():
                log_signal.emit(2, '已有任务在运行，请先停止当前任务')
                return False
            baas.flag_run = True
            worker = threading.Thread(target=_target, name=name, daemon=True)
            worker.start()
            return True

    log_signal.emit(1, '--- 常驻模式: 虚拟屏幕已启动，等待任务命令 ---')
    _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log_signal.emit(2, f'忽略无效命令: {raw}')
            continue

        cmd = msg.get('cmd')
        if cmd == 'start_scheduler':
            if ensure_config(msg.get('configDir')):
                start_job('baas-scheduler', '调度任务', None, baas.thread_starter)
        elif cmd == 'run_task':
            task = msg.get('task', '').strip()
            if task and ensure_config(msg.get('configDir')):
                start_job(f'baas-task-{task}', f'单次任务: {task}', task, lambda: baas.solve(task))
        elif cmd == 'stop_task':
            if worker_alive():
                log_signal.emit(1, '停止当前任务请求已发送')
                baas.flag_run = False
            else:
                log_signal.emit(1, '当前没有正在运行的任务')
            _emit_status({'running': False, 'currentTask': None})
        elif cmd == 'virtual_status':
            _emit_virtual_display(_virtual_display_data(baas, config_set, running=True))
        elif cmd == 'wake_unlock_screen_off':
            _wake_unlock_and_turn_screen_off(baas, config_set, log_signal)
        elif cmd == 'shutdown_virtual_display':
            if worker_alive():
                log_signal.emit(2, '任务运行中，请先停止任务再关闭虚拟屏幕')
                continue
            log_signal.emit(1, '--- 常驻模式: 关闭虚拟屏幕 ---')
            _send_power_key(baas.serial, log_signal)
            _emit_virtual_display(_virtual_display_data(baas, config_set, running=False))
            _cleanup_virtual_scrcpy()
            return
        else:
            log_signal.emit(2, f'未知命令: {cmd}')

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
        log_signal.emit(3, f'config/{config_dir}/config.json 不存在，请先创建配置')
        sys.exit(1)

    try:
        from main import Main
        from core.Baas_thread import Baas_thread
        from core.config.config_set import ConfigSet
        from core.config.default_config import STATIC_DEFAULT_CONFIG
    except ImportError as e:
        log_signal.emit(3, f'导入 BAAS 失败: {e}')
        log_signal.emit(3, '请确认依赖已安装: pip install -r requirements-linux.txt')
        sys.exit(1)

    # 用最新的 default_config 刷新 config/static.json（GUI 启动时由 window.py 完成，WebUI 需手动触发）
    try:
        static_json_path = os.path.join(os.path.dirname(__file__), 'config', 'static.json')
        with open(static_json_path, 'w', encoding='utf-8') as _f:
            _f.write(STATIC_DEFAULT_CONFIG)
    except Exception as e:
        log_signal.emit(2, f'刷新 static.json 失败（活动数据可能过旧）: {e}')

    log_signal.emit(1, '--- BAAS WebUI Runner 启动 ---')

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
        ocr_needed = _server_lang.get(_server, ['zh-cn', 'en-us'])
        log_signal.emit(1, f'服务器: {_server}，加载 OCR 语言: {ocr_needed}')
        main_instance = Main(logger_signal=log_signal, ocr_needed=ocr_needed)
    except Exception as e:
        log_signal.emit(3, f'OCR 初始化失败: {e}')
        sys.exit(1)

    try:
        config_set = ConfigSet(config_dir=config_dir)
    except Exception as e:
        log_signal.emit(3, f'配置加载失败: {e}')
        sys.exit(1)

    baas = Baas_thread(config_set, log_signal, button_signal, update_signal, None)
    baas.set_ocr(main_instance.ocr)

    if not baas.init_all_data():
        log_signal.emit(3, '初始化失败，请检查设备连接和配置')
        sys.exit(1)

    if persistent:
        _start_resource_reporter(config_set)
        _launch_game_on_virtual_display(baas, log_signal)
        _command_loop(baas, config_set, log_signal, button_signal, update_signal)
        return

    # 一次性任务模式：BAAS_TASK 指定任务名，执行完即退出
    one_shot = os.environ.get('BAAS_TASK', '').strip()
    if one_shot:
        log_signal.emit(1, f'--- 单次任务模式: {one_shot} ---')
        baas.solve(one_shot)
        log_signal.emit(1, f'--- 任务完成: {one_shot} ---')
        return

    # 定期汇报资源数据
    _start_resource_reporter(config_set)

    baas.thread_starter()

if __name__ == '__main__':
    main()
