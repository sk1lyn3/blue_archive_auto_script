"""
Monkey-patches for virtual_scrcpy compatibility.
Call apply() once before initializing any BAAS classes (done in _webui_runner.py).

This file exists so upstream core files can stay unmodified, keeping git pull clean.
When upstream changes a patched method, the wrap still calls the original — conflicts
are isolated to this file only.
"""
import time
from core.device.backend_compat import get_backend_compat


def apply():
    _patch_control_factory()
    _patch_screenshot_factory()
    _patch_connection()
    _patch_color()
    _patch_baas_thread()
    from core.device.swipe_compat import apply as apply_swipe_compat
    apply_swipe_compat()


# ── Device factories ──────────────────────────────────────────────────────────

def _patch_control_factory():
    from core.device.Control import Control
    from core.device.control.virtual_scrcpy import VirtualScrcpyControl

    _orig = Control.init_control_instance

    def _init_control_instance(self):
        if getattr(self.config, 'control_method', '') == 'virtual_scrcpy':
            self.method = 'virtual_scrcpy'
            self.logger.info("Control method : virtual_scrcpy")
            self.control_instance = VirtualScrcpyControl(self.connection)
            return
        _orig(self)

    Control.init_control_instance = _init_control_instance


def _patch_screenshot_factory():
    from core.device.Screenshot import Screenshot
    from core.device.screenshot.virtual_scrcpy import VirtualScrcpyScreenshot

    _orig = Screenshot.init_screenshot_instance

    def _init_screenshot_instance(self):
        if getattr(self.config, 'screenshot_method', '') == 'virtual_scrcpy':
            self.method = 'virtual_scrcpy'
            self.logger.info("Screenshot method : virtual_scrcpy")
            self.screenshot_instance = VirtualScrcpyScreenshot(self.connection)
            return
        _orig(self)

    Screenshot.init_screenshot_instance = _init_screenshot_instance


# ── Connection ────────────────────────────────────────────────────────────────

def _patch_connection():
    from core.device.connection import Connection

    _orig = Connection.get_current_package

    def _get_current_package(self):
        if getattr(getattr(self, 'config', None), 'screenshot_method', '') == 'virtual_scrcpy':
            from core.device.virtual_scrcpy_client import VirtualScrcpyClient
            return VirtualScrcpyClient.get_instance(self.serial).get_foreground_package()
        return _orig(self)

    Connection.get_current_package = _get_current_package


# ── Color tolerance ───────────────────────────────────────────────────────────

def _patch_color():
    import core.color as _m

    _orig = _m.rgb_in_range

    def _rgb_in_range(self, x, y, r_min, r_max, g_min, g_max, b_min, b_max,
                      check_nearby=False, nearby_range=1):
        method = getattr(getattr(self, 'config', None), 'screenshot_method', '')
        tol = get_backend_compat(method).rgb_tolerance
        if tol:
            r_min = max(0, r_min - tol); r_max = min(255, r_max + tol)
            g_min = max(0, g_min - tol); g_max = min(255, g_max + tol)
            b_min = max(0, b_min - tol); b_max = min(255, b_max + tol)
        return _orig(self, x, y, r_min, r_max, g_min, g_max, b_min, b_max,
                     check_nearby, nearby_range)

    _m.rgb_in_range = _rgb_in_range


# ── Baas_thread ───────────────────────────────────────────────────────────────

def _patch_baas_thread():
    from core.Baas_thread import Baas_thread

    # _get_android_device_resolution: wrap u2 with VirtualU2Proxy, skip check_atx
    _orig_get_res = Baas_thread._get_android_device_resolution

    def _get_android_device_resolution(self):
        if getattr(getattr(self, 'config', None), 'screenshot_method', '') == 'virtual_scrcpy':
            from core.device.virtual_u2_proxy import VirtualU2Proxy
            from core.device.uiautomator2_client import U2Client
            self.u2_client = U2Client.get_instance(self.serial)
            self.u2 = self.u2_client.get_connection()
            self.last_refresh_u2_time = time.time()
            self.u2 = VirtualU2Proxy(self.u2, self.serial)
            return (1280, 720)
        return _orig_get_res(self)

    Baas_thread._get_android_device_resolution = _get_android_device_resolution

    # init_all_data: upstream doesn't call refresh_common/hard_tasks after init
    _orig_init_all = Baas_thread.init_all_data

    def _init_all_data(self):
        result = _orig_init_all(self)
        if result:
            self.refresh_common_tasks()
            self.refresh_hard_tasks()
        return result

    Baas_thread.init_all_data = _init_all_data
