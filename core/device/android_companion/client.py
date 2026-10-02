"""Shared native display session, independent of game tasks and Server state."""
import atexit
import os
import re
import threading
import time

import cv2
import numpy as np

from core.device.adb_path import adb_executable
from .transport import MiddlewareClient, MiddlewareError


class AndroidCompanionClient:
    _instances = {}
    _instances_lock = threading.RLock()

    def __init__(self, serial):
        self.serial = serial
        self.transport = MiddlewareClient(serial, adb=adb_executable(),
                                          adb_server_socket=os.environ.get('ADB_SERVER_SOCKET'))
        self.capture_transport = MiddlewareClient(serial, adb=adb_executable(),
                                                  adb_server_socket=os.environ.get('ADB_SERVER_SOCKET'))
        self.lock = threading.RLock()
        self.capture_lock = threading.RLock()
        self.frame_state_lock = threading.Lock()
        self.display_id = None
        self.frame_id = 0
        self.completed_ns = 0
        self.dirty = False
        self.last_metadata = None
        self.packages = set()
        self._closing = threading.Event()
        self._heartbeat = None
        self._heartbeat_error = None
        reused = False
        try:
            self.transport.connect()
            state = self._status()
            if state['displayId'] == -1:
                self.display_id = self.transport.command('start', width=1280, height=720, dpi=240)['displayId']
            else:
                if not state.get('alive'):
                    raise RuntimeError('Existing companion display is not alive')
                if (state.get('width'), state.get('height')) != (1280, 720):
                    raise RuntimeError('Existing companion display must be 1280x720')
                self.display_id = state['displayId']
                reused = True
            self.capture_transport.connect(bootstrap=False)
            if reused:
                # Establish frame state from the daemon without restarting its display.
                self.screenshot()
            self._heartbeat = threading.Thread(target=self._keepalive, daemon=True,
                                               name='android-companion-keepalive')
            self._heartbeat.start()
        except Exception:
            if reused:
                # Failed attachment closes our connections, preserving the existing display.
                self.display_id = None
            self.stop()
            raise

    @classmethod
    def get_instance(cls, serial):
        with cls._instances_lock:
            if serial not in cls._instances:
                cls._instances[serial] = cls(serial)
            return cls._instances[serial]

    @classmethod
    def release_all(cls):
        with cls._instances_lock:
            instances = list(cls._instances.values())
            cls._instances.clear()
        errors = []
        for instance in instances:
            try:
                instance.stop()
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError('; '.join(errors))

    def _status(self):
        if self.transport.socket is None:
            self.transport.connect(bootstrap=False)
        try:
            return self.transport.command('status')['state']
        except MiddlewareError:
            if self.transport.socket is not None:
                raise
            # Only read operations can be repeated after a lost connection.
            self.transport.connect(bootstrap=False)
            return self.transport.command('status')['state']

    def _ready(self):
        if self._closing.is_set():
            raise RuntimeError('Companion session is closed')
        if self.transport.socket is None or self._heartbeat_error is not None:
            if self._status()['displayId'] != self.display_id:
                raise RuntimeError('Companion display session changed')
            self._heartbeat_error = None

    def _keepalive(self):
        while not self._closing.wait(10):
            try:
                with self.lock:
                    if self._status()['displayId'] != self.display_id:
                        raise RuntimeError('Companion display session changed')
            except Exception as error:
                self._heartbeat_error = error

    def _input(self, command, **arguments):
        with self.lock:
            self._ready()
            reply = self.transport.command(command, **arguments)
            with self.frame_state_lock:
                self.completed_ns = reply['completedNs']
                self.dirty = True
            return reply

    @staticmethod
    def _point(x, y):
        return max(0, min(1279, round(x))), max(0, min(719, round(y)))

    def click(self, x, y):
        x, y = self._point(x, y)
        self._input('click', x=x, y=y)

    def swipe(self, x1, y1, x2, y2, duration=None):
        x1, y1 = self._point(x1, y1)
        x2, y2 = self._point(x2, y2)
        self._input('swipe', x1=x1, y1=y1, x2=x2, y2=y2,
                    duration=max(80, min(10000, round((duration or .3) * 1000))))

    def long_click(self, x, y, duration):
        self.swipe(x, y, x, y, duration)

    def pinch(self, cx, cy, spread_start, spread_end, steps=30):
        self._input('pinch', cx=round(cx), cy=round(cy), start=round(spread_start),
                    end=round(spread_end), duration=max(80, min(10000, steps * 16)))

    def key(self, key_code):
        self._input('key', keyCode=key_code)

    def launch_app(self, package, activity=None):
        with self.lock:
            self._input('launch', package=package, activity=activity or '')
            self.packages.add(package)

    def app_stop(self, package):
        with self.lock:
            self.transport.adb._shell(['am', 'force-stop', package])

    def turn_screen_off(self):
        with self.lock:
            self._ready()
            self.transport.command('wake')
            self.transport.command('screen', off=True)

    def get_foreground_package(self):
        with self.lock:
            text = self.transport.adb._shell(['dumpsys', 'activity', 'activities']).stdout
        in_display = False
        for line in text.splitlines():
            if f'Display #{self.display_id} ' in line:
                in_display = True
            elif in_display and re.match(r'\s*Display #\d+', line):
                in_display = False
            if in_display and re.search(r'Hist\s+#0:', line):
                match = re.search(r'([\w.]+)/[\w.$]+', line)
                if match:
                    return match.group(1)
        return 'unknown'

    def screenshot(self):
        # Input and frame transfer use different sockets and never share a long lock.
        with self.capture_lock:
            if self._closing.is_set():
                raise RuntimeError('Companion session is closed')
            with self.frame_state_lock:
                completed_ns = self.completed_ns
                min_timestamp = completed_ns if self.dirty else 0
                frame_id = self.frame_id
            deadline = time.monotonic() + 30
            while True:
                if self._closing.is_set():
                    raise RuntimeError('Companion session is closed')
                try:
                    if self.capture_transport.socket is None:
                        self.capture_transport.connect(bootstrap=False)
                    metadata, png = self.capture_transport.screenshot(
                        min(20, max(.01, deadline - time.monotonic())))
                    if metadata['displayId'] != self.display_id:
                        raise RuntimeError('Companion display session changed')
                    if not min_timestamp or (metadata['frameId'] > frame_id and
                                             metadata['timestampNs'] > min_timestamp):
                        break
                except MiddlewareError as error:
                    if self.capture_transport.socket is not None and 'No frame available yet' not in str(error):
                        raise
                if time.monotonic() >= deadline:
                    raise TimeoutError('No fresh companion frame after input')
                time.sleep(.1)
            image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape != (720, 1280, 3):
                raise RuntimeError('Invalid companion screenshot')
            with self.frame_state_lock:
                self.frame_id = metadata['frameId']
                self.last_metadata = metadata
                # A capture begun during input must not consume that input's fence.
                if self.completed_ns == completed_ns:
                    self.dirty = False
            return image

    def stop(self):
        self._closing.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=25)
        try:
            with self.lock:
                if self.display_id is not None:
                    state = self._status()
                    if state['displayId'] not in (-1, self.display_id):
                        raise RuntimeError('Refusing to stop a different companion display')
                    for package in self.packages:
                        self.app_stop(package)
                    if state['displayId'] == self.display_id:
                        self.transport.command('stop')
                    if self._status()['displayId'] != -1:
                        raise RuntimeError('Companion display cleanup failed')
                    self.display_id = None
        finally:
            try:
                with self.capture_lock:
                    self.capture_transport.close()
            finally:
                self.transport.close()


atexit.register(AndroidCompanionClient.release_all)
