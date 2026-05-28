"""
BAAS 子进程运行器 —— 由 webui_server.py 启动，不要直接运行
stdout 输出 JSON Lines，webui_server.py 解析后转发给 WebSocket 客户端
"""
import json
import os
import sys
import time
from datetime import datetime

# 在任何 BAAS 类被导入前注入 virtual_scrcpy 兼容补丁
import core.virtual_scrcpy_compat
core.virtual_scrcpy_compat.apply()

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

# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    config_dir = os.environ.get('BAAS_CONFIG_DIR', 'default')

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
    except ImportError as e:
        log_signal.emit(3, f'导入 BAAS 失败: {e}')
        log_signal.emit(3, '请确认依赖已安装: pip install -r requirements-linux.txt')
        sys.exit(1)

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

    # 一次性任务模式：BAAS_TASK 指定任务名，执行完即退出
    one_shot = os.environ.get('BAAS_TASK', '').strip()
    if one_shot:
        log_signal.emit(1, f'--- 单次任务模式: {one_shot} ---')
        baas.solve(one_shot)
        log_signal.emit(1, f'--- 任务完成: {one_shot} ---')
        return

    # 定期汇报资源数据
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

    import threading
    threading.Thread(target=_resource_reporter, daemon=True).start()

    baas.thread_starter()

if __name__ == '__main__':
    main()
