import time
from datetime import datetime


def _get_current_package(self):
    if self.config.screenshot_method == "virtual_scrcpy":
        from core.device.virtual_scrcpy_client import VirtualScrcpyClient
        return VirtualScrcpyClient.get_instance(self.serial).get_foreground_package()
    return self.u2.app_current()['package']


def implement(self):
    if not self.is_android_device:
        return True
    cur_package = _get_current_package(self)
    if cur_package != self.package_name:
        self.logger.warning("APP NOT RUNNING current package: " + cur_package)
        start(self)
        return True
    self.logger.info("CHECK RESTART")
    if check_need_restart(self):
        self.logger.info("current package: " + cur_package)
        self.logger.info("--STOP CURRENT BLUE ARCHIVE--")
        self.u2.app_stop(self.package_name)
        time.sleep(2)
        start(self)
        return True
    return True


def start(self):
    self.logger.info("-- START BLUE ARCHIVE --")
    activity_name = self.activity_name
    if self.server == 'CN' and self.config.screenshot_method != 'virtual_scrcpy':
        activity_name = None
    if self.config.screenshot_method == "virtual_scrcpy":
        from core.device.virtual_scrcpy_client import VirtualScrcpyClient
        VirtualScrcpyClient.get_instance(self.serial).launch_app(self.package_name, activity_name)
    else:
        self.u2.app_start(self.package_name, activity_name)
    self.to_main_page()


def check_need_restart(self):
    now = datetime.now()
    if self.server == 'CN':
        if abs(time.time() - datetime(year=now.year, month=now.month, day=now.day, hour=4).timestamp()) <= 60:
            return True
    elif self.server == 'Global' or self.server == 'JP':
        if abs(time.time() - datetime(year=now.year, month=now.month, day=now.day, hour=3).timestamp()) <= 60:
            return True
    return False
