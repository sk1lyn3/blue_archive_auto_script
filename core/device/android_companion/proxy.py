"""Compatibility surface for existing tasks which call self.u2 directly."""
import cv2
from PIL import Image
from adbutils import adb

from .client import AndroidCompanionClient


class AndroidCompanionProxy:
    def __init__(self, serial):
        self.client = AndroidCompanionClient.get_instance(serial)
        self._adb_device = adb.device(serial)

    def __call__(self, *args, **kwargs):
        return self

    def screenshot(self):
        return Image.fromarray(cv2.cvtColor(self.client.screenshot(), cv2.COLOR_BGR2RGB))

    def click(self, x, y):
        self.client.click(x, y)

    def swipe(self, x1, y1, x2, y2, duration=None):
        self.client.swipe(x1, y1, x2, y2, duration)

    def app_start(self, package, activity=None):
        self.client.launch_app(package, activity)

    def app_stop(self, package):
        self.client.app_stop(package)

    def app_current(self):
        return {'package': self.client.get_foreground_package()}

    def pinch_in(self, percent=100, steps=10):
        self.client.pinch(640, 360, 300, round(300 * (1 - percent / 100)), steps)

    def pinch_out(self, percent=100, steps=10):
        self.client.pinch(640, 360, round(300 * (1 - percent / 100)), 300, steps)
