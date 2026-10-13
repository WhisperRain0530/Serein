import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.codex_config import (CODEX_MODEL_ID, CODEX_TASKS, codex_model,
                                 codex_task_status, codex_tasks_enabled, is_codex_model)
from serein.config import Settings
from serein.deployment import configured_models, read_settings, task_model


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.delenv('SEREIN_CODEX_TASKS_ENABLED', raising=False)
    monkeypatch.delenv('SEREIN_CODEX_TASK_STATE_FILE', raising=False)
    settings = Settings(tmp_path/'memory.db', writable=True)
    initialize(settings)
    return settings, TestClient(create_app(settings, token='synthetic', live=True),
                                headers={'Authorization': 'Bearer synthetic'})


@pytest.mark.parametrize('value,enabled', [('1',True),('true',True),('yes',True),('TRUE',True),('0',False),('',False)])
def test_subscription_gate_matches_runtime(monkeypatch,value,enabled):
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED',value)
    assert codex_tasks_enabled() is enabled


def test_internal_model_is_opt_in_and_keeps_feature_switches(deployment, monkeypatch):
    settings, client = deployment
    before = client.get('/v1/settings').json()
    assert before['internal_models'] == []
    assert before['codex_subscription']['state'] == 'disabled'
    assert client.patch('/v1/settings', json={'assignments': {'writer': CODEX_MODEL_ID}}).status_code == 400
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    response = client.patch('/v1/settings', json={'assignments': {task: CODEX_MODEL_ID for task in CODEX_TASKS}})
    assert response.status_code == 200, response.text
    state = response.json()
    assert len(state['internal_models']) == 1
    assert state['available_models'] == [] and state['upstreams'] == [] and state['models'] == []
    assert state['features'] == before['features']
    assert state['upstream']['writer_enabled'] is False
    assert configured_models(read_settings(settings.database)) == []
    for task in CODEX_TASKS:
        model = task_model(settings.database, task)
        assert is_codex_model(model) and model['task'] == task
        assert model['model'] == 'gpt-5.6-sol' and model['request_timeout_seconds'] == 360
    assert task_model(settings.database, 'anti_retreat')['task'] == 'persona'
    assert client.patch('/v1/settings', json={'features': {'narrative_tools': True},
        'upstream': {'writer_enabled': True}}).json()['upstream']['writer_enabled'] is False
    # A temporarily disabled service must not block unrelated settings edits.
    monkeypatch.delenv('SEREIN_CODEX_TASKS_ENABLED')
    assert client.patch('/v1/settings', json={'clock': {'timezone': 'UTC'}}).status_code == 200
    with pytest.raises(ValueError, match='not enabled'):
        task_model(settings.database, 'writer')
    assert client.patch('/v1/settings', json={'assignments': {'writer': ''}}).status_code == 200


@pytest.mark.parametrize('task', ['chat', 'embedding', 'reranker', 'image_transcription',
                                 'track_router', 'event_curator', 'event_writer', 'event_pipeline'])
def test_reserved_model_cannot_be_assigned_to_other_tasks(deployment, monkeypatch, task):
    settings, client = deployment
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    response = client.patch('/v1/settings', json={'assignments': {task: CODEX_MODEL_ID}})
    assert response.status_code == 400
    assert not read_settings(settings.database)['assignments'].get(task)
    with pytest.raises(ValueError):
        codex_model(task)


def test_reserved_model_never_enters_chat_catalog_or_external_routes(deployment, monkeypatch):
    settings, client = deployment
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    upstream = {'name': 'Synthetic', 'base_url': 'https://synthetic.invalid/v1',
                'models': [{'id': 'external', 'upstream_model': 'synthetic-model'}]}
    response = client.patch('/v1/settings', json={'upstreams': [upstream], 'assignments': {'writer': CODEX_MODEL_ID}})
    assert response.status_code == 200
    assert [item['id'] for item in client.get('/v1/models').json()['data']] == ['Synthetic/synthetic-model']
    with pytest.raises(ValueError, match='not available'):
        task_model(settings.database, 'chat', requested=CODEX_MODEL_ID)
    assert client.post('/v1/chat/completions', json={'model': CODEX_MODEL_ID,
        'messages': [{'role': 'user', 'content': 'synthetic'}]}).status_code == 400
    upstream['models'][0]['id'] = CODEX_MODEL_ID
    assert client.patch('/v1/settings', json={'upstreams': [upstream]}).status_code == 400
    forged = {'id': CODEX_MODEL_ID, 'label': 'Forged', 'model': 'synthetic', 'base_url': 'https://synthetic.invalid/v1'}
    assert client.patch('/v1/settings', json={'models': [forged]}).status_code == 400
    assert client.patch('/v1/settings', json={'internal_models': [codex_model()]}).status_code == 422


def test_status_is_bounded_redacted_and_never_claims_stale_login(tmp_path, monkeypatch):
    path = tmp_path/'status.json'
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    monkeypatch.setenv('SEREIN_CODEX_TASK_STATE_FILE', str(path))
    assert codex_task_status(now=1000)['online'] is False
    source = {'state': 'working', 'updated_at': datetime.fromtimestamp(1000, timezone.utc).isoformat(),
              'model': 'gpt-5.6-sol', 'reasoning_effort': 'high', 'subscription_authenticated': True,
              'current_task': 'writer', 'last_error': '', 'retry_at': 0,
              'access_token': 'synthetic-secret', 'prompt': 'synthetic-private-text', 'account': 'synthetic-account'}
    path.write_text(json.dumps(source))
    current = codex_task_status(now=1010)
    assert current['online'] and current['subscription_authenticated'] and not current['stale']
    assert not {'access_token', 'prompt', 'account'} & current.keys()
    stale = codex_task_status(now=1100)
    assert stale['state'] == 'stale' and not stale['online'] and not stale['subscription_authenticated']
    assert codex_task_status(now=900)['stale']  # Do not trust future timestamps.
    source.update(state=[], current_task=['writer'], last_error='synthetic-private-error', model='synthetic-private-model')
    path.write_text(json.dumps(source))
    invalid = codex_task_status(now=1010)
    assert invalid['state'] == 'unavailable' and invalid['current_task'] == ''
    assert 'synthetic-private' not in json.dumps(invalid)
    path.write_text('x'*17000)
    assert codex_task_status(now=1010)['state'] == 'unavailable'
    path.unlink()
    target = tmp_path/'other.json'
    target.write_text(json.dumps(source))
    path.symlink_to(target)
    assert codex_task_status(now=1010)['state'] == 'unavailable'
