from core.device.virtual_scrcpy_client import VirtualScrcpyClient


class VirtualScrcpyControl:
    def __init__(self, conn):
        self.cli = VirtualScrcpyClient.get_instance(conn.serial)

    def click(self, x, y):
        self.cli.click(x, y)

    def swipe(self, x1, y1, x2, y2, duration):
        self.cli.swipe(x1, y1, x2, y2, duration)

    def long_click(self, x, y, duration):
        self.cli.long_click(x, y, duration)
