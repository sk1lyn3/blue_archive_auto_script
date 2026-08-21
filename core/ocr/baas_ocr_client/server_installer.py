import sys, io

# ================================
# Check the std::in and std::out Status before
# dulwich-related crashes, for dulwich will
# connect to the io, while the io is unset
# by the built window app.

if sys.stdin is None:
    sys.stdin = io.TextIOWrapper(io.BytesIO())
    sys.stdout = io.TextIOWrapper(io.BytesIO())
# ================================

import shutil
import os
import json
from core.exception import OcrInternalError
from dulwich import porcelain
from dulwich.repo import Repo
import platform
import re

if sys.platform not in ['win32', 'linux', 'darwin']:
    raise Exception("Ocr Unsupported platform " + sys.platform)

OCR_SERVER_PREBUILD_URLS = [
    "https://github.com/pur1fying/BAAS_Cpp_prebuild.git",
    "https://gitee.com/pur1fy/baas_-cpp_prebuild.git",
    "https://v4.gh-proxy.org/https://github.com/pur1fying/BAAS_Cpp_prebuild.git",
    "https://v6.gh-proxy.org/https://github.com/pur1fying/BAAS_Cpp_prebuild.git",
    "https://cdn.gh-proxy.org/https://github.com/pur1fying/BAAS_Cpp_prebuild.git",
    "https://gh-proxy.org/https://github.com/pur1fying/BAAS_Cpp_prebuild.git",
    "https://gh.sevencdn.com/https://github.com/pur1fying/BAAS_Cpp_prebuild.git",
    "https://githubfast.com/pur1fying/BAAS_Cpp_prebuild.git",
]

SERVER_INSTALLER_DIR_PATH = os.path.dirname(os.path.abspath(__file__))
SERVER_BIN_DIR = os.path.join(SERVER_INSTALLER_DIR_PATH, 'bin')

branch = {
    'win32': {
        'amd64': 'windows-x64',
    },
    'linux': {
        'x86_64': 'linux-x64',
    },
    'darwin': {
        'arm64': 'macos-arm64',
        'x86_64': 'macos-x64',
    },
}
branch = branch[sys.platform]
arch = platform.machine().lower()
if arch not in branch:
    raise Exception("Unsupported machine architecture " + arch)
branch = branch[arch]


def should_skip_installer_managed_update():
    """Only the C++ installer's explicit, valid handoff marker disables I/O."""
    marker_path = os.path.join(SERVER_BIN_DIR, '.baas-installer-managed.json')
    try:
        with open(marker_path, encoding='utf-8') as marker_file:
            marker = json.load(marker_file)
        expected_executable = 'BAAS_ocr_server.exe' if sys.platform == 'win32' else 'BAAS_ocr_server'
        commit = marker.get('commit', '')
        if not (marker.get('schema_version') == 1 and
                marker.get('managed_by') == 'baas-installer' and
                marker.get('branch') == branch and
                isinstance(commit, str) and re.fullmatch(r'[0-9a-fA-F]{40}', commit) and
                os.path.isfile(os.path.join(SERVER_BIN_DIR, expected_executable))):
            return False
        git_dir = os.path.join(SERVER_BIN_DIR, '.git')
        if os.path.isdir(git_dir):
            return Repo(SERVER_BIN_DIR).head().decode('ascii').lower() == commit.lower()
        return True
    except Exception:
        return False


def check_git(logger):
    if should_skip_installer_managed_update():
        logger.info("OCR server was verified by the BAAS installer; skipping legacy network update.")
        return
    if not os.path.exists(SERVER_BIN_DIR + '/.git'):
        clone_repo(logger)
    else:
        logger.info("Ocr Server Update check.")
        try:
            repo = Repo(SERVER_BIN_DIR)
            # Get local SHA
            local_sha = repo.head().decode('ascii')
        except Exception:
            logger.warning("Git Repo corrupted, remove .git folder and reinstall.")
            shutil.rmtree(SERVER_BIN_DIR + '/.git')
            clone_repo(logger)
            return
        remote_url, remote_sha = get_remote_sha(logger)
        if remote_url is None:
            logger.warning("Ocr Server update check skipped; all remote sources are unavailable.")
            return

        logger.info(f"remote_sha: {remote_sha}")
        logger.info(f"local_sha : {local_sha}")

        if local_sha == remote_sha:
            logger.info("Ocr Server No updates available.")
        else:
            logger.info("Pulling updates from the remote repository...")
            # Reset the local repository to the state of the remote repository
            porcelain.reset(repo, mode='hard')
            # Pull the latest changes from the remote repository
            for i, url in enumerate(OCR_SERVER_PREBUILD_URLS, start=1):
                try:
                    porcelain.pull(repo, url, branch, protocol_version=0)
                    break
                except Exception as e:
                    if i == len(OCR_SERVER_PREBUILD_URLS):
                        raise OcrInternalError("Failed to update the BAAS_ocr_server. Please check your network")
                    logger.warning(f"Failed to update BAAS_ocr_server from {url}, trying next source... {i}")
                    logger.warning(e)
            updated_local_sha = repo.head().decode('ascii')
            if updated_local_sha == remote_sha:
                logger.info("Ocr Server Update success.")
            else:
                logger.warning("Failed to update the BAAS_ocr_server, please check your network.")


def clone_repo(logger):
    logger.info("Installing Ocr Server, please hang on...")
    for i, url in enumerate(OCR_SERVER_PREBUILD_URLS, start=1):
        try:
            porcelain.clone(url, SERVER_BIN_DIR, branch=branch)
            break
        except Exception as e:
            if i == len(OCR_SERVER_PREBUILD_URLS):
                raise OcrInternalError("Failed to install the BAAS_ocr_server. Please check your network")
            logger.warning(f"Failed to install BAAS_ocr_server from {url}, trying next source... {i}")
            logger.warning(e.__str__())
    logger.info("Ocr Server Install success.")


def get_remote_sha(logger):
    ref_name = b'refs/heads/' + branch.encode('ascii')
    for url in OCR_SERVER_PREBUILD_URLS:
        try:
            remote_refs = porcelain.ls_remote(url)
            remote_sha = remote_refs.get(ref_name)
            if remote_sha is None:
                logger.warning(f"Ocr Server source missing branch {branch}: {url}")
                continue
            return url, remote_sha.decode('ascii')
        except Exception as e:
            logger.warning(f"Failed to check BAAS_ocr_server source {url}: {e}")
    return None, None
