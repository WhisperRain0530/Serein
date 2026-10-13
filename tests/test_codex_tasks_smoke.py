"""Exercise real builders/validators and mock only the fixed subscription transport."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
spec = importlib.util.spec_from_file_location('codex_tasks_smoke', ROOT/'scripts'/'codex_tasks_smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.fixture
def node_bin():
    executable = shutil.which('node')
    if not executable:
        pytest.skip('Node is needed for the production Writer prompt builder')
    return executable


def install_transport(monkeypatch, handler):
    actual_client = httpx.AsyncClient
    def client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(handler)
        return actual_client(*args, **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)


def test_default_all_seven_use_full_prompts_without_network_or_credentials(tmp_path, monkeypatch, node_bin):
    from serein import codex_client
    def forbidden(*args, **kwargs):
        pytest.fail('Dry run must not make a request or read authentication')
    install_transport(monkeypatch, forbidden)
    monkeypatch.setattr(codex_client, '_token', forbidden)
    monkeypatch.setenv('SEREIN_MEMORY_URL', 'https://production.invalid')
    monkeypatch.setenv('SEREIN_CODEX_TASK_TOKEN_FILE', str(tmp_path/'must-not-read'))
    monkeypatch.setenv('SEREIN_CODEX_TASK_STATE_FILE', str(tmp_path/'must-not-read-state'))
    progress = []
    result = smoke.run_smoke(temp_root=tmp_path, node_bin=node_bin, progress=lambda *args: progress.append(args))
    assert result['status'] == 'offline_ready'
    assert result['model_called'] is False
    assert result['semantic_validation'] == 'manual_review_required'
    rows = {row['task']: row for row in result['results']}
    assert set(rows) == smoke.CODEX_TASKS
    calls = [call for row in rows.values() for call in row['requests']]
    assert len(calls) == 9
    assert all(call['request']['messages'] and call['raw_output'] and not call['model_called'] for call in calls)
    assert all(row['status'] == 'offline_fixture_validated' for row in rows.values())
    prompt = rows['writer']['requests'][0]['request']['messages'][0]['content']
    assert all(text in prompt for _, text in smoke.DIALOGUE)
    assert '{user_name}' not in prompt and '{ai_name}' not in prompt
    assert rows['writer']['validator']['publication_status'] == 'not_published'
    assert rows['writer']['requests'][0]['request']['response_format']['json_schema']['schema'] == json.loads(
        (ROOT/'web/codex_agents/narrative_writer/output.schema.json').read_text())
    assert rows['narrative_scout']['validator']['result']['candidate_count'] == 2
    assert len(rows['narrative_scout']['requests']) == 2
    assert rows['operit_tagging']['validator']['result']['body_unchanged']
    assert rows['relations']['validator']['result']['edges'][0]['target_scene_id'] == smoke.SCENE_ID
    assert not rows['dreams']['requests'][0]['raw_output'].startswith('{')
    assert result['production_writes_performed'] == [] and result['temporary_database_deleted']
    assert list(tmp_path.iterdir()) == []
    assert sum(status == 'request_started' for _, status, _ in progress) == 9


def test_run_dispatches_all_nine_through_exact_loopback_with_synthetic_token(tmp_path, monkeypatch, node_bin):
    token = tmp_path/'token'
    token.write_text('synthetic-subscription-token')
    monkeypatch.setenv('SEREIN_CODEX_TASK_TOKEN_FILE', str(token))
    monkeypatch.setenv('SEREIN_MEMORY_URL', 'https://production.invalid')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-use')
    calls = []
    def handler(request):
        assert str(request.url) == 'http://127.0.0.1:8012/complete'
        assert request.headers['Authorization'] == 'Bearer synthetic-subscription-token'
        payload = json.loads(request.content)
        assert payload['timeout_seconds'] == 345
        assert payload['task'] in smoke.CODEX_TASKS
        assert not set(payload) & {'api_key', 'model', 'base_url', 'tools'}
        calls.append(payload)
        value = smoke.fixture_output(payload['task'], payload)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant',
            'content': value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}, 'finish_reason': 'stop'}]})
    install_transport(monkeypatch, handler)
    result = smoke.run_smoke(run=True, temp_root=tmp_path, node_bin=node_bin)
    assert result['status'] == 'completed' and result['model_called']
    assert len(calls) == 9 and {call['task'] for call in calls} == smoke.CODEX_TASKS
    captured = [call for row in result['results'] for call in row['requests']]
    assert [call['messages'] for call in calls] == [call['request']['messages'] for call in captured]
    assert all(row['status'] == 'validated' for row in result['results'])
    assert 'synthetic-subscription-token' not in json.dumps(result)
    assert list(tmp_path.iterdir()) == [token]


def test_real_relation_validator_rejects_unprovided_scene_and_preserves_raw_output(tmp_path, monkeypatch):
    token = tmp_path/'token'
    token.write_text('synthetic-token')
    monkeypatch.setenv('SEREIN_CODEX_TASK_TOKEN_FILE', str(token))
    def handler(request):
        payload = json.loads(request.content)
        value = smoke.fixture_output('relations', payload)
        value['edges'][0]['candidate_scene_id'] = 'unprovided-scene'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)}, 'finish_reason': 'stop'}]})
    install_transport(monkeypatch, handler)
    result = smoke.run_smoke(run=True, tasks=['relations'], temp_root=tmp_path)
    row = result['results'][0]
    assert result['status'] == row['status'] == 'failed'
    assert row['validator']['result']['rejected']
    assert 'unprovided-scene' in row['requests'][0]['raw_output']
    assert row['validator']['writes_performed'] == []
    assert list(tmp_path.iterdir()) == [token]


def test_transport_error_is_fixed_code_and_temp_database_is_removed(tmp_path, monkeypatch):
    token = tmp_path/'token'
    token.write_text('synthetic-token')
    monkeypatch.setenv('SEREIN_CODEX_TASK_TOKEN_FILE', str(token))
    install_transport(monkeypatch, lambda request: httpx.Response(503, text='never-expose-service-detail'))
    result = smoke.run_smoke(run=True, tasks=['arc_linker'], temp_root=tmp_path)
    assert result['status'] == 'failed'
    assert result['results'][0]['requests'][0]['error_code'] == 'codex_task_unavailable'
    assert 'never-expose-service-detail' not in json.dumps(result)
    assert list(tmp_path.iterdir()) == [token]


def test_no_production_model_and_no_database_reuse(tmp_path, monkeypatch):
    with pytest.raises(smoke.SmokeError, match='invalid_task_selection'):
        smoke.run_smoke(tasks=['chat'], temp_root=tmp_path)
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    smoke.seed(tmp_path)
    with pytest.raises(smoke.SmokeError, match='database_not_empty'):
        smoke.seed(tmp_path)
