"""Offline transport/CLI tests: synthetic strings and fake subprocesses only."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import codex_event_worker as worker
import codex_task_service as service


def task_body(task='dreams'):
    return {'task': task, 'messages': [{'role': 'system', 'content': '原提示\r\n  不删空白。'},
        {'role': 'user', 'content': '合成材料 <message> 🐈\n'}]}


def ready_service(tmp_path, *, env=None):
    instance = service.TaskService(tmp_path / 'service', 'synthetic-secret',
        environ=env or os.environ, probe=lambda *_: True)
    instance.refresh_login()
    return instance


def fake_cli(tmp_path, monkeypatch, *, output='合成梦境\r\n  原样。\n', sleep=0,
             failure=None, events=None, child=False):
    script = tmp_path / 'fake_cli.py'
    marker = tmp_path / 'started'
    child_pid = tmp_path / 'child-pid'
    child_code = ('import pathlib,time\n'
        f'p=pathlib.Path({str(tmp_path / "child-heartbeat")!r})\n'
        'while True:\n with p.open("ab") as f: f.write(b".")\n time.sleep(.03)\n')
    script.write_text('import sys,json,pathlib,time,subprocess,os\n'
        'prompt=sys.stdin.read()\n'
        + f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n'
        + (f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}]);'
           f'pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))\n' if child else '')
        + f'time.sleep({sleep!r})\n'
        + (f'print({failure!r},file=sys.stderr);sys.exit(1)\n' if failure else '')
        + 'args=sys.argv[1:]\n'
        + 'path=pathlib.Path(args[args.index("--output-last-message")+1])\n'
        + ('path.write_bytes(prompt.encode())\n' if output is None else
           f'path.write_bytes({output.encode()!r})\n')
        + f'events={events or [{"type": "turn.completed"}]!r}\n'
        + 'for event in events: print(json.dumps(event))\n', encoding='utf-8')
    real_command = worker.codex_command
    monkeypatch.setattr(worker, 'codex_command', lambda *args:
                        [sys.executable, str(script)] + real_command(*args)[1:])
    return marker, child_pid


def request(instance, body, *, headers=None, content=None):
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.create_app(instance)),
                                     base_url='http://127.0.0.1:8012') as client:
            return await client.post('/complete', headers=headers if headers is not None else
                {'Authorization': 'Bearer synthetic-secret'},
                **({'content': content} if content is not None else {'json': body}))
    return asyncio.run(call())


@pytest.mark.parametrize('name', sorted(service.TASKS))
def test_all_seven_roles_preserve_original_messages_without_new_rules(name):
    body = task_body(name)
    body['response_format'] = {'type': 'json_object'}
    task, prompt, timeout = service.unpack_request(body)
    assert task == name and timeout == 345
    assert prompt == ('<message role="system">\n原提示\r\n  不删空白。\n</message>\n'
        '<message role="user">\n合成材料 <message> 🐈\n\n</message>\n'
        '<response_format>\n{"type": "json_object"}\n</response_format>')


@pytest.mark.parametrize('patch', [
    {'task': 'chat'}, {'task': 'embedding'}, {'task': 'event_writer'}, {'tools': []},
    {'stream': False}, {'images': []}, {'model': 'another-model'}, {'temperature': 1},
    {'messages': [{'role': 'tool', 'content': 'x'}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'url': 'https://example.invalid'}]}]},
    {'messages': [{'role': 'user', 'content': 'x', 'tool_calls': []}]},
    {'timeout_seconds': 0}, {'timeout_seconds': 346}, {'timeout_seconds': True},
])
def test_unsupported_inputs_never_reach_cli(tmp_path, monkeypatch, patch):
    instance = ready_service(tmp_path)
    monkeypatch.setattr(instance, 'run', lambda *_: pytest.fail('Rejected input reached CLI'))
    response = request(instance, {**task_body(), **patch})
    assert response.status_code == 400 and response.json() == {
        'error': {'code': 'invalid_task_request', 'message': 'invalid_task_request'}}


def test_auth_and_size_limits_do_not_echo_material(tmp_path, monkeypatch):
    instance = ready_service(tmp_path)
    monkeypatch.setattr(instance, 'run', lambda *_: pytest.fail('Rejected input reached CLI'))
    assert request(instance, task_body(), headers={}).status_code == 401
    assert request(instance, task_body(), content=b'x' * (service.MAX_REQUEST_BYTES + 1)).status_code == 413
    response = request(instance, task_body(), content=b'{"messages":"synthetic-secret","messages":[]}')
    assert response.status_code == 400 and 'synthetic-secret' not in response.text


def test_plain_text_and_json_are_returned_verbatim_not_reformatted(tmp_path, monkeypatch, capsys):
    instance = ready_service(tmp_path)
    fake_cli(tmp_path, monkeypatch)
    result = request(instance, task_body())
    assert result.status_code == 200
    assert result.json()['choices'][0]['message']['content'] == '合成梦境\r\n  原样。\n'
    assert result.json()['model'] == 'gpt-5.6-sol'
    assert not list(instance.directory.glob('task-*'))
    assert capsys.readouterr().out == ''
    state = json.loads(instance.status_path.read_text())
    assert state['state'] == 'idle' and state['subscription_authenticated'] is True
    assert set(state) == {'state', 'updated_at', 'model', 'reasoning_effort',
                         'subscription_authenticated', 'last_error', 'retry_at', 'current_task'}
    assert '合成' not in instance.status_path.read_text() and 'synthetic-secret' not in instance.status_path.read_text()


def test_timeout_stops_actual_fake_process_and_removes_temp_files(tmp_path, monkeypatch):
    instance = ready_service(tmp_path)
    marker, _ = fake_cli(tmp_path, monkeypatch, sleep=30)
    response = request(instance, {**task_body(), 'timeout_seconds': .3})
    assert response.status_code == 504 and response.json()['error']['code'] == 'codex_timeout'
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not list(instance.directory.glob('task-*'))


def test_async_cancellation_kills_process_instead_of_abandoning_thread(tmp_path, monkeypatch):
    instance = ready_service(tmp_path)
    marker, _ = fake_cli(tmp_path, monkeypatch, sleep=30)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.create_app(instance)),
                                     base_url='http://127.0.0.1:8012') as client:
            pending = asyncio.create_task(client.post('/complete', json=task_body(),
                headers={'Authorization': 'Bearer synthetic-secret'}))
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(.02)
            assert marker.exists()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    asyncio.run(exercise())
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert instance.pending == 0 and not instance.cancels
    assert not list(instance.directory.glob('task-*'))


def test_http_disconnect_terminates_inflight_cli(tmp_path, monkeypatch):
    instance = ready_service(tmp_path)
    marker, _ = fake_cli(tmp_path, monkeypatch, sleep=30)
    app = service.create_app(instance)
    async def exercise():
        sent = []
        body_sent = False
        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {'type': 'http.request', 'body': json.dumps(task_body()).encode(), 'more_body': False}
            while not marker.exists():
                await asyncio.sleep(.01)
            return {'type': 'http.disconnect'}
        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'scheme': 'http', 'method': 'POST', 'path': '/complete', 'raw_path': b'/complete',
            'query_string': b'', 'root_path': '', 'server': ('127.0.0.1', 8012),
            'client': ('127.0.0.1', 9000),
            'headers': [(b'authorization', b'Bearer synthetic-secret')]}
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        assert next(item for item in sent if item['type'] == 'http.response.start')['status'] == 499
    asyncio.run(exercise())
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert not list(instance.directory.glob('task-*'))


@pytest.mark.skipif(os.name != 'posix', reason='Production container process groups are POSIX')
def test_timeout_also_stops_cli_descendant(tmp_path, monkeypatch):
    instance = ready_service(tmp_path)
    _, child_pid = fake_cli(tmp_path, monkeypatch, sleep=30, child=True)
    result = request(instance, {**task_body(), 'timeout_seconds': .5})
    assert result.status_code == 504 and child_pid.exists()
    heartbeat = tmp_path / 'child-heartbeat'
    assert heartbeat.exists()
    size = heartbeat.stat().st_size
    time.sleep(.15)
    assert heartbeat.stat().st_size == size  # The grandchild stopped with its process group.


@pytest.mark.skipif(os.name != 'posix', reason='Production container uses flock')
def test_event_and_task_processes_share_one_bounded_cli_slot(tmp_path, monkeypatch):
    lock = tmp_path / 'shared.lock'
    instance = ready_service(tmp_path, env={**os.environ, 'SEREIN_CODEX_LOCK_FILE': str(lock)})
    marker, _ = fake_cli(tmp_path, monkeypatch)
    # A second process holds the same OS lock, as the Event worker does.
    holder = subprocess.Popen([sys.executable, '-c',
        'import fcntl,sys,time;f=open(sys.argv[1],"w");'
        'fcntl.flock(f,fcntl.LOCK_EX);print("ready",flush=True);time.sleep(30)', str(lock)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'ready'
        response = request(instance, {**task_body(), 'timeout_seconds': .15})
        assert response.status_code == 504
        assert response.json()['error']['code'] == 'codex_queue_timeout'
        assert not marker.exists()  # No second CLI while the Event slot is held.
    finally:
        holder.terminate(); holder.wait(timeout=2); holder.stdout.close()
    assert request(instance, task_body()).status_code == 200


def test_quota_backoff_is_shared_with_event_and_survives_service_restart(tmp_path, monkeypatch):
    lock = tmp_path / 'shared.lock'
    env = {**os.environ, 'SEREIN_CODEX_LOCK_FILE': str(lock)}
    instance = ready_service(tmp_path, env=env)
    fake_cli(tmp_path, monkeypatch, failure='usage limit synthetic-secret/private-source')
    response = request(instance, task_body())
    assert response.status_code == 503 and response.json()['error']['code'] == 'codex_usage_limit'
    assert int(response.headers['retry-after']) >= 3600 and 'private-source' not in response.text
    with pytest.raises(worker.WorkerError, match='codex_usage_limit'):
        with worker.cli_execution(lock):
            pytest.fail('Event invocation must wait for shared cooldown')
    restored = ready_service(tmp_path, env=env)
    monkeypatch.setattr(service, 'run_codex_text', lambda *_args, **_kwargs:
                        pytest.fail('Cooldown invoked a model'))
    second = request(restored, task_body())
    assert second.status_code == 503 and second.json()['error']['code'] == 'codex_usage_limit'
    assert 'private-source' not in restored.status_path.read_text()


def test_login_probe_has_no_model_or_api_key_and_no_account_output(tmp_path, monkeypatch, capsys):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b'Logged in using ChatGPT synthetic@example.invalid', b'')
    monkeypatch.setattr(service.subprocess, 'run', run)
    assert service.authenticated('codex', {'CODEX_HOME': str(tmp_path),
        'OPENAI_API_KEY': 'synthetic-secret', 'SEREIN_AGENT_TOKEN': 'synthetic-secret'})
    command, options = calls[0]
    assert command[-2:] == ['login', 'status'] and 'exec' not in command
    assert set(options['env']) == {'CODEX_HOME'}
    assert capsys.readouterr().out == ''


def test_status_refresh_does_not_call_model_or_publish_raw_login_error(tmp_path, monkeypatch):
    instance = ready_service(tmp_path)
    monkeypatch.setattr(instance, 'probe', lambda *_: False)
    instance.refresh_login()
    state = json.loads(instance.status_path.read_text())
    assert state['subscription_authenticated'] is False and state['last_error'] == 'codex_login_required'
    monkeypatch.setattr(instance, 'probe', lambda *_: True)
    instance.refresh_login()
    assert json.loads(instance.status_path.read_text())['state'] == 'idle'
