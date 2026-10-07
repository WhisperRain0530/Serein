"""Offline worker contracts: synthetic tasks, fake CLI, temporary databases."""
import asyncio
import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from test_public_features import settings, ingest, output_for


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('codex_event_worker', ROOT / 'scripts' / 'codex_event_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def task(role='track_router'):
    return {'status': 'awaiting_agent', 'job_id': 'synthetic-job', 'role': role,
            'request': {'role': role, 'prompt': 'Synthetic frozen source; return JSON.',
                        'rules': 'Synthetic role rules.', 'images': []}}


def fake_cli(tmp_path, monkeypatch, *, content=None, fail=None, sleep=False, event=None):
    """The Python child replaces only the CLI executable, never contacts a provider."""
    script = tmp_path / 'synthetic_cli.py'
    script.write_text(
        'import sys, json, os, pathlib, time\n'
        'args=sys.argv[1:]\n'
        'prompt=sys.stdin.read()\n'
        + ('time.sleep(10)\n' if sleep else '')
        + (f'print({fail!r},file=sys.stderr);sys.exit(1)\n' if fail else '')
        + 'output=pathlib.Path(args[args.index("--output-last-message")+1])\n'
        + ('output.write_text(' + repr(content) + ',encoding="utf-8")\n' if content is not None else
           'images=[pathlib.Path(args[i+1]).read_bytes().hex() for i,v in enumerate(args) if v=="-i"]\n'
           'output.write_text(json.dumps({"prompt":prompt,"images":images,"cwd":os.getcwd(),'
           '"keys":sorted(os.environ)}),encoding="utf-8")\n')
        + 'print(json.dumps(' + repr(event or {'type': 'turn.completed', 'usage': {}}) + '))\n', encoding='utf-8')
    build = worker.codex_command
    monkeypatch.setattr(worker, 'codex_command', lambda *args: [sys.executable, str(script)] + build(*args)[1:])


def test_actual_fake_process_gets_frozen_text_images_and_no_secrets(tmp_path, monkeypatch, capsys):
    fake_cli(tmp_path, monkeypatch)
    item = task('event_curator')
    item['request']['images'] = [{'url': 'data:image/png;base64,' + base64.b64encode(b'synthetic PNG').decode(),
                                  'source_message_id': 7, 'position': 0, 'evidence_role': 'owned'}]
    item['request']['unrelated_private_field'] = 'must not enter prompt'
    runner = worker.CodexRunner(tmp_path, environ={**os.environ,
        'SEREIN_AGENT_TOKEN': 'fake-private-token', 'OPENAI_API_KEY': 'fake-api-key',
        'CODEX_API_KEY': 'fake-api-key-2', 'CODEX_ACCESS_TOKEN': 'fake-access-token'})
    result = runner.run(item)
    assert item['request']['prompt'] in result['prompt'] and item['request']['rules'] in result['prompt']
    assert 'must not enter prompt' not in result['prompt'] and 'base64' not in result['prompt']
    assert 'source_message_id' in result['prompt']
    assert result['images'] == [b'synthetic PNG'.hex()]
    assert not {'SEREIN_AGENT_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'} & set(result['keys'])
    assert not Path(result['cwd']).exists()  # All task text/images/output removed.
    assert capsys.readouterr().out == ''


def test_cli_options_require_subscription_isolation_and_exact_requested_model(tmp_path):
    command = worker.codex_command('codex', 'gpt-5.6-sol', tmp_path, tmp_path / 'final.json', [])
    assert command[command.index('--model') + 1] == 'gpt-5.6-sol'
    assert {'--ephemeral', '--ignore-user-config', '--skip-git-repo-check', '--json'} <= set(command)
    assert command[command.index('--sandbox') + 1] == 'read-only'
    for override in ('forced_login_method="chatgpt"', 'model_provider="openai"',
                     'features.shell_tool=false', 'features.apps=false', 'features.hooks=false',
                     'features.multi_agent=false', 'mcp_servers={}', 'web_search="disabled"',
                     'features.plugins=false', 'features.browser_use=false', 'features.computer_use=false',
                     'features.view_image=false', 'features.skill_search=false', 'skip_host_skill_discovery=true',
                     'project_doc_max_bytes=0', 'history.persistence="none"'):
        assert override in command
    assert command[-1] == '-'
    assert '--dangerously-bypass-approvals-and-sandbox' not in command


@pytest.mark.parametrize('body', ['```json\n{}\n```', '[]', '{"x":1,"x":2}',
                                 '{"x":NaN}', '{"x":1e999}', '{"x":'])
def test_non_json_or_ambiguous_result_never_becomes_submission(tmp_path, monkeypatch, body):
    fake_cli(tmp_path, monkeypatch, content=body)
    with pytest.raises(worker.WorkerError):
        worker.CodexRunner(tmp_path).run(task())
    assert not list(tmp_path.glob('task-*'))


def test_cli_timeout_kills_child_and_cleans_materials(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, sleep=True)
    with pytest.raises(worker.WorkerError, match='codex_timeout'):
        worker.CodexRunner(tmp_path, timeout=0.1).run(task())
    assert not list(tmp_path.glob('task-*'))


@pytest.mark.parametrize('kind', ['command_execution', 'mcp_tool_call', 'web_search', 'file_change', 'unknown_tool'])
def test_tool_activity_fails_closed(tmp_path, monkeypatch, kind):
    fake_cli(tmp_path, monkeypatch, content='{}', event={'type': 'item.completed', 'item': {'type': kind}})
    with pytest.raises(worker.WorkerError, match='codex_tool_activity'):
        worker.CodexRunner(tmp_path).run(task())


def test_incomplete_cli_turn_is_not_success():
    with pytest.raises(worker.WorkerError, match='incomplete_codex_turn'):
        worker.check_events(b'{"type":"turn.started"}\n')


@pytest.mark.parametrize('url', ['https://images.example/private.png', 'file:///tmp/image.png',
                                'data:image/svg+xml;base64,PHN2Zy8+'])
def test_worker_never_fetches_or_opens_external_image_urls(tmp_path, url):
    item = task('event_curator')
    item['request']['images'] = [{'url': url}]
    with pytest.raises(worker.WorkerError, match='invalid_images'):
        worker.frozen_input(item, tmp_path)


def test_writer_scope_is_never_expanded_with_images(tmp_path):
    item = task('event_writer')
    item['request']['images'] = [{'url': 'data:image/png;base64,YQ=='}]
    with pytest.raises(worker.WorkerError, match='invalid_images'):
        worker.frozen_input(item, tmp_path)


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []

    def call(self, name, payload):
        self.calls.append((name, copy.deepcopy(payload)))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def scheduled_ready(self, timestamp, *, manual=False):
        return True


def model(result=None, error=None):
    calls = []
    def run(item):
        calls.append(item)
        if error:
            raise error
        return result if result is not None else {'synthetic': True}
    return SimpleNamespace(model=worker.DEFAULT_MODEL, run=run, calls=calls)


def test_lost_submit_receipt_and_restart_resubmit_exact_output_without_model(tmp_path):
    first = FakeClient([task(), worker.WorkerError('serein_unavailable')])
    original = model({'synthetic': 'same exact output'})
    with pytest.raises(worker.WorkerError):
        worker.Worker(first, original, tmp_path).step()
    assert (tmp_path / 'outbox.json').exists()
    retry = FakeClient([{'status': 'unchanged', 'job_id': 'synthetic-job'}])
    second = model(error=AssertionError('Model must not be run again'))
    assert worker.Worker(retry, second, tmp_path).step()
    assert first.calls[-1] == retry.calls[0]
    assert not (tmp_path / 'outbox.json').exists()


def test_rejected_result_is_retained_for_private_inspection_not_repeated(tmp_path):
    client = FakeClient([task(), worker.WorkerError('serein_rejected', definitive=True)])
    with pytest.raises(worker.WorkerError, match='pipeline_output_rejected'):
        worker.Worker(client, model(), tmp_path).step()
    assert not (tmp_path / 'outbox.json').exists()
    assert (tmp_path / 'rejected-output.json').exists()


def test_quota_backoff_survives_restart_and_logs_no_content(tmp_path, capsys):
    now = [1000]
    limited = worker.WorkerError('codex_usage_limit', retry_after=3600)
    first = worker.Worker(FakeClient([task()]), model(error=limited), tmp_path, clock=lambda: now[0])
    assert first.run(once=True) == 1
    saved = json.loads((tmp_path / 'backoff.json').read_text())
    assert saved['retry_at'] >= 4600
    second = worker.Worker(FakeClient([]), model(), tmp_path, clock=lambda: now[0])
    assert second.run(once=True) == 1 and not second.client.calls
    logs = capsys.readouterr().out
    assert 'codex_usage_limit' in logs and 'Synthetic' not in logs and 'synthetic-job' not in logs
    assert set(json.loads((tmp_path / 'status.json').read_text())) == {
        'state', 'updated_at', 'retry_at', 'last_error', 'model'}


def test_continuous_worker_waits_when_idle_and_stops_cleanly(tmp_path):
    client = FakeClient([{'status': 'current'}])
    agent = worker.Worker(client, model(), tmp_path)
    waits = []
    def wait(seconds):
        waits.append(seconds)
        agent.stop.set()
    agent.wait = wait
    assert agent.run() == 0 and waits == [60]
    assert json.loads((tmp_path / 'status.json').read_text())['state'] == 'stopped'


def test_automatic_worker_obeys_pause_without_claiming_work(tmp_path):
    client = FakeClient([])
    client.scheduled_ready = lambda _now, **_kwargs: False
    agent = worker.Worker(client, model(), tmp_path)
    assert agent.run(once=True) == 0 and not client.calls


def test_schedule_respects_shanghai_three_am_and_saved_switch():
    from datetime import datetime, timezone
    client = worker.SereinClient('http://127.0.0.1:8011', 'synthetic')
    client.call = lambda *_: {'pipeline': {'auto_enabled': True, 'execution_mode': 'agent'}}
    before = datetime(2026, 10, 7, 18, 59, tzinfo=timezone.utc).timestamp()
    after = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc).timestamp()
    assert not client.scheduled_ready(before) and client.scheduled_ready(after)
    client.call = lambda *_: {'pipeline': {'auto_enabled': False, 'execution_mode': 'agent'}}
    assert not client.scheduled_ready(after)
    assert client.scheduled_ready(before, manual=True)
    client.call = lambda *_: {'pipeline': {'auto_enabled': True, 'execution_mode': 'api'}}
    with pytest.raises(worker.WorkerError, match='select_agent_execution_mode'):
        client.scheduled_ready(after, manual=True)


def test_http_contract_redirect_refusal_and_safe_errors():
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return io.BytesIO(b'{"status":"current"}')
    client = worker.SereinClient('http://127.0.0.1:8011', 'synthetic-token', opener=Opener())
    assert client.call('pipeline_next', {'include_recent': False}) == {'status': 'current'}
    assert calls[0].full_url == 'http://127.0.0.1:8011/v1/extensions/pipeline_next'
    assert calls[0].headers['Authorization'] == 'Bearer synthetic-token'
    assert worker.NoRedirect().redirect_request(None, None, None, None, None, None) is None
    class Reject:
        def open(self, *_args, **_kwargs):
            raise HTTPError('https://fake.invalid', 429, 'PRIVATE RESPONSE', {'Retry-After': '7200'}, None)
    client.opener = Reject()
    with pytest.raises(worker.WorkerError) as failure:
        client.call('pipeline_next', {})
    assert failure.value.retry_after == 7200 and str(failure.value) == 'serein_rate_limit'
    with pytest.raises(worker.WorkerError):
        worker.SereinClient('http://public.example', 'synthetic-token')


def test_only_one_worker_owns_the_state_directory(tmp_path):
    with worker.worker_lock(tmp_path):
        with pytest.raises(worker.WorkerError, match='worker_already_running'):
            with worker.worker_lock(tmp_path):
                pass
    with worker.worker_lock(tmp_path):
        pass


def test_real_synthetic_pipeline_is_drained_and_settled(settings, tmp_path):
    """Exercise actual frozen requests, roles, validation and durable settlement."""
    from serein.extensions import pipeline
    from serein.core.store import Store
    ingest(settings)
    roles = []
    class LocalClient:
        def scheduled_ready(self, timestamp, *, manual=False):
            return True

        def call(self, name, payload):
            if name == 'pipeline_next':
                return asyncio.run(pipeline.advance(settings.database, **payload))
            return pipeline.submit(settings.database, **payload)
    def run(item):
        roles.append(item['role'])
        worker.frozen_input(item, tmp_path)
        return output_for(item['role'], item['request'])
    runner = SimpleNamespace(model=worker.DEFAULT_MODEL, run=run)
    agent = worker.Worker(LocalClient(), runner, tmp_path)
    assert agent.run(once=True, include_recent=True) == 0
    assert roles == ['track_router', 'event_curator', 'event_writer']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2
    assert not agent.outbox.exists()
