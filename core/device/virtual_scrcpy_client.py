"""
VirtualScrcpyClient：虚拟屏截图 + scrcpy 控制方案

- 用 scrcpy-server v4.0 创建 Android 虚拟 display（new_display=1280x720）
- 低帧率视频流 + 缓存帧，screenshot() 极快（~0.2ms）
- 操作后设 dirty flag，下次 screenshot() 才等新帧，契合 BAAS 的截图-操作-截图节奏
- 虚拟屏控制走 scrcpy control socket；启动应用、查 display 仍通过 adb shell
"""
import os
import re
import subprocess
import threading
import time

from adbutils import adb
from core.device.scrcpy.core import Client


DISPLAY_SIZE = "1280x720/240"
STREAM_FPS = 15
FRAME_TIMEOUT = 3.0


def _get_server_v4_path() -> str:
    base = os.path.abspath(os.path.dirname(__file__))
    for _ in range(2):
        base = os.path.dirname(base)
    return os.path.join(base, "src", "scrcpy_bin", "scrcpy-server-v4.jar")


class VirtualScrcpyError(Exception):
    pass


class VirtualScrcpyClient:
    _instances: dict = {}
    _instances_lock = threading.Lock()

    def __init__(self, serial: str):
        self.serial = serial
        self._server_path = _get_server_v4_path()
        self._display_id: int | None = None
        self._lock = threading.RLock()
        self._dirty = threading.Event()
        self._dirty.set()
        self._client: Client | None = None
        self._start_session()

    @classmethod
    def get_instance(cls, serial: str) -> "VirtualScrcpyClient":
        with cls._instances_lock:
            if serial not in cls._instances:
                cls._instances[serial] = VirtualScrcpyClient(serial)
            else:
                cls._instances[serial].ensure_ready()
            return cls._instances[serial]

    @classmethod
    def release_instance(cls, serial: str):
        with cls._instances_lock:
            inst = cls._instances.pop(serial, None)
        if inst:
            inst.stop()

    @classmethod
    def recreate_instance(cls, serial: str) -> "VirtualScrcpyClient":
        with cls._instances_lock:
            inst = cls._instances.get(serial)
            if inst is None:
                inst = VirtualScrcpyClient(serial)
                cls._instances[serial] = inst
                return inst
        inst.recreate()
        return inst

    @classmethod
    def release_all(cls):
        with cls._instances_lock:
            instances = list(cls._instances.values())
            cls._instances.clear()
        for inst in instances:
            inst.stop()

    def stop(self):
        with self._lock:
            self._stop_session()

    # ── 截图 ─────────────────────────────────────────────────────────────

    def screenshot(self):
        with self._lock:
            self.ensure_ready()
            frame_age = time.time() - self._client.last_frame_time
            if self._dirty.is_set() or frame_age > 1.0 / STREAM_FPS:
                if not self._wait_new_frame(FRAME_TIMEOUT) and self._client.last_frame is None:
                    self.recreate()
                self._dirty.clear()
            frame = self._client.last_frame
            if frame is None:
                raise VirtualScrcpyError("No frame available")
            return frame.copy()

    def get_foreground_package(self) -> str:
        """查虚拟屏上的前台包名"""
        with self._lock:
            self.ensure_ready()
            display_id = self._display_id
            out = subprocess.check_output(
                ["adb", "-s", self.serial, "shell", "dumpsys", "activity", "activities"],
                text=True
            )
            in_display = False
            for line in out.split('\n'):
                if f"Display #{display_id} " in line:
                    in_display = True
                elif in_display and re.match(r'\s*Display #\d+', line):
                    in_display = False
                if in_display and re.search(r'Hist\s+#0:', line):
                    m = re.search(r'([\w.]+)/[\w.$]+', line)
                    if m:
                        return m.group(1)
            return "unknown"

    def launch_app(self, package: str, activity: str = None):
        """在虚拟屏上启动应用"""
        with self._lock:
            self.ensure_ready()
            if activity:
                cmd = f"am start --display {self._display_id} -n {package}/{activity}"
            else:
                cmd = f"am start --display {self._display_id} -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p {package}"
            self._adb(cmd)

    def turn_screen_off(self):
        """通过当前 scrcpy control session 关闭物理屏显示。"""
        from core.device.scrcpy import const
        with self._lock:
            self.ensure_ready()
            self._client.control.set_screen_power_mode(const.POWER_MODE_OFF)

    # ── 控制（走 scrcpy control socket，无 adb 子进程开销）────────────────

    def click(self, x, y):
        from core.device.scrcpy import const
        with self._lock:
            self.ensure_ready()
            ctrl = self._client.control
            ctrl.touch(x, y, const.ACTION_DOWN)
            time.sleep(0.05)
            ctrl.touch(x, y, const.ACTION_UP)
            self._dirty.set()

    def swipe(self, x1, y1, x2, y2, duration):
        """duration 单位：秒"""
        from core.device.scrcpy import const
        with self._lock:
            self.ensure_ready()
            ctrl = self._client.control
            dist = max(1, ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
            steps = max(1, int(dist / 5))
            delay = max(0.001, duration / steps)
            ctrl.swipe(x1, y1, x2, y2,
                       move_step_length=5,
                       move_steps_delay=delay)
            self._dirty.set()

    def long_click(self, x, y, duration=1.0):
        from core.device.scrcpy import const
        with self._lock:
            self.ensure_ready()
            ctrl = self._client.control
            ctrl.touch(x, y, const.ACTION_DOWN)
            time.sleep(duration)
            ctrl.touch(x, y, const.ACTION_UP)
            self._dirty.set()

    def pinch(self, cx, cy, spread_start, spread_end, steps=30):
        """双指捏合/张开。spread_start > spread_end = 缩小，反之放大。"""
        from core.device.scrcpy import const
        with self._lock:
            self.ensure_ready()
            ctrl = self._client.control
            delay = 0.016
            ctrl.touch(cx - spread_start, cy, const.ACTION_DOWN, touch_id=1)
            ctrl.touch(cx + spread_start, cy, const.ACTION_DOWN, touch_id=2)
            for i in range(1, steps + 1):
                frac = i / steps
                s = spread_start + int((spread_end - spread_start) * frac)
                ctrl.touch(cx - s, cy, const.ACTION_MOVE, touch_id=1)
                ctrl.touch(cx + s, cy, const.ACTION_MOVE, touch_id=2)
                time.sleep(delay)
            ctrl.touch(cx - spread_end, cy, const.ACTION_UP, touch_id=1)
            ctrl.touch(cx + spread_end, cy, const.ACTION_UP, touch_id=2)
            self._dirty.set()

    # ── 内部 ─────────────────────────────────────────────────────────────

    @property
    def display_id(self) -> int | None:
        return self._display_id

    def ensure_ready(self):
        with self._lock:
            if self._session_alive():
                return
            self._restart_session()

    def recreate(self):
        with self._lock:
            self._restart_session()

    def _start_session(self):
        device = adb.device(serial=self.serial)
        displays_before = self._list_display_ids()

        self._client = Client(device, max_width=1280, max_fps=STREAM_FPS)
        self._patch_deploy(device)

        try:
            self._client.start(daemon_threaded=True)
        except Exception as e:
            self._client = None
            raise VirtualScrcpyError(f"Failed to start virtual scrcpy: {e}")

        if not self._wait_new_frame(timeout=10.0):
            self._stop_session()
            raise VirtualScrcpyError("No frame received from virtual scrcpy")
        self._dirty.clear()

        self._display_id = self._find_new_display(displays_before)
        if self._display_id is None:
            self._stop_session()
            raise VirtualScrcpyError("Virtual display not found after start")

    def _stop_session(self):
        client = self._client
        self._client = None
        self._display_id = None
        self._dirty.set()
        if client:
            try:
                client.stop()
            except Exception:
                pass

    def _restart_session(self):
        self._stop_session()
        time.sleep(0.2)
        self._start_session()

    def _session_alive(self) -> bool:
        client = self._client
        if client is None or not client.is_alive() or client.control_socket is None:
            return False
        if self._display_id is None:
            return False
        try:
            if self._display_id not in self._list_display_ids():
                return False
        except Exception:
            return False
        if client.last_frame is None:
            return False
        return True

    def _adb(self, cmd: str):
        subprocess.run(["adb", "-s", self.serial, "shell", cmd], capture_output=True)

    def _list_display_ids(self) -> set:
        out = subprocess.check_output(
            ["adb", "-s", self.serial, "shell", "dumpsys", "display"], text=True
        )
        return set(int(x) for x in re.findall(r"mDisplayId=(\d+)", out))

    def _find_new_display(self, displays_before: set) -> int | None:
        # Wait up to 5s for the new display to appear
        deadline = time.time() + 5.0
        while time.time() < deadline:
            displays_after = self._list_display_ids()
            new_displays = displays_after - displays_before - {0}
            if new_displays:
                display_id = max(new_displays)
                return display_id
            time.sleep(0.2)
        # Fallback: use max non-zero display
        displays_after = self._list_display_ids()
        virtual = sorted(displays_after - {0})
        return virtual[-1] if virtual else None

    def _wait_new_frame(self, timeout: float):
        deadline = time.time() + timeout
        now = time.time()
        while time.time() < deadline:
            if self._client is not None and self._client.last_frame is not None and self._client.last_frame_time > now:
                return True
            time.sleep(0.01)
        return False

    def _patch_deploy(self, device):
        server_path = self._server_path
        remote_jar = "/data/local/tmp/scrcpy-server-v4.jar"

        def patched_deploy(self_inner):
            device.sync.push(server_path, remote_jar)
            commands = [
                f"CLASSPATH={remote_jar}",
                "app_process", "/", "com.genymobile.scrcpy.Server",
                "4.0",
                "log_level=info",
                "max_size=1280",
                f"max_fps={STREAM_FPS}",
                "video_bit_rate=8000000",
                "video_codec=h264",
                "video_encoder=OMX.google.h264.encoder",
                "tunnel_forward=true",
                "send_frame_meta=false",
                "control=true",
                "audio=false",
                "show_touches=false",
                "stay_awake=false",
                "power_off_on_close=false",
                "clipboard_autosync=false",
                f"new_display={DISPLAY_SIZE}",
            ]
            self_inner._Client__server_stream = device.shell(commands, stream=True)
            self_inner._Client__server_stream.read(10)

        Client._Client__deploy_server = patched_deploy
