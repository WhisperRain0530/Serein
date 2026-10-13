"""Empty temporary volumes and fake processes; never connect to a deployment."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('zeabur_runtime', ROOT / 'scripts' / 'zeabur_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_bootstrap_is_private_idempotent_and_preserves_database(tmp_path, capsys):
    data = tmp_path / 'synthetic instance'
    config = runtime.prepare(data)
    credentials = runtime.read_credentials(data)
    assert credentials['username'] == 'kitty'
    assert len(credentials['gateway_key']) >= 48 and len(credentials['password']) >= 40
    assert credentials['gateway_key'] != credentials['password']
    parsed = tomllib.loads(config.read_text('utf-8'))
    assert Path(parsed['storage']['database']) == data / 'serein.db'
    assert Path(parsed['storage']['index']) == data / 'recall-index.sqlite'
    assert parsed['runtime']['writable'] is True
    assert (data / 'legacy-input').is_dir()
    from serein.config import load_settings
    from serein.bootstrap import initialize
    from serein.core.store import Store
    settings = load_settings(config)
    initialize(settings)
    with Store(settings.database) as store:
        store.create('synthetic-scene', 'scene', 'Synthetic title', 'Synthetic retained body')
    snapshot = {path.name:path.read_bytes() for path in (data / 'secrets').iterdir()}
    old_config = config.read_bytes()
    runtime.prepare(data, username='ignored-on-restart')
    assert {path.name:path.read_bytes() for path in (data / 'secrets').iterdir()} == snapshot
    assert config.read_bytes() == old_config
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM documents WHERE id=?', ('synthetic-scene',)).fetchone()[0] == 1
    captured = capsys.readouterr()
    assert credentials['gateway_key'] not in captured.out + captured.err
    assert credentials['password'] not in captured.out + captured.err
    if os.name != 'nt':
        assert data.stat().st_mode & 0o777 == 0o700
        for path in (data / 'secrets').iterdir():
            assert path.stat().st_mode & 0o777 == 0o600


def test_native_web_password_verifier_accepts_bootstrap_record(tmp_path):
    if not shutil.which('node'):
        pytest.skip('Node is required for the cross-runtime password verification')
    runtime.prepare(tmp_path)
    code = """
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {authenticate} from './web/server/webAuth.mjs';
const directory=process.argv[1];
const record=JSON.parse(readFileSync(join(directory,'secrets','web-auth.json'),'utf8'));
const password=readFileSync(join(directory,'secrets','web-password'),'utf8').trim();
const basic='Basic '+Buffer.from(record.username+':'+password).toString('base64');
if(!authenticate(basic,record)) throw new Error('Synthetic bootstrap credentials were rejected');
"""
    result = subprocess.run(['node', '--input-type=module', '-e', code, str(tmp_path)],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''


def test_missing_or_corrupt_existing_credentials_fail_closed(tmp_path):
    runtime.prepare(tmp_path)
    (tmp_path / 'serein.db').touch()
    token = tmp_path / 'secrets' / 'api-token'
    token.unlink()
    with pytest.raises(RuntimeError, match='credentials are missing'):
        runtime.prepare(tmp_path)
    assert not token.exists()


def test_credentials_read_does_not_initialize_and_detects_mismatch(tmp_path):
    with pytest.raises(RuntimeError, match='unavailable or inconsistent'):
        runtime.read_credentials(tmp_path)
    assert not list(tmp_path.iterdir())
    runtime.prepare(tmp_path)
    (tmp_path / 'secrets' / 'web-password').write_text('synthetic-unmatched-password', 'utf-8')
    with pytest.raises(RuntimeError, match='unavailable or inconsistent'):
        runtime.read_credentials(tmp_path)


def test_only_explicit_credentials_command_emits_secrets(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('SEREIN_PUBLIC_ORIGIN', 'https://synthetic.example')
    assert runtime.main(['init', '--data-dir', str(tmp_path)]) == 0
    initial_output = capsys.readouterr()
    credentials = runtime.read_credentials(tmp_path)
    assert credentials['password'] not in initial_output.out + initial_output.err
    assert credentials['gateway_key'] not in initial_output.out + initial_output.err
    assert runtime.main(['credentials', '--data-dir', str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {**credentials, 'web_url':'https://synthetic.example',
                       'api_base_url':'https://synthetic.example/v1'}


def test_real_init_command_uses_temporary_volume_without_starting_services(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'zeabur_runtime.py'),
        'init', '--data-dir', str(tmp_path)], cwd=ROOT,
        env={**os.environ, 'PYTHONPATH':str(ROOT / 'src')}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'No services were started' in result.stdout
    credentials = runtime.read_credentials(tmp_path)
    assert credentials['password'] not in result.stdout + result.stderr
    assert credentials['gateway_key'] not in result.stdout + result.stderr
    assert not (tmp_path / 'serein.db').exists()


def test_environment_uses_only_own_files_and_internal_loopback(tmp_path):
    env = runtime.runtime_environment(tmp_path, {
        'PORT':'8080', 'SEREIN_HTTP_TOKEN':'synthetic-unrelated-token',
        'SEREIN_MEMORY_TOKEN':'synthetic-unrelated-token',
        'SEREIN_MEMORY_URL':'https://unrelated.example', 'SEREIN_GATEWAY_BIND':'192.0.2.1',
        'SEREIN_PUBLIC_ORIGIN':'https://synthetic.example'})
    assert 'SEREIN_HTTP_TOKEN' not in env and 'SEREIN_MEMORY_TOKEN' not in env
    assert env['SEREIN_MEMORY_URL'] == 'http://127.0.0.1:8011'
    assert env['SEREIN_GATEWAY_BIND'] == '0.0.0.0' and env['SEREIN_GATEWAY_PORT'] == '8080'
    assert env['SEREIN_PREVIEW_PORT'] == '4173'
    assert env['SEREIN_PUBLIC_ORIGIN'] == 'https://synthetic.example'
    for key in ('SEREIN_HTTP_TOKEN_FILE', 'SEREIN_MEMORY_TOKEN_FILE', 'SEREIN_WEB_AUTH_FILE',
                'SEREIN_ROUTE_DRAFT_FILE', 'SEREIN_MIGRATION_PATH_ROOT'):
        assert Path(env[key]).is_relative_to(tmp_path)


@pytest.mark.parametrize('port', ['wrong', '0', '65536', '8011', '8012', '4173'])
def test_invalid_or_internal_public_port_is_refused(port):
    with pytest.raises(ValueError, match='PORT'):
        runtime.gateway_port({'PORT':port})


class Child:
    def __init__(self, code=None):
        self.code = code
        self.terminated = False
    def poll(self):
        return self.code
    def terminate(self):
        self.terminated = True
        self.code = -15
    def wait(self, timeout=None):
        return self.code


def test_gateway_failure_stops_core_and_returns_failure(tmp_path, monkeypatch, capsys):
    core, gateway = Child(), Child(1)
    children = iter([core, gateway])
    commands = []
    def launch(command, **options):
        commands.append(command)
        assert options['env']['SEREIN_MEMORY_URL'] == 'http://127.0.0.1:8011'
        return next(children)
    monkeypatch.setattr(runtime.subprocess, 'Popen', launch)
    monkeypatch.setattr(runtime, 'wait_for_core', lambda child, stop:child is core)
    assert runtime.supervise(tmp_path, threading.Event(), env={}) == 1
    assert core.terminated
    assert commands[0][-4:] == ['--host', '127.0.0.1', '--port', '8011']
    assert commands[1] == ['node', str(ROOT / 'web' / 'server' / 'gateway.mjs')]
    assert 'Gateway service exited' in capsys.readouterr().err


def test_startup_failure_does_not_start_gateway_and_cleans_child(tmp_path, monkeypatch):
    core = Child()
    calls = []
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *args, **kwargs:(calls.append(args), core)[1])
    def fail(*args):
        raise RuntimeError('Synthetic readiness failure')
    monkeypatch.setattr(runtime, 'wait_for_core', fail)
    with pytest.raises(RuntimeError, match='Synthetic readiness failure'):
        runtime.supervise(tmp_path, threading.Event(), env={})
    assert len(calls) == 1 and core.terminated


def test_shutdown_during_startup_stops_core_without_gateway(tmp_path, monkeypatch):
    core = Child()
    stop = threading.Event()
    stop.set()
    calls = []
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *args, **kwargs:(calls.append(args), core)[1])
    assert runtime.supervise(tmp_path, stop, env={}) == 0
    assert len(calls) == 1 and core.terminated


def test_healthcheck_uses_gateway_ready_endpoint(monkeypatch):
    seen = []
    monkeypatch.setenv('PORT', '8080')
    monkeypatch.setattr(runtime, 'http_ready', lambda url:(seen.append(url), True)[1])
    assert runtime.main(['healthcheck']) == 0
    assert seen == ['http://127.0.0.1:8080/ready']


def test_codex_worker_has_dedicated_home_and_no_api_fallback(tmp_path):
    source = {'CODEX_HOME':'unrelated', 'OPENAI_API_KEY':'synthetic-secret',
              'CODEX_API_KEY':'synthetic-other', 'OPENAI_BASE_URL':'https://unrelated.example',
              'SEREIN_AGENT_TOKEN':'synthetic-other-token'}
    env = runtime.codex_environment(tmp_path, source)
    assert Path(env['CODEX_HOME']) == tmp_path / 'codex-worker' / 'auth'
    assert env['SEREIN_AGENT_URL'] == 'http://127.0.0.1:8011'
    assert Path(env['SEREIN_AGENT_TOKEN_FILE']) == tmp_path / 'secrets' / 'api-token'
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'SEREIN_AGENT_TOKEN'):
        assert key not in env
    core_env = runtime.runtime_environment(tmp_path, source)
    for key in runtime.CODEX_AUTH_ENV:
        assert key not in core_env
    assert source['CODEX_HOME'] == 'unrelated'


def test_worker_exit_does_not_restart_chat_services(tmp_path, monkeypatch, capsys):
    core, gateway, worker = Child(), Child(), Child(2)
    launched = []
    children = iter((core, gateway, worker))
    def launch(command, **options):
        launched.append((command, options))
        return next(children)
    class StopAfter:
        count = 0
        def wait(self, _seconds):
            self.count += 1
            return self.count >= 3
    monkeypatch.setattr(runtime.subprocess, 'Popen', launch)
    monkeypatch.setattr(runtime, 'wait_for_core', lambda *_:True)
    assert runtime.supervise(tmp_path, StopAfter(), env={'SEREIN_CODEX_WORKER_ENABLED':'1'}) == 0
    assert len(launched) == 3
    assert launched[2][0][1].endswith('codex_event_worker.py')
    assert 'CODEX_HOME' not in launched[0][1]['env']
    assert Path(launched[2][1]['env']['CODEX_HOME']) == tmp_path / 'codex-worker' / 'auth'
    assert 'Chat remains available' in capsys.readouterr().out


def test_task_service_uses_same_login_and_serial_lock_but_not_core_auth(tmp_path, monkeypatch):
    launched = []
    def launch(command, **options):
        launched.append((command, options))
        return Child()
    class StopAfter:
        count = 0
        def wait(self, _seconds):
            self.count += 1
            return self.count >= 3
    monkeypatch.setattr(runtime.subprocess, 'Popen', launch)
    monkeypatch.setattr(runtime, 'wait_for_core', lambda *_:True)
    assert runtime.supervise(tmp_path, StopAfter(), env={
        'SEREIN_CODEX_WORKER_ENABLED':'1', 'SEREIN_CODEX_TASKS_ENABLED':'1',
        'CODEX_HOME':'unrelated', 'OPENAI_API_KEY':'unrelated-secret',
    }) == 0
    assert len(launched) == 4
    assert launched[3][0][1].endswith('codex_task_service.py')
    assert launched[3][0][-1] == str(tmp_path / 'codex-tasks')
    core_env, worker_env, task_env = [launched[i][1]['env'] for i in (0, 2, 3)]
    assert 'CODEX_HOME' not in core_env and 'OPENAI_API_KEY' not in task_env
    assert worker_env['CODEX_HOME'] == task_env['CODEX_HOME']
    assert worker_env['SEREIN_CODEX_LOCK_FILE'] == task_env['SEREIN_CODEX_LOCK_FILE']
    assert core_env['SEREIN_CODEX_TASK_TOKEN_FILE'] == task_env['SEREIN_AGENT_TOKEN_FILE']
    assert core_env['SEREIN_CODEX_TASK_STATE_FILE'] == str(tmp_path / 'codex-tasks' / 'status.json')


def test_status_redacts_diagnostics_and_does_not_accept_api_login(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, '', 'Logged in using an API key: synthetic-secret'))
    status = runtime.codex_status(tmp_path, {})
    assert status == {'subscription_authenticated':False, 'worker':{}}
    assert 'synthetic-secret' not in json.dumps(status)
