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
        + 'for event in ' + repr(event if isinstance(event, list) else
                                [event or {'type': 'turn.completed', 'usage': {}}]) + ':\n'
        + ' print(json.dumps(event))\n', encoding='utf-8')
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
                     'features.view_image=false', 'features.skill_search=false',
                     'project_doc_max_bytes=0', 'history.persistence="none"'):
        assert override in command
    assert command[-1] == '-'
    assert '--dangerously-bypass-approvals-and-sandbox' not in command
    assert not any(value.startswith(('skip_host_skill_discovery=', 'tools.view_image=')) for value in command)


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


def test_event_runner_respects_subscription_service_shared_cooldown(tmp_path, monkeypatch):
    lock = tmp_path / 'shared.lock'
    worker.private_write(lock.with_name(lock.name + '.cooldown.json'), {
        'code': 'codex_usage_limit', 'retry_at': worker.time.time() + 3600})
    monkeypatch.setattr(worker, 'run_codex_text', lambda *_args, **_kwargs:
                        pytest.fail('Event must not invoke CLI during shared quota backoff'))
    with pytest.raises(worker.WorkerError, match='codex_usage_limit'):
        worker.CodexRunner(tmp_path, environ={'SEREIN_CODEX_LOCK_FILE': str(lock)}).run(task())
    assert not list(tmp_path.glob('task-*'))


@pytest.mark.parametrize('kind', ['command_execution', 'mcp_tool_call', 'collab_tool_call',
                                 'web_search', 'file_change', 'unknown_tool'])
def test_tool_activity_fails_closed(tmp_path, monkeypatch, kind):
    fake_cli(tmp_path, monkeypatch, content='{}', event={'type': 'item.completed', 'item': {'type': kind}})
    with pytest.raises(worker.WorkerError, match='codex_tool_activity'):
        worker.CodexRunner(tmp_path).run(task())


def test_verified_startup_notice_and_plan_updates_allow_completed_json(tmp_path, monkeypatch):
    # Replay the verified 0.160.1 notice, plus its ordinary plan lifecycle.
    events = [{'type': 'item.completed', 'item': {'type': 'error', 'message':
        'Code Mode is unavailable because code-mode host is disabled. '
        'Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.'}}]
    events.extend({'type': lifecycle, 'item': {'type': 'todo_list', 'items': [
        {'text': 'Read synthetic evidence', 'completed': lifecycle == 'item.completed'}]}}
        for lifecycle in ('item.started', 'item.updated', 'item.completed'))
    events.extend([{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"synthetic":true}'}},
                   {'type': 'turn.completed', 'usage': {}}])
    fake_cli(tmp_path, monkeypatch, content='{"synthetic":true}', event=events)
    assert worker.CodexRunner(tmp_path).run(task()) == {'synthetic': True}
    assert not list(tmp_path.glob('task-*'))


@pytest.mark.parametrize('notice', [
    'Codex is ignoring 2 unrecognized configuration settings.',
    'Code Mode is unavailable because code-mode host is disabled. Falling back to direct tools; '
    'enable `features.code_mode_host` and install `codex-code-mode-host`.',
    worker.CODE_MODE_DISABLED_WARNING + ' Additional unexpected behavior.',
    worker.CODE_MODE_DISABLED_WARNING.replace('host is disabled', 'host failed to start'),
    'Synthetic unknown notice with fake-private-token',
])
def test_other_cli_notices_fail_without_leaking_text(notice, capsys):
    events = [{'type': 'item.completed', 'item': {'type': 'error', 'message': notice}},
              {'type': 'turn.completed', 'usage': {}}]
    with pytest.raises(worker.WorkerError) as failure:
        worker.check_events('\n'.join(json.dumps(event) for event in events))
    assert failure.value.code == 'codex_cli_warning'
    assert str(failure.value) == 'codex_cli_warning'
    assert failure.value.retry_after == 3600 and capsys.readouterr().out == ''


def test_model_reroute_is_rejected_even_when_turn_completed():
    events = [{'type': 'item.completed', 'item': {'type': 'error', 'message':
        'model rerouted: gpt-5.6-sol -> another-model (SyntheticReason)'}},
        {'type': 'turn.completed', 'usage': {}}]
    with pytest.raises(worker.WorkerError, match='^model_rerouted$'):
        worker.check_events('\n'.join(json.dumps(event) for event in events))


def test_expected_notice_does_not_replace_turn_completion():
    event = {'type': 'item.completed', 'item': {'type': 'error',
                                             'message': worker.CODE_MODE_DISABLED_WARNING}}
    with pytest.raises(worker.WorkerError, match='incomplete_codex_turn'):
        worker.check_events(json.dumps(event))


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


def pretranscribed_task():
    item = task('event_curator')
    item['request'].update(pretranscribed=True, identity={}, curator_image_transcriptions=[
        {'source_message_id': 1, 'position': position, 'sha256': str(position) * 64,
         'evidence_role': 'owned', 'text': f'Synthetic page {position}', 'unreadable': False}
        for position in (1, 2)])
    return item


@pytest.mark.parametrize('change', ['text', 'whitespace', 'source', 'position', 'sha256',
                                  'evidence_role', 'unreadable', 'boolean_as_number',
                                  'number_as_boolean', 'number_as_float', 'order',
                                  'missing_item', 'extra_item', 'missing_field', 'extra_field'])
def test_curator_echo_must_match_every_original_receipt_field_and_order(change):
    from serein.extensions.pipeline import validate
    item = pretranscribed_task()
    echoed = copy.deepcopy(item['request']['curator_image_transcriptions'])
    if change == 'text': echoed[0]['text'] = 'Invented page'
    elif change == 'whitespace': echoed[0]['text'] += ' '
    elif change == 'source': echoed[0]['source_message_id'] = 2
    elif change == 'position': echoed[0]['position'] = 2
    elif change == 'sha256': echoed[0]['sha256'] = 'f' * 64
    elif change == 'evidence_role': echoed[0]['evidence_role'] = 'context_only'
    elif change == 'unreadable': echoed[0]['unreadable'] = True
    elif change == 'boolean_as_number': echoed[0]['unreadable'] = 0
    elif change == 'number_as_boolean': echoed[0]['source_message_id'] = True
    elif change == 'number_as_float': echoed[0]['position'] = 1.0
    elif change == 'order': echoed.reverse()
    elif change == 'missing_item': echoed.pop()
    elif change == 'extra_item': echoed.append(copy.deepcopy(echoed[0]))
    elif change == 'missing_field': echoed[0].pop('sha256')
    elif change == 'extra_field': echoed[0]['input_image'] = 1
    result = {'image_transcriptions': echoed}
    original = copy.deepcopy((item, result))
    assert worker.normalize_curator_transcription_echo(item, result) is result
    with pytest.raises(ValueError, match='每张输入图片必须恰有一份'):
        validate(item['request'], result)
    assert (item, result) == original


@pytest.mark.parametrize('change', ['pretranscribed_false', 'pretranscribed_missing',
                                  'pretranscribed_number', 'transcription_only',
                                  'images_missing', 'images_present', 'other_role', 'role_mismatch'])
def test_curator_echo_normalization_does_not_expand_to_other_task_shapes(change):
    item = pretranscribed_task()
    result = {'image_transcriptions': copy.deepcopy(item['request']['curator_image_transcriptions'])}
    request = item['request']
    if change == 'pretranscribed_false': request['pretranscribed'] = False
    elif change == 'pretranscribed_missing': request.pop('pretranscribed')
    elif change == 'pretranscribed_number': request['pretranscribed'] = 1
    elif change == 'transcription_only': request['transcription_only'] = True
    elif change == 'images_missing': request.pop('images')
    elif change == 'images_present': request['images'] = [{'synthetic': True}]
    elif change == 'other_role': item['role'] = request['role'] = 'event_writer'
    elif change == 'role_mismatch': item['role'] = 'track_router'
    original = copy.deepcopy((item, result))
    assert worker.normalize_curator_transcription_echo(item, result) is result
    assert (item, result) == original
    if change.startswith('pretranscribed_'):
        from serein.extensions.pipeline import validate
        with pytest.raises(ValueError, match='每张输入图片必须恰有一份'):
            validate(request, result)


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


def test_pretranscribed_curator_echo_settles_same_job_without_changing_host_evidence(settings, tmp_path):
    """Real Router/image/Curator/Writer contracts with only the model replaced."""
    from serein.compat.raw_archive import raw_archive
    from serein.core.store import Store
    from serein.extensions import pipeline
    from test_event_handoff import PNG
    second_png = 'data:image/png;base64,' + base64.b64encode(
        base64.b64decode(PNG.split(',', 1)[1]) + b'synthetic-second-page').decode()
    raw_archive(settings).ingest([
        {'source_event_id': 'pages', 'session_id': 'synthetic', 'role': 'user',
         'text': 'Library plan', 'created_at': '2025-01-01T00:00:00Z',
         'metadata': {'attachments': [{'kind': 'image', 'url': url} for url in (PNG, second_png)]}},
        {'source_event_id': 'reply', 'session_id': 'synthetic', 'role': 'assistant',
         'text': 'Agreed to the library plan', 'created_at': '2025-01-01T00:01:00Z'}], source='synthetic')
    captured = {}
    class LocalClient:
        def scheduled_ready(self, timestamp, *, manual=False):
            return True

        def call(self, name, payload):
            if name == 'pipeline_next':
                return asyncio.run(pipeline.advance(settings.database, **payload))
            if payload['job_id'] == captured.get('job_id'):
                assert payload['output'] == captured['decision']
                assert 'image_transcriptions' not in payload['output']
                with Store(settings.database, read_only=True) as store:
                    assert store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?',
                        (payload['job_id'],)).fetchone()[0] == captured['frozen_request']
            return pipeline.submit(settings.database, **payload)

    def run(item):
        request = item['request']
        if request.get('transcription_only'):
            position = request['images'][0]['position']
            return {'image_transcriptions': [{'input_image': 1,
                'text': f'Synthetic page {position}', 'unreadable': False}]}
        result = output_for(item['role'], request)
        if item['role'] == 'event_curator':
            assert request['pretranscribed'] is True and request['images'] == []
            assert len(request['curator_image_transcriptions']) == 2
            captured.update(job_id=item['job_id'], task=item, original_task=copy.deepcopy(item),
                            decision=copy.deepcopy(result))
            result['image_transcriptions'] = copy.deepcopy(request['curator_image_transcriptions'])
            captured.update(raw_result=result, original_result=copy.deepcopy(result))
            with Store(settings.database, read_only=True) as store:
                captured['cache'] = store.conn.execute(
                    'SELECT image_transcription_json FROM raw_events WHERE id=1').fetchone()[0]
                captured['frozen_request'] = store.conn.execute(
                    'SELECT request_json FROM pipeline_jobs WHERE id=?', (item['job_id'],)).fetchone()[0]
        elif item['role'] == 'event_writer':
            assert request['curator_image_transcriptions'] == [
                {**row, 'evidence_role': 'owned'}
                for row in captured['original_task']['request']['curator_image_transcriptions']]
        return result

    agent = worker.Worker(LocalClient(), SimpleNamespace(model=worker.DEFAULT_MODEL, run=run), tmp_path)
    assert agent.run(once=True, include_recent=True) == 0
    assert captured['task'] == captured['original_task']
    assert captured['raw_result'] == captured['original_result']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT image_transcription_json FROM raw_events WHERE id=1').fetchone()[0] == captured['cache']
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0] == 1
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2
        assert json.loads(store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE id=?',
            (captured['job_id'],)).fetchone()[0]) == captured['decision']
    assert not agent.outbox.exists()
