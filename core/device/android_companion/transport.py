#!/usr/bin/env python3
"""Persistent adb-forwarded client for the native Android companion middleware."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
from typing import Any
from uuid import uuid4

from .adb_bridge import APP_ID, CompanionClient

SOCKET_NAME = "baas-companion-v1"


class MiddlewareError(RuntimeError):
    def __init__(self, message, *, reply=None):
        super().__init__(message)
        self.reply = reply or {}


class MiddlewareClient:
    """One ordered connection; callers must explicitly stop displays they own.

    Failed or timed-out commands are never silently replayed. Reconnect explicitly
    and inspect status, since a lost reply does not prove a command was unapplied.
    """
    def __init__(self, serial: str, adb: str = "adb", adb_server_socket: str | None = None,
                 timeout: float = 20, socket_host: str | None = None) -> None:
        self.adb = CompanionClient(serial, adb, adb_server_socket, timeout)
        server = self.adb.env.get("ADB_SERVER_SOCKET", "")
        self.host = socket_host or (server.rsplit(":", 1)[0][4:] if server.startswith("tcp:") else "127.0.0.1")
        self.timeout = timeout
        self.port: int | None = None
        self.socket: socket.socket | None = None
        self.lock = threading.RLock()

    def connect(self, bootstrap: bool = True) -> MiddlewareClient:
        with self.lock:
            if self.socket is not None:
                return self
            if self.port is None:
                self.port = int(self.adb._adb(["forward", "tcp:0", f"localabstract:{SOCKET_NAME}"]).stdout.strip())
            try:
                self.socket = socket.create_connection((self.host, self.port), self.timeout)
                self.command("ping")
                return self
            except (OSError, MiddlewareError):
                self._disconnect_socket()
                if not bootstrap:
                    self.close()
                    raise
            # Cold bootstrap uses the same wake/dismiss sequence as BAAS. A live
            # daemon connection avoids opening a physical-screen Activity at all.
            try:
                installed = self.adb._shell(["pm", "path", APP_ID], check=False)
                if installed.returncode != 0 and installed.stderr.strip():
                    raise MiddlewareError(f"Cannot check Android Companion installation: {installed.stderr.strip()}")
                if not any(line.startswith("package:") for line in installed.stdout.splitlines()):
                    raise MiddlewareError(
                        f"Android Companion ({APP_ID}) is not installed on device {self.adb.serial}. "
                        "Install the Companion APK on this device, start Shizuku, "
                        "and grant Companion permission before retrying."
                    )
                self.adb._shell(["input", "keyevent", "224"])
                self.adb._shell(["wm", "dismiss-keyguard"], check=False)
                result = self.adb.command("ping")
                if not result.get("ok"):
                    raise MiddlewareError(f"Bootstrap failed: {result}")
                self.socket = socket.create_connection((self.host, self.port), self.timeout)
                self.command("ping")
                return self
            except Exception:
                self.close()
                raise

    def _disconnect_socket(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def close(self) -> None:
        with self.lock:
            self._disconnect_socket()
            if self.port is not None:
                try:
                    self.adb._adb(["forward", "--remove", f"tcp:{self.port}"], check=False)
                finally:
                    self.port = None

    def __enter__(self) -> MiddlewareClient:
        return self.connect()

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _receive(self, count: int, deadline: float) -> bytes:
        assert self.socket is not None
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Response deadline exceeded")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(min(count - len(data), 65536))
            if not chunk:
                raise MiddlewareError("Connection closed during response")
            data.extend(chunk)
        return bytes(data)

    def request(self, cmd: str, *, rpc_timeout: float | None = None, **extras: Any) -> tuple[dict[str, Any], bytes]:
        with self.lock:
            if self.socket is None:
                raise MiddlewareError("Connect before sending commands")
            request_id = uuid4().hex
            request = {**extras, "cmd": cmd, "apiVersion": 1, "requestId": request_id}
            body = json.dumps(request).encode()
            if len(body) > 65536:
                raise ValueError("Request exceeds 64 KiB")
            timeout = self.timeout if rpc_timeout is None else rpc_timeout
            if timeout <= 0: raise TimeoutError("Request deadline exceeded")
            deadline = time.monotonic() + timeout
            try:
                self.socket.settimeout(timeout)
                self.socket.sendall(struct.pack(">I", len(body)) + body)
                length, = struct.unpack(">I", self._receive(4, deadline))
                if not 0 < length <= 65536:
                    raise MiddlewareError("Invalid response header length")
                reply = json.loads(self._receive(length, deadline))
                if not isinstance(reply, dict) or reply.get("requestId") != request_id or reply.get("apiVersion") != 1:
                    raise MiddlewareError("Response request ID or API version mismatch")
                size = reply.get("payloadBytes", 0)
                if type(size) is not int or not 0 <= size <= 128 * 1024 * 1024:
                    raise MiddlewareError("Invalid payload length")
                payload = self._receive(size, deadline)
            except (OSError, ValueError, MiddlewareError):
                self._disconnect_socket()
                raise
            if not reply.get("ok"):
                raise MiddlewareError(str(reply.get("error", reply)), reply=reply)
            return reply, payload

    def command(self, cmd: str, **extras: Any) -> dict[str, Any]:
        reply, payload = self.request(cmd, **extras)
        if payload:
            raise MiddlewareError("Use screenshot() to receive frame bytes")
        return reply

    def screenshot(self, timeout: float | None = None) -> tuple[dict[str, Any], bytes]:
        reply, png = self.request("screenshot", rpc_timeout=timeout)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            self._disconnect_socket()
            raise MiddlewareError("Invalid PNG payload")
        return reply, png

    def wait_frame(self, timeout: float = 30, after_frame_id: int = 0,
                   nonblack: bool = True, max_age_ms: int = 1000) -> tuple[dict[str, Any], bytes]:
        deadline = time.monotonic() + timeout
        last: Any = "No frame received"
        while time.monotonic() < deadline:
            try:
                metadata, png = self.screenshot(min(self.timeout, max(0, deadline - time.monotonic())))
                last = metadata
                if (metadata["frameId"] > after_frame_id and metadata["ageMs"] <= max_age_ms
                        and (not nonblack or metadata["nonblackFraction"] > 0.01)):
                    return metadata, png
            except MiddlewareError as e:
                last = str(e)
                if self.socket is None:
                    raise
            time.sleep(0.1)
        raise TimeoutError(f"No suitable frame within {timeout}s: {last}")
