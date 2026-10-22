"""Online launcher isolation checks; no live credentials, requests or processes."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location('online_launcher_tests', Path(__file__).resolve().parents[2] / 'scripts' / 'online_launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
TOKEN = 'synthetic-online-test-token-never-used'


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'online-launcher'
    (root / 'companion').mkdir(parents=True)
    (root / '.venv' / 'Scripts').mkdir(parents=True)
    (root / '.venv' / 'Scripts' / 'python.exe').touch()
    (root / '.env').write_text('DATABASE_URL=sqlite:///local-untouched.db\n', encoding='utf-8')
    (root / 'companion' / '.env').write_text('BACKEND_URL=http://127.0.0.1:8000\nCOMPANION_TOKEN=local-untouched\n', encoding='utf-8')
    (root / '.env.online').write_text('PUBLIC_WEBSITE_URL=https://paper-app.vercel.app\n', encoding='utf-8')
    folder = tmp_path / 'online-reports'
    (root / 'companion' / '.env.online').write_text(
        f'BACKEND_URL=https://paper-api.onrender.com\nCOMPANION_TOKEN={TOKEN}\nTRADES_FOLDER={folder.as_posix()}\nPOLL_SECONDS=10\n', encoding='utf-8')
    return root


def test_online_config_is_separate_and_ignores_inherited_values(project, monkeypatch):
    before = [(path, path.read_bytes()) for path in [project / '.env', project / 'companion' / '.env']]
    monkeypatch.setenv('BACKEND_URL', 'http://inherited-unsafe.test')
    monkeypatch.setenv('PUBLIC_WEBSITE_URL', 'http://inherited-unsafe.test')
    config = launcher.load_config(project)
    assert config.backend == 'https://paper-api.onrender.com'
    assert config.website == 'https://paper-app.vercel.app'
    assert config.token == TOKEN
    assert TOKEN not in repr(config)
    assert not config.folder.exists()
    assert all(path.read_bytes() == content for path, content in before)


@pytest.mark.parametrize('value', [
    'http://127.0.0.1:8000', 'http://paper-api.onrender.com',
    'https://user:password@paper-api.onrender.com', 'https://paper-api.onrender.com/api',
    'https://paper-api.onrender.com?token=test', 'https://paper-api.onrender.com/#fragment',
])
def test_online_rejects_unsafe_or_ambiguous_origins(value):
    with pytest.raises(ValueError):
        launcher.https_origin(value)


@pytest.mark.parametrize('payload', [
    {'ok': True, 'mode': 'LIVE', 'live_orders_supported': False},
    {'ok': True, 'mode': 'PAPER', 'live_orders_supported': True},
    {'ok': True, 'mode': 'PAPER'},
    ['PAPER'],
])
def test_health_requires_explicit_paper_only_response(monkeypatch, payload):
    class Response(io.BytesIO):
        status = 200
    opener = SimpleNamespace(open=lambda *a, **kw: Response(json.dumps(payload).encode()))
    monkeypatch.setattr(launcher.urllib.request, 'build_opener', lambda *args: opener)
    assert launcher.paper_health('https://paper-api.onrender.com') is False


def test_health_request_has_no_credential_and_disables_redirects(monkeypatch):
    observed = {}
    class Response(io.BytesIO):
        status = 200
    def open_request(url, **kwargs):
        observed['url'] = url
        return Response(b'{"ok":true,"mode":"PAPER","live_orders_supported":false}')
    def build(handler):
        assert isinstance(handler, launcher.NoRedirects)
        assert handler.redirect_request(None, None, 302, '', {}, 'http://unsafe.test') is None
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr(launcher.urllib.request, 'build_opener', build)
    assert launcher.paper_health('https://paper-api.onrender.com') is True
    assert observed['url'] == 'https://paper-api.onrender.com/api/health'


def test_existing_local_manifest_is_never_repurposed(project):
    config = launcher.load_config(project)
    state = config.folder / '.companion'
    state.mkdir(parents=True)
    journal = state / 'manifest.sqlite3'
    journal.write_bytes(b'untouched-existing-journal')
    with pytest.raises(RuntimeError, match='separate online report folder'):
        launcher.bind_report_folder(config)
    assert journal.read_bytes() == b'untouched-existing-journal'
    assert not (state / 'online-backend.txt').exists()


def test_online_folder_binding_survives_reuse_and_refuses_another_backend(project):
    config = launcher.load_config(project)
    launcher.bind_report_folder(config)
    launcher.bind_report_folder(config)
    binding = config.folder / '.companion' / 'online-backend.txt'
    original = binding.read_bytes()
    config.backend = 'https://another-paper-api.onrender.com'
    with pytest.raises(RuntimeError, match='different online backend'):
        launcher.bind_report_folder(config)
    assert binding.read_bytes() == original


def test_start_reuses_only_matching_process_and_configuration(project, monkeypatch):
    config = launcher.load_config(project)
    state = project / '.desktop'
    state.mkdir()
    note = state / 'online-companion.json'
    note.write_text(json.dumps({'pid': 56, 'creation': 789, 'configuration': config.fingerprint()}), encoding='utf-8')
    monkeypatch.setattr(launcher, 'ROOT', project)
    monkeypatch.setattr(launcher, 'process_token', lambda pid: 789)
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **kw: pytest.fail('A second companion must not start'))
    assert launcher.start_online(config) == 56
    config.token = 'another-synthetic-online-token-long-enough'
    with pytest.raises(RuntimeError, match='different settings'):
        launcher.start_online(config)


def test_stale_pid_starts_only_the_online_wrapper_without_private_ambient_env(project, monkeypatch):
    config = launcher.load_config(project)
    state = project / '.desktop'
    state.mkdir()
    (state / 'online-companion.json').write_text(json.dumps({'pid': 56, 'creation': 789, 'configuration': config.fingerprint()}), encoding='utf-8')
    monkeypatch.setattr(launcher, 'ROOT', project)
    monkeypatch.setattr(launcher, 'process_token', lambda pid: 790 if pid == 56 else 999)
    for key in ('DATABASE_URL', 'DATA_DIR', 'BACKEND_URL', 'COMPANION_TOKEN', 'TRADES_FOLDER', 'POLL_SECONDS', 'PUBLIC_WEBSITE_URL'):
        monkeypatch.setenv(key, 'must-not-leak-into-online-child')
    observed = {}
    def spawn(arguments, **kwargs):
        observed.update(arguments=arguments, **kwargs)
        return SimpleNamespace(pid=60)
    monkeypatch.setattr(launcher.subprocess, 'Popen', spawn)
    assert launcher.start_online(config) == 60
    assert observed['arguments'] == [str(project / '.venv' / 'Scripts' / 'python.exe'), str(project / 'scripts' / 'online_launcher.py'), '--run-companion']
    assert TOKEN not in ' '.join(observed['arguments'])
    assert not any(key in observed['env'] for key in ('DATABASE_URL', 'DATA_DIR', 'BACKEND_URL', 'COMPANION_TOKEN', 'TRADES_FOLDER', 'POLL_SECONDS', 'PUBLIC_WEBSITE_URL'))
    assert observed['creationflags'] & launcher.subprocess.CREATE_NO_WINDOW
    stored = json.loads((state / 'online-companion.json').read_text())
    assert stored['creation'] == 999 and stored['pid'] == 60
    assert TOKEN not in json.dumps(stored)


def test_stop_touches_only_online_process_with_exact_creation_token(project, monkeypatch):
    state = project / '.desktop'
    state.mkdir()
    for role in ('backend', 'companion', 'online-companion'):
        (state / f'{role}.json').write_text(json.dumps({'pid': 56, 'creation': 789}), encoding='utf-8')
    observed = []
    monkeypatch.setattr(launcher, 'ROOT', project)
    monkeypatch.setattr(launcher, 'process_token', lambda *args, **kwargs: observed.append((args, kwargs)))
    launcher.stop_online()
    assert observed == [((56,), {'terminate': True, 'expected': 789})]
    assert (state / 'backend.json').is_file() and (state / 'companion.json').is_file()
    assert not (state / 'online-companion.json').exists()


def test_unverifiable_service_does_not_start_process_or_create_report_state(project, monkeypatch):
    config = launcher.load_config(project)
    monkeypatch.setattr(launcher, 'ROOT', project)
    monkeypatch.setattr(launcher, 'paper_health', lambda url: False)
    monkeypatch.setattr(launcher, 'start_online', lambda config: pytest.fail('No process should start'))
    with pytest.raises(RuntimeError, match='could not be verified'):
        launcher.main(['--no-open'])
    assert not config.folder.exists()


def test_incomplete_process_record_is_refused_instead_of_reusing_an_unknown_pid(project, monkeypatch):
    config = launcher.load_config(project)
    state = project / '.desktop'
    state.mkdir()
    (state / 'online-companion.json').write_text('{"pid":56}', encoding='utf-8')
    monkeypatch.setattr(launcher, 'ROOT', project)
    monkeypatch.setattr(launcher, 'process_token', lambda *a, **kw: pytest.fail('Unknown process metadata must not be used'))
    with pytest.raises(RuntimeError, match='process record needs review'):
        launcher.start_online(config)
    with pytest.raises(RuntimeError, match='process record needs review'):
        launcher.stop_online()


def test_online_worker_passes_only_explicit_settings_without_calling_local_main(project, monkeypatch):
    config = launcher.load_config(project)
    observed = {}
    class Service:
        def __init__(self, backend, token, folder):
            observed.update(backend=backend, token=token, folder=folder)
        def poll_once(self):
            observed['polled'] = True
        def close(self):
            observed['closed'] = True
    class StopEvent:
        stopped = False
        def is_set(self):
            return self.stopped
        def set(self):
            self.stopped = True
        def wait(self, interval):
            self.stopped = True
    fake_module = SimpleNamespace(Companion=Service, main=lambda: pytest.fail('Local companion.main must never run'))
    fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
    monkeypatch.setattr(launcher, 'ROOT', project)
    monkeypatch.setattr(launcher, 'paper_health', lambda url: True)
    monkeypatch.setattr(launcher.importlib.util, 'spec_from_file_location', lambda *args: fake_spec)
    monkeypatch.setattr(launcher.importlib.util, 'module_from_spec', lambda spec: fake_module)
    monkeypatch.setattr(launcher.threading, 'Event', StopEvent)
    monkeypatch.setattr(launcher.signal, 'signal', lambda *args: None)
    launcher.run_companion(config)
    assert observed == {'backend': config.backend, 'token': TOKEN, 'folder': config.folder, 'polled': True, 'closed': True}
