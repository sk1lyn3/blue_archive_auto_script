from core.device.android_companion.client import AndroidCompanionClient


class AndroidCompanionScreenshot:
    def __init__(self, connection):
        if connection.config.control_method != 'android_companion':
            raise ValueError('Use android_companion for both control and screenshots')
        self.client = AndroidCompanionClient.get_instance(connection.serial)

    def screenshot(self):
        return self.client.screenshot()
