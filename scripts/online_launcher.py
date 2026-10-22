"""Open the hosted app and run its separate, outbound-only Excel companion."""
import argparse
from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ROLE = 'online-companion'

# Reuse the local launcher's exact Windows process-creation-time protection.
_spec = importlib.util.spec_from_file_location('online_desktop_helpers', Path(__file__).with_name('desktop_launcher.py'))
_desktop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_desktop)
process_token = _desktop.process_token


@dataclass
class OnlineConfig:
    website: str
    backend: str
    folder: Path
    interval: int
    token: str = field(repr=False)

    def fingerprint(self):
        value = [self.backend, str(self.folder), self.interval, self.token]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def https_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.strip('/')):
        raise ValueError('Online addresses must be HTTPS origins without credentials, paths, queries or fragments.')
    parsed.port  # Reject malformed port values before any request.
    return value.rstrip('/')


def load_config(root=ROOT):
    website_file, companion_file = root / '.env.online', root / 'companion' / '.env.online'
    if not website_file.is_file() or not companion_file.is_file():
        raise RuntimeError('The online app is not configured yet. Its separate online settings must be prepared first.')
    website_values = dotenv_values(website_file, interpolate=False)
    values = dotenv_values(companion_file, interpolate=False)
    website = https_origin(website_values.get('PUBLIC_WEBSITE_URL') or '')
    backend = https_origin(values.get('BACKEND_URL') or '')
    token = (values.get('COMPANION_TOKEN') or '').strip()
    if len(token) < 32:
        raise ValueError('The online companion needs its own paired token with at least 32 characters.')
    destination = values.get('TRADES_FOLDER') or ''
    if not destination or not Path(destination).expanduser().is_absolute():
        raise ValueError('Choose a full, separate folder path for online Excel reports.')
    folder = Path(destination).expanduser().resolve()
    try:
        interval = int(values.get('POLL_SECONDS') or '10')
    except ValueError:
        raise ValueError('The online companion polling interval must be a whole number.') from None
    if not 5 <= interval <= 3600:
        raise ValueError('The online companion polling interval must be between 5 and 3600 seconds.')
    return OnlineConfig(website, backend, folder, interval, token)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def paper_health(backend):
    """Probe without credentials, redirects, or weakened certificate checks."""
    try:
        https_origin(backend)
        opener = urllib.request.build_opener(NoRedirects())
        with opener.open(backend + '/api/health', timeout=90) as response:
            data = json.load(response)
            return (response.status == 200 and isinstance(data, dict) and data.get('ok') is True
                    and data.get('mode') == 'PAPER' and data.get('live_orders_supported') is False)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def bind_report_folder(config):
    """An existing local/offline manifest must never be used against the cloud."""
    state = config.folder / '.companion'
    binding = state / 'online-backend.txt'
    if binding.exists():
        if binding.read_text(encoding='utf-8').strip() != config.backend:
            raise RuntimeError('This report folder belongs to a different online backend. Its files were preserved.')
    elif (state / 'manifest.sqlite3').exists():
        raise RuntimeError('This folder already has another companion journal. Choose a separate online report folder.')
    else:
        state.mkdir(parents=True, exist_ok=True)
        with binding.open('x', encoding='utf-8') as target:
            target.write(config.backend + '\n')


def run_companion(config):
    # Do not call companion.main(): it loads the existing local companion .env.
    import httpx
    spec = importlib.util.spec_from_file_location('hosted_report_companion', ROOT / 'companion' / 'companion.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not paper_health(config.backend):
        raise RuntimeError('The hosted paper-only service could not be verified. No companion credential was sent.')
    bind_report_folder(config)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    service = module.Companion(config.backend, config.token, config.folder)
    logging.info('Online companion is watching for completed reports. Save folder: %s', config.folder)
    try:
        while not stop.is_set():
            try:
                service.poll_once()
            except (httpx.HTTPError, OSError) as error:
                logging.warning('Online backend or folder unavailable (%s); reports remain queued.', type(error).__name__)
            stop.wait(config.interval)
    finally:
        service.close()


def start_online(config):
    state = ROOT / '.desktop'
    state.mkdir(exist_ok=True)
    note = state / f'{ROLE}.json'
    fingerprint = config.fingerprint()
    if note.exists():
        previous = json.loads(note.read_text(encoding='utf-8'))
        if not valid_process_note(previous):
            raise RuntimeError('The online companion process record needs review. No process was changed.')
        if process_token(previous['pid']) == previous['creation']:
            if previous.get('configuration') != fingerprint:
                raise RuntimeError('The online companion is running with different settings. Stop it before changing its connection.')
            return previous['pid']
    python = ROOT / '.venv' / 'Scripts' / 'python.exe'
    if not python.is_file():
        raise RuntimeError('Run Setup-Windows.cmd once to install the app dependencies.')
    environment = os.environ.copy()
    for key in ('BACKEND_URL', 'COMPANION_TOKEN', 'TRADES_FOLDER', 'POLL_SECONDS', 'PUBLIC_WEBSITE_URL', 'DATABASE_URL', 'DATA_DIR'):
        environment.pop(key, None)
    environment['PYTHONUNBUFFERED'] = '1'
    with (state / f'{ROLE}.log').open('ab') as output:
        process = subprocess.Popen([str(python), str(ROOT / 'scripts' / 'online_launcher.py'), '--run-companion'],
            cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    creation = process_token(process.pid)
    if creation is None:
        raise RuntimeError('The online companion did not start. See .desktop/online-companion.log.')
    note.write_text(json.dumps({'pid': process.pid, 'creation': creation, 'configuration': fingerprint}), encoding='utf-8')
    return process.pid


def valid_process_note(note):
    return (isinstance(note, dict) and type(note.get('pid')) is int and note['pid'] > 0
            and type(note.get('creation')) is int and note['creation'] > 0)


def stop_online():
    note = ROOT / '.desktop' / f'{ROLE}.json'
    if note.exists():
        previous = json.loads(note.read_text(encoding='utf-8'))
        if not valid_process_note(previous):
            raise RuntimeError('The online companion process record needs review. No process was changed.')
        process_token(previous['pid'], terminate=True, expected=previous['creation'])
        note.unlink()
    print('The online report companion has stopped. Local app services and saved records were retained.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--run-companion', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != 'nt':
        raise RuntimeError('This online desktop launcher is for Windows.')
    if args.stop:
        stop_online()
        return
    config = load_config(ROOT)
    if args.run_companion:
        run_companion(config)
        return
    if not paper_health(config.backend):
        raise RuntimeError('The hosted paper-only service could not be verified. Existing local services were retained.')
    bind_report_folder(config)
    start_online(config)
    print('The online paper-trading app and its separate Excel companion are ready.')
    print(f'Open: {config.website}')
    print(f'Online Excel reports save to: {config.folder}')
    if not args.no_open:
        os.startfile(config.website)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Could not open the online app: {error}', file=sys.stderr)
        raise SystemExit(1)
