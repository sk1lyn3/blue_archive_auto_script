"""
VirtualU2Proxy — 透明代理 self.u2，将输入/截图操作重定向到虚拟 display。
非输入操作（app_start/stop/current、uiautomator 内部调用等）穿透到真实 u2。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from core.device.virtual_scrcpy_client import VirtualScrcpyClient


class _VirtualGestureProxy:
    """self.u2() 返回的对象，负责 pinch 等手势操作。"""

    def __init__(self, vc: VirtualScrcpyClient):
        self._vc = vc

    def pinch_in(self, percent: int = 100, steps: int = 10):
        """双指捏合（缩小）：通过 scrcpy control socket 发送真正的多点触控。"""
        spread = int(640 * percent / 200)
        self._vc.pinch(640, 360, spread_start=spread, spread_end=0, steps=steps)

    def pinch_out(self, percent: int = 100, steps: int = 10):
        """双指张开（放大）。"""
        spread = int(640 * percent / 200)
        self._vc.pinch(640, 360, spread_start=0, spread_end=spread, steps=steps)


class VirtualU2Proxy:
    """
    套在 self.u2 上的透明代理。
    只拦截：screenshot / swipe / click（输入 + 截图），其余全部穿透到真实 u2。
    """

    _OVERRIDE = frozenset(["screenshot", "swipe", "click"])

    def __init__(self, real_u2, serial: str):
        from core.device.virtual_scrcpy_client import VirtualScrcpyClient
        object.__setattr__(self, "_real", real_u2)
        object.__setattr__(self, "_vc", VirtualScrcpyClient.get_instance(serial))

    # ── 透传非拦截属性 ─────────────────────────────────────────────────────

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_real"), name)

    # ── self.u2() → 手势代理 ────────────────────────────────────────────────

    def __call__(self, *args, **kwargs):
        return _VirtualGestureProxy(object.__getattribute__(self, "_vc"))

    # ── 截图：用 H.264 流最新帧，转为 PIL-compatible RGB ───────────────────

    def screenshot(self):
        from PIL import Image
        vc = object.__getattribute__(self, "_vc")
        frame = vc._client.last_frame  # BGR24 numpy array
        if frame is None:
            raise RuntimeError("VirtualU2Proxy: no frame from virtual display yet")
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    # ── swipe：u2 duration 单位是秒，直接透传 ────────────────────────────────

    def swipe(self, x1, y1, x2, y2, duration=None):
        vc = object.__getattribute__(self, "_vc")
        vc.swipe(int(x1), int(y1), int(x2), int(y2), duration or 0.3)

    # ── click：转发到虚拟 display ──────────────────────────────────────────

    def click(self, x, y):
        vc = object.__getattribute__(self, "_vc")
        vc.click(int(x), int(y))
