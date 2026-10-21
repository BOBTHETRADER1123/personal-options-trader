"""Desktop pairing and process-safety checks using only isolated test files."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    'desktop_launcher_tests',
    Path(__file__).resolve().parents[2] / 'scripts' / 'desktop_launcher.py',
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'application'
    root.mkdir()
    (root / 'companion').mkdir()
    (root / '.env.example').write_text(
        'ENVIRONMENT=development\nCOMPANION_TOKEN_SHA256=\nCUSTOM_SETTING=keep-me\n',
        encoding='utf-8',
    )
    return root


def test_initial_pairing_keeps_raw_token_only_in_companion(project, tmp_path):
    destination = tmp_path / 'chosen-reports'
    assert launcher.prepare_config(project, destination) == destination.resolve()
    backend = launcher.env_values(project / '.env')
    companion = launcher.env_values(project / 'companion' / '.env')
    token = companion['COMPANION_TOKEN']
    assert len(token) >= 40
    assert backend['COMPANION_TOKEN_SHA256'] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in (project / '.env').read_text()
    assert companion['BACKEND_URL'] == launcher.URL
    assert companion['TRADES_FOLDER'] == destination.resolve().as_posix()
    assert destination.is_dir()
    assert backend['CUSTOM_SETTING'] == 'keep-me'


def test_repeated_pairing_preserves_token_settings_and_existing_folder(project, tmp_path):
    chosen = tmp_path / 'original-folder'
    launcher.prepare_config(project, chosen)
    original = launcher.env_values(project / 'companion' / '.env')['COMPANION_TOKEN']
    launcher.put_env(project / 'companion' / '.env', 'POLL_SECONDS', '27')
    launcher.put_env(project / 'companion' / '.env', 'CUSTOM_COMPANION_SETTING', 'keep-this-too')
    ignored = tmp_path / 'different-requested-folder'
    assert launcher.prepare_config(project, ignored) == chosen.resolve()
    values = launcher.env_values(project / 'companion' / '.env')
    assert values['COMPANION_TOKEN'] == original
    assert values['POLL_SECONDS'] == '27'
    assert values['CUSTOM_COMPANION_SETTING'] == 'keep-this-too'
    assert launcher.env_values(project / '.env')['CUSTOM_SETTING'] == 'keep-me'
    assert not ignored.exists()


@pytest.mark.parametrize('local_token', [None, 'different-test-token'])
def test_conflicting_pairing_does_not_modify_existing_configuration(project, tmp_path, local_token):
    digest = hashlib.sha256(b'previous-pairing-test-token').hexdigest()
    (project / '.env').write_text(
        f'ENVIRONMENT=development\nCOMPANION_TOKEN_SHA256={digest}\nCUSTOM_SETTING=preserved\n',
        encoding='utf-8',
    )
    companion_path = project / 'companion' / '.env'
    if local_token:
        companion_path.write_text(f'COMPANION_TOKEN={local_token}\nPOLL_SECONDS=42\n', encoding='utf-8')
    before_backend = (project / '.env').read_bytes()
    before_companion = companion_path.read_bytes() if companion_path.exists() else None
    destination = tmp_path / 'must-not-be-created'
    with pytest.raises(RuntimeError, match='Existing secrets were preserved'):
        launcher.prepare_config(project, destination)
    assert (project / '.env').read_bytes() == before_backend
    assert (companion_path.read_bytes() if companion_path.exists() else None) == before_companion
    assert not destination.exists()


def test_production_configuration_is_not_changed(project, tmp_path):
    configuration = project / '.env'
    configuration.write_text('ENVIRONMENT=production\nCUSTOM_SETTING=do-not-alter\n', encoding='utf-8')
    before = configuration.read_bytes()
    destination = tmp_path / 'uncreated-folder'
    with pytest.raises(RuntimeError, match='production configuration'):
        launcher.prepare_config(project, destination)
    assert configuration.read_bytes() == before
    assert not (project / 'companion' / '.env').exists()
    assert not destination.exists()


def test_hosted_companion_url_is_refused_without_repointing(project, tmp_path):
    token = 'existing-cloud-pairing-test-token'
    digest = hashlib.sha256(token.encode()).hexdigest()
    backend = project / '.env'
    backend.write_text(f'ENVIRONMENT=development\nCOMPANION_TOKEN_SHA256={digest}\n', encoding='utf-8')
    companion = project / 'companion' / '.env'
    companion.write_text(
        f'BACKEND_URL=https://private-test-backend.onrender.com\nCOMPANION_TOKEN={token}\nPOLL_SECONDS=19\n',
        encoding='utf-8',
    )
    before_backend, before_companion = backend.read_bytes(), companion.read_bytes()
    destination = tmp_path / 'never-created'
    with pytest.raises(RuntimeError, match='hosted backend'):
        launcher.prepare_config(project, destination)
    assert backend.read_bytes() == before_backend
    assert companion.read_bytes() == before_companion
    assert not destination.exists()


class FakeWindowsAPI:
    def __init__(self, creation):
        self.creation = creation
        self.terminated = []
        self.closed = []
        self.opened = []

    def OpenProcess(self, permissions, inherit, pid):
        self.opened.append((permissions, inherit, pid))
        return 123

    def GetProcessTimes(self, handle, creation, exit_time, kernel, user):
        creation._obj.dwHighDateTime = self.creation >> 32
        creation._obj.dwLowDateTime = self.creation & 0xffffffff
        return True

    def TerminateProcess(self, handle, code):
        self.terminated.append((handle, code))
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)


@pytest.mark.parametrize('expected,should_stop', [(0x123456789, True), (0x123456788, False), (None, False)])
def test_stop_requires_exact_process_creation_time(monkeypatch, expected, should_stop):
    api = FakeWindowsAPI(0x123456789)
    monkeypatch.setattr(launcher, 'windows_api', lambda: api)
    assert launcher.process_token(321, terminate=True, expected=expected) == 0x123456789
    assert bool(api.terminated) is should_stop
    assert api.closed == [123]
    assert api.opened[0][2] == 321


def test_creation_check_closes_handle_without_stopping(monkeypatch):
    api = FakeWindowsAPI(123456)
    monkeypatch.setattr(launcher, 'windows_api', lambda: api)
    assert launcher.process_token(222) == 123456
    assert api.terminated == []
    assert api.closed == [123]


def test_start_reuses_only_matching_recorded_process(project, monkeypatch):
    state = project / '.desktop'
    state.mkdir()
    (state / 'backend.json').write_text(json.dumps({'pid': 456, 'creation': 123456}), encoding='utf-8')
    monkeypatch.setattr(launcher, 'ROOT', project)
    monkeypatch.setattr(launcher, 'process_token', lambda pid: 123456 if pid == 456 else None)
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No new process should start'))
    assert launcher.start_process('backend', ['must-not-be-run']) == 456


@pytest.mark.parametrize('explicit_local_config', [False, True])
def test_start_drops_inherited_qa_database_overrides(project, monkeypatch, explicit_local_config):
    from types import SimpleNamespace
    expected_data = (project / 'configured-local-data').as_posix()
    expected_database = 'sqlite:///configured-local-journal.sqlite3'
    config = 'ENVIRONMENT=development\n'
    if explicit_local_config:
        config += f'DATA_DIR={expected_data}\nDATABASE_URL={expected_database}\n'
    (project / '.env').write_text(config, encoding='utf-8')
    monkeypatch.setenv('DATA_DIR', 'must-not-use-inherited-qa-data')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///must-not-use-qa.sqlite3')
    monkeypatch.setenv('POT_TEST_UNRELATED_SETTING', 'keep-unrelated-environment')
    monkeypatch.setattr(launcher, 'ROOT', project)
    monkeypatch.setattr(launcher, 'process_token', lambda pid: 987654 if pid == 999 else None)
    observed = {}

    def fake_start(*args, **kwargs):
        for key in ('DATA_DIR', 'DATABASE_URL', 'POT_TEST_UNRELATED_SETTING'):
            observed[key] = kwargs['env'].get(key)
        return SimpleNamespace(pid=999)

    monkeypatch.setattr(launcher.subprocess, 'Popen', fake_start)
    assert launcher.start_process('backend', ['never-executed']) == 999
    assert observed.get('DATA_DIR') == (expected_data if explicit_local_config else None)
    assert observed.get('DATABASE_URL') == (expected_database if explicit_local_config else None)
    assert observed.get('POT_TEST_UNRELATED_SETTING') == 'keep-unrelated-environment'
