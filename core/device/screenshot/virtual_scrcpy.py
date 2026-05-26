import time
import cv2
from core.device.virtual_scrcpy_client import VirtualScrcpyClient

_last_save_time = 0


class VirtualScrcpyScreenshot:
    def __init__(self, conn):
        self.cli = VirtualScrcpyClient.get_instance(conn.serial)

    def screenshot(self):
        global _last_save_time
        frame = self.cli.screenshot()
        now = time.time()
        if now - _last_save_time > 5.0:
            _last_save_time = now
            cv2.imwrite("/tmp/baas_last_frame.png", frame)
        return frame
