import time
import cv2
import os
from core.device.scrcpy_client import ScrcpyClient

_last_save_time = 0


class ScrcpyScreenshot:
    def __init__(self, conn):
        self.serial = conn.serial
        self.cli = ScrcpyClient.get_instance(self.serial)

    def screenshot(self):
        global _last_save_time
        frame = self.cli.screenshot()
        now = time.time()
        if now - _last_save_time > 5.0:
            _last_save_time = now
            project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
            cv2.imwrite(os.path.join(project_dir, "baas_scrcpy_frame.png"), frame)
        return frame
