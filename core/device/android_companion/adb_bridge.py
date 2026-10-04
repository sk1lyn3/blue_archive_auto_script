#!/usr/bin/env python3
"""Host-side adb client for the BAAS Android Companion POC."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import shlex
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


APP_ID = "moe.baas.companion"
COMMAND_ACTIVITY = f"{APP_ID}/.CommandActivity"
RESULT_PATH = "files/last_command_result.json"
DEFAULT_CAPTURE_PATH = "/sdcard/Download/baas_companion_frame.png"


class CompanionClient:
    def __init__(
        self,
        serial: str | None = None,
        adb: str = "adb",
        adb_server_socket: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.serial = serial
        self.adb = adb
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self._deadline: float | None = None
        self.env = os.environ.copy()
        if adb_server_socket:
            self.env["ADB_SERVER_SOCKET"] = adb_server_socket

    def command(self, cmd: str, **extras: Any) -> dict[str, Any]:
        self._deadline = time.monotonic() + self.timeout
        try:
            return self._command(cmd, **extras)
        except (subprocess.TimeoutExpired, TimeoutError) as e:
            return {"ok": False, "cmd": cmd, "error": f"Command timed out: {e}"}
        finally:
            self._deadline = None

    def _command(self, cmd: str, **extras: Any) -> dict[str, Any]:
        request_id = uuid4().hex
        self._shell(["run-as", APP_ID, "rm", "-f", RESULT_PATH], check=False)

        am_args = [
            "am",
            "start",
            "-W",
            "-n",
            COMMAND_ACTIVITY,
            "--es",
            "cmd",
            cmd,
            "--es",
            "requestId",
            request_id,
        ]
        for key, value in extras.items():
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                am_args.extend(["--ez", key, "true" if value else "false"])
            elif isinstance(value, int):
                am_args.extend(["--ei", key, str(value)])
            else:
                am_args.extend(["--es", key, str(value)])

        self._shell(am_args)
        return self._wait_result(request_id)

    def pull(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._adb(["pull", remote, str(local)])

    def _wait_result(self, request_id: str) -> dict[str, Any]:
        deadline = self._deadline or (time.monotonic() + self.timeout)
        last_raw = ""
        while time.monotonic() < deadline:
            proc = self._shell(["run-as", APP_ID, "cat", RESULT_PATH], check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                last_raw = proc.stdout
                try:
                    result = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "Invalid JSON result", "raw": proc.stdout}
                if result.get("requestId") == request_id and result.get("state") != "started":
                    return result
            time.sleep(0.2)
        return {"ok": False, "error": "Timed out waiting for command result", "raw": last_raw}

    def _shell(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._adb(["shell", shlex.join(args)], check=check)

    def _adb(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [self.adb]
        if self.serial:
            cmd.extend(["-s", self.serial])
        cmd.extend(args)
        timeout = self.timeout
        if self._deadline is not None:
            timeout = self._deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("Command deadline exceeded")
        return subprocess.run(
            cmd,
            check=check,
            timeout=timeout,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
