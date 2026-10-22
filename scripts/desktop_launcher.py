"""Start the user's local app and verified-save companion without terminal windows."""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
URL = 'http://127.0.0.1:8000'

def env_values(path: Path):
    from dotenv import dotenv_values
    return dict(dotenv_values(path)) if path.exists() else {}

def put_env(path: Path, key: str, value: str):
    from dotenv import set_key
    # python-dotenv replaces one key without displaying private values.
    # Windows sync/scanning tools can briefly lock the atomic replacement.
    for attempt in range(5):
        try:
            set_key(str(path), key, value, quote_mode='always')
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.1 * (2 ** attempt))

def prepare_config(root: Path = ROOT, folder: Path | None = None):
    config = root / '.env'
    if not config.exists():
        shutil.copyfile(root / '.env.example', config)
    backend = env_values(config)
    if backend.get('ENVIRONMENT') == 'production':
        raise RuntimeError('Use the hosted website for this production configuration; the local launcher will not alter it.')
    companion = root / 'companion' / '.env'
    local = env_values(companion)
    existing_url = local.get('BACKEND_URL') or URL
    if urlsplit(existing_url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise RuntimeError('Your companion already uses a hosted backend. Its connection was preserved; open your hosted website instead.')
    expected = (backend.get('COMPANION_TOKEN_SHA256') or '').strip()
    token = (local.get('COMPANION_TOKEN') or '').strip()
    if expected and (not token or hashlib.sha256(token.encode()).hexdigest() != expected):
        raise RuntimeError('An existing companion pairing needs review. Existing secrets were preserved.')
    if not token:
        token = secrets.token_urlsafe(48)
    destination = Path(local.get('TRADES_FOLDER') or folder or Path.home() / 'Documents' / 'PaperTrading' / 'Trades').expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if not companion.exists():
        companion.touch()
    put_env(companion, 'BACKEND_URL', URL)
    put_env(companion, 'COMPANION_TOKEN', token)
    put_env(companion, 'TRADES_FOLDER', destination.as_posix())
    put_env(companion, 'POLL_SECONDS', local.get('POLL_SECONDS') or '10')
    put_env(config, 'COMPANION_TOKEN_SHA256', hashlib.sha256(token.encode()).hexdigest())
    return destination

def health():
    try:
        with urllib.request.urlopen(URL + '/api/health', timeout=2) as response:
            data = json.load(response)
            return response.status == 200 and data.get('mode') == 'PAPER' and data.get('live_orders_supported') is False
    except (OSError, ValueError, urllib.error.URLError):
        return False

def windows_api():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetProcessTimes.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL
    return api

def process_token(pid: int, terminate: bool = False, expected: int | None = None):
    api = windows_api()
    handle = api.OpenProcess(0x1000 | (0x0001 if terminate else 0), False, pid)
    if not handle:
        return None
    try:
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not api.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        token = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        # Exact process creation time prevents stopping an unrelated reused PID.
        if terminate and token == expected:
            if not api.TerminateProcess(handle, 0):
                raise OSError('Could not stop the recorded application process.')
        return token
    finally:
        api.CloseHandle(handle)

def start_process(role: str, arguments: list[str]):
    state = ROOT / '.desktop'
    state.mkdir(exist_ok=True)
    note = state / f'{role}.json'
    if note.exists():
        previous = json.loads(note.read_text())
        if process_token(previous['pid']) == previous.get('creation'):
            return previous['pid']
    environment = os.environ.copy()
    # This launcher is the real local app, never a temporary QA database.
    from dotenv import dotenv_values
    configured = {k: v for k, v in dotenv_values(ROOT / '.env').items() if v is not None}
    for key in ('DATA_DIR', 'DATABASE_URL'):
        if key not in configured:
            environment.pop(key, None)
    environment.update(configured)
    with (state / f'{role}.log').open('ab') as log:
        process = subprocess.Popen(arguments, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    token = process_token(process.pid)
    if token is None:
        raise RuntimeError(f'The {role} process could not be started. See .desktop/{role}.log.')
    note.write_text(json.dumps({'pid': process.pid, 'creation': token}), encoding='utf-8')
    return process.pid

def stop_processes():
    for role in ('companion', 'backend'):
        note = ROOT / '.desktop' / f'{role}.json'
        if not note.exists():
            continue
        previous = json.loads(note.read_text())
        process_token(previous['pid'], terminate=True, expected=previous['creation'])
        note.unlink()
    print('The recorded local app services have stopped. Saved records were retained.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-open', action='store_true', help='Start services without opening a browser')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--folder', type=Path, help='Local report destination for initial setup')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This desktop launcher is for Windows.')
    if args.stop:
        stop_processes()
        return
    python = ROOT / '.venv' / 'Scripts' / 'python.exe'
    if not python.exists() or not (ROOT / 'frontend' / 'dist' / 'index.html').exists():
        raise RuntimeError('Run Setup-Windows.cmd once to install the app.')
    folder = prepare_config(folder=args.folder)
    if not health():
        start_process('backend', [str(python), '-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend',
                                  '--host', '127.0.0.1', '--port', '8000', '--workers', '1'])
        deadline = time.monotonic() + 35
        while not health():
            if time.monotonic() > deadline:
                raise RuntimeError('The backend did not become ready. See .desktop/backend.log; another app may use port 8000.')
            time.sleep(.5)
    start_process('companion', [str(python), str(ROOT / 'companion' / 'companion.py')])
    print('Personal Options Trader is running in paper-only mode.')
    print(f'Open: {URL}')
    print(f'Verified Excel reports save to: {folder}')
    if not args.no_open:
        os.startfile(URL)

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Could not complete setup: {error}', file=sys.stderr)
        raise SystemExit(1)
