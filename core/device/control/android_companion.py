from core.device.android_companion.client import AndroidCompanionClient


class AndroidCompanionControl:
    def __init__(self, connection):
        if connection.config.screenshot_method != 'android_companion':
            raise ValueError('Use android_companion for both control and screenshots')
        self.client = AndroidCompanionClient.get_instance(connection.serial)

    def click(self, x, y):
        self.client.click(x, y)

    def swipe(self, x1, y1, x2, y2, duration):
        self.client.swipe(x1, y1, x2, y2, duration)

    def long_click(self, x, y, duration):
        self.client.long_click(x, y, duration)
