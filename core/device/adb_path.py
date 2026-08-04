import os
import shutil
from typing import Optional


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def adb_executable() -> str:
    configured = os.environ.get('BAAS_ADB_PATH', '').strip().strip('"')
    if configured:
        return configured

    exe_name = 'adb.exe' if os.name == 'nt' else 'adb'
    candidates = [
        os.path.join(_project_root(), 'platform-tools', exe_name),
        os.path.join(os.path.dirname(_project_root()), 'platform-tools', exe_name),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path

    return shutil.which('adb') or 'adb'


def adb_directory() -> Optional[str]:
    path = adb_executable()
    directory = os.path.dirname(path)
    if directory and os.path.exists(path):
        return directory
    return None
