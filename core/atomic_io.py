"""Atomic JSON file replacement for config and schedule persistence."""
import json
import os
import shutil
import tempfile


def write_json(path, data, *, indent=2, ensure_ascii=False, backup=True):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=ensure_ascii, indent=indent)
            stream.flush()
            os.fsync(stream.fileno())
        if backup and os.path.exists(path):
            # Keep the live path intact until the new file is ready to replace it.
            shutil.copy2(path, path + ".bak")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
