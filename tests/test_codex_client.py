"""Offline subscription dispatch contracts; no model calls or real credentials."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from serein import codex_client, model_runtime
from serein.codex_config import CODEX_MODEL_ID, codex_model
from test_public_settings import deployment
from test_live_clients import live


def response(content):
    return {'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]}


def transport(monkeypatch, tmp_path, handler):
    token = tmp_path / 'synthetic-token'
    token.write_text('synthetic-subscription-secret', encoding='utf-8')
    monkeypatch.setenv('SEREIN_CODEX_TASK_TOKEN_FILE', str(token))
    original = httpx.AsyncClient
    options = []
    def create(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(codex_client.httpx, 'AsyncClient', create)
    return options


@pytest.mark.parametrize('task,contract,content', [
    ('dreams', None, '  我走进雨里。\r\n  '),
    ('writer', {'type': 'json_schema', 'json_schema': {'name': 'narrative_preview', 'strict': True,
        'schema': {'type': 'object', 'properties': {'body': {'type': 'string'}}}}}, '{"body":"原话"}'),
])
def test_dispatch_preserves_messages_contract_and_raw_result(monkeypatch, tmp_path, task, contract, content):
    messages = [{'role': 'system', 'content': '作者规则原文\r\n  不要改写。'},
                {'role': 'user', 'content': '  合成材料：{"原话":"保留"}\n'}]
    calls = []
    def handle(request):
        calls.append(request)
        assert str(request.url) == 'http://127.0.0.1:8012/complete'
        assert request.headers['Authorization'] == 'Bearer synthetic-subscription-secret'
        return httpx.Response(200, json=response(content))
    options = transport(monkeypatch, tmp_path, handle)
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid')
    monkeypatch.setattr(model_runtime, 'request_for', lambda *a, **k: pytest.fail('Internal calls must not use API transport'))
    payload = {'messages': messages, 'temperature': .85, 'max_tokens': 900}
    if contract:
        payload['response_format'] = contract
    before = json.loads(json.dumps(payload))
    result = asyncio.run(model_runtime.complete(codex_model(task), payload))
    assert result['choices'][0]['message']['content'] == content
    sent = json.loads(calls[0].content)
    assert sent == {'task': task, 'messages': messages, 'timeout_seconds': 345,
                    **({'response_format': contract} if contract else {})}
    assert payload == before
    assert options[0]['trust_env'] is False and options[0]['follow_redirects'] is False
    assert options[0]['timeout'].read == 360


@pytest.mark.parametrize('status', [302, 401, 429, 503, 504])
def test_errors_do_not_follow_redirect_retry_or_expose_body(monkeypatch, tmp_path, status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, text='private source and synthetic-subscription-secret',
                              headers={'Location': 'https://foreign.invalid/steal'})
    transport(monkeypatch, tmp_path, handle)
    with pytest.raises(codex_client.CodexTaskError) as caught:
        asyncio.run(codex_client.complete(codex_model('dreams'), {'messages': [{'role': 'user', 'content': '合成'}]}))
    assert len(calls) == 1
    assert caught.value.status_code == status
    assert 'private' not in str(caught.value) and 'secret' not in str(caught.value)


@pytest.mark.parametrize('payload', [
    {'messages': [], 'stream': False},
    {'messages': [{'role': 'user', 'content': '合成'}], 'stream': True},
    {'messages': [{'role': 'user', 'content': '合成'}], 'tools': [{'type': 'function'}]},
])
def test_unsupported_requests_never_open_transport(monkeypatch, payload):
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **k: pytest.fail('Rejected before transport'))
    with pytest.raises(codex_client.CodexTaskError):
        asyncio.run(codex_client.complete(codex_model('persona'), payload))
    with pytest.raises(codex_client.CodexTaskError, match='not_allowed'):
        asyncio.run(codex_client.complete({**codex_model(), 'task': 'chat'}, payload))


def test_cancellation_reaches_inflight_http_request(monkeypatch, tmp_path):
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def handle(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        transport(monkeypatch, tmp_path, handle)
        task = asyncio.create_task(codex_client.complete(codex_model('dreams'),
            {'messages': [{'role': 'user', 'content': '合成'}]}))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
    asyncio.run(run())


@pytest.mark.parametrize('internal,expected', [(False, 4), (True, 360)])
def test_task_client_only_overrides_internal_short_budget(monkeypatch, internal, expected):
    model = codex_model('persona') if internal else {'model': 'synthetic-api', 'base_url': 'https://api.invalid'}
    monkeypatch.setattr(model_runtime, 'task_model', lambda *a: model)
    budgets, calls = [], []
    async def waiting(awaitable, timeout):
        budgets.append(timeout)
        return await awaitable
    async def complete(selected, payload):
        calls.append(payload)
        return response('{"signal":false}')
    monkeypatch.setattr(asyncio, 'wait_for', waiting)
    monkeypatch.setattr(model_runtime, 'complete', complete)
    result = asyncio.run(model_runtime.TaskClient('unused', 'persona').create(
        messages=[{'role': 'system', 'content': '完整规则'}], timeout=4,
        response_format={'type': 'json_object'}))
    assert result.choices[0].message.content == '{"signal":false}'
    assert budgets == [expected] and 'timeout' not in calls[0]
    assert calls[0]['messages'] == [{'role': 'system', 'content': '完整规则'}]


def test_theme_internal_path_keeps_original_prompt_and_json_parser(monkeypatch):
    from serein.compat.narrative_theme import model_json
    calls = []
    async def complete(model, payload):
        calls.append(payload)
        return response('{"terms":["雨声"]}')
    monkeypatch.setattr(model_runtime, 'complete', complete)
    async def forbidden(*args, **kwargs):
        pytest.fail('Internal theme must not use its external HTTP client')
    result = asyncio.run(model_json(SimpleNamespace(post=forbidden), codex_model('narrative_scout'),
                                    '完整作者规则', {'theme': '雨声'}))
    assert result == {'terms': ['雨声']}
    assert calls[0]['messages'] == [{'role': 'system', 'content': '完整作者规则'},
                                    {'role': 'user', 'content': '{"theme": "雨声"}'}]


def test_internal_writer_retains_json_schema_and_preview_contract(deployment, monkeypatch):
    from serein.deployment import save_settings
    settings, client = deployment
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    save_settings(settings.database, {'assignments': {'writer': CODEX_MODEL_ID}, 'upstream': {'writer_enabled': True}})
    calls = []
    async def complete(model, payload):
        calls.append((model, payload))
        return response('{"body":"合成正文"}')
    monkeypatch.setattr('serein.api.chat.complete', complete)
    schema = {'type': 'object', 'properties': {'body': {'type': 'string'}}}
    result = client.post('/v1/models/writer', json={'prompt': '完整原提示', 'output_schema': schema})
    assert result.status_code == 200 and result.json() == {'result': {'body': '合成正文'}}
    assert calls[0][0]['task'] == 'writer' and calls[0][0]['request_timeout_seconds'] == 360
    assert calls[0][1] == {'messages': [{'role': 'user', 'content': '完整原提示'}],
                          'response_format': {'type': 'json_schema', 'json_schema': {
                              'name': 'narrative_preview', 'strict': True, 'schema': schema}}}


@pytest.mark.parametrize('internal,expected', [(False, [60, 120]), (True, [390, 360])])
def test_scene_budget_encloses_task_client_without_changing_parser(monkeypatch, tmp_path, internal, expected):
    from serein.compat.germany.scene_linker import SceneLinker
    model = codex_model('relations') if internal else {'model': 'synthetic-api', 'base_url': 'https://api.invalid'}
    monkeypatch.setattr(model_runtime, 'task_model', lambda *a: model)
    monkeypatch.setattr('serein.deployment.task_model', lambda *a: model)
    budgets = []
    async def waiting(awaitable, timeout):
        budgets.append(timeout)
        return await awaitable
    async def complete(*args):
        return response('{"edges":[]}')
    monkeypatch.setattr(asyncio, 'wait_for', waiting)
    monkeypatch.setattr(model_runtime, 'complete', complete)
    linker = SceneLinker({'serein_database': str(tmp_path / 'synthetic.db'), 'scene_linker': {}})
    provider = {'name': 'synthetic', 'model': model['model'], 'client': model_runtime.TaskClient('unused', 'relations')}
    assert asyncio.run(linker._call_provider(provider, {'new_scene': {'scene_id': 'synthetic'}})) == {'edges': []}
    assert budgets == expected


@pytest.mark.parametrize('internal,late', [(False, False), (True, False), (True, True)])
def test_retreat_extended_lease_preserves_next_turn_only_rule(deployment, monkeypatch, internal, late):
    from serein import chat_features
    from serein.core.store import Store, encode
    from serein.deployment import read_settings, save_settings
    settings, _ = deployment
    model = codex_model('persona') if internal else {'model': 'synthetic-api'}
    monkeypatch.setattr(chat_features, 'task_model', lambda *a: model)
    save_settings(settings.database, {'features': {'anti_retreat': True}})
    with Store(settings.database) as store:
        store.conn.execute('INSERT INTO background_state VALUES (?,?)', ('feature_round:w', encode(1)))
    budgets = []
    async def waiting(awaitable, timeout):
        budgets.append(timeout)
        return await awaitable
    async def detect(*args):
        with Store(settings.database) as store:
            state = json.loads(store.conn.execute('SELECT value_json FROM background_state WHERE name=?', ('anti_retreat:w',)).fetchone()[0])
            assert (419 if internal else 119) <= state['running_until'] - chat_features.time.time() <= (420 if internal else 120)
            if late:
                store.conn.execute('UPDATE background_state SET value_json=? WHERE name=?', (encode(2), 'feature_round:w'))
        return {'triggered': True, 'nudge': '这一轮可能有合成误解'}
    monkeypatch.setattr(asyncio, 'wait_for', waiting)
    monkeypatch.setattr(chat_features, 'persona_engine', lambda *a: SimpleNamespace(detect_conflict_nudge=detect))
    asyncio.run(chat_features.evaluate_retreat(settings.database, 'w', '合成问题', '合成回复', [], 1, read_settings(settings.database)))
    with Store(settings.database) as store:
        saved = json.loads(store.conn.execute('SELECT value_json FROM background_state WHERE name=?', ('anti_retreat:w',)).fetchone()[0])
    assert budgets == [390 if internal else 90]
    assert saved['running_until'] == 0
    assert bool(saved['pending']) is not late
    if not late:
        assert saved['pending']['round'] == 2 and saved['pending']['nudge'] == '上一轮可能有合成误解'


@pytest.mark.parametrize('enabled', [False, True])
def test_background_jobs_start_internal_assignments_only_when_enabled(deployment, monkeypatch, enabled):
    from serein import model_jobs, import_tagging, arc_linking
    from serein.compat import jobs
    from serein.deployment import save_settings
    settings, _ = deployment
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    save_settings(settings.database, {'assignments': {task: CODEX_MODEL_ID for task in ('relations', 'dreams', 'operit_tagging', 'arc_linker')}})
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1' if enabled else '0')
    observed = set()
    class Background:
        def __init__(self, settings, *, features):
            self.features = features
        async def run(self):
            observed.update(self.features)
            await asyncio.Future()
    async def tag(*args):
        observed.add('operit_tagging')
        await asyncio.Future()
    async def arc(*args):
        observed.add('arc_linker')
        await asyncio.Future()
    monkeypatch.setattr(jobs, 'BackgroundJobs', Background)
    monkeypatch.setattr(import_tagging, 'run', tag)
    monkeypatch.setattr(arc_linking, 'run', arc)
    async def check():
        task = asyncio.create_task(model_jobs.run(SimpleNamespace(settings=settings, enabled_extensions=set())))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())
    assert observed == ({'relations', 'dreams', 'operit_tagging', 'arc_linker', 'narrative_revision'} if enabled else {'narrative_revision'})


def test_disabled_subscription_does_not_block_unrelated_jobs_or_use_legacy_models(deployment, monkeypatch, tmp_path):
    from dataclasses import replace
    from serein.compat.jobs import BackgroundJobs
    from serein.compat.background import germany_config
    from serein.deployment import save_settings
    settings, _ = deployment
    legacy = tmp_path / 'synthetic.yml'
    legacy.write_text('scene_linker:\n  enabled: true\n  models:\n    - model: legacy\n      api_key: synthetic\ndream:\n  enabled: true\n  model: legacy\n  base_url: https://api.invalid\n  api_key: synthetic\n', encoding='utf-8')
    settings = replace(settings, background={'germany_config_file': str(legacy)})
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    save_settings(settings.database, {'assignments': {'relations': CODEX_MODEL_ID, 'dreams': CODEX_MODEL_ID}})
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '0')
    config = germany_config(settings)
    assert config['scene_linker']['enabled'] is False and config['scene_linker']['models'] == []
    assert config['dream']['enabled'] is False and config['dream']['api_key'] == ''
    revision = BackgroundJobs(settings, features={'narrative_revision'})
    assert revision.scout is not None and revision.linker is None and revision.dreams is None
    save_settings(settings.database, {'models': [{'id': 'api-dream', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:9/v1'}],
                                      'assignments': {'dreams': 'api-dream'}})
    dream = BackgroundJobs(settings, features={'dreams'})
    assert dream.dreams is not None and dream.linker is None


@pytest.mark.parametrize('include_external', [False, True])
def test_disabled_scout_still_executes_revision_scan(live, monkeypatch, include_external):
    from serein import deployment as configuration
    from serein.compat.scout import Scout
    from serein.core.store import Store
    from test_live_narratives import seed
    settings, client = live
    seed(client, settings)
    with Store(settings.database) as store:
        store.create('synthetic-unbound-scene', 'scene', '未收录的雨声', '我们再次在窗边听雨。',
                     metadata={'object_kind': 'scene'})
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    configuration.save_settings(settings.database, {
        'assignments': {'narrative_scout': CODEX_MODEL_ID},
        'features': {'narrative_nightly_organize': True}})
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '0')
    # Direct requests still reject a disabled subscription; optional scans skip it.
    original = configuration.task_model
    with pytest.raises(ValueError, match='not enabled'):
        original(settings.database, 'narrative_scout')
    def selected(database, task, **kwargs):
        assert task != 'narrative_scout', 'Program scan must not resolve its disabled model'
        return original(database, task, **kwargs)
    monkeypatch.setattr(configuration, 'task_model', selected)
    scout = Scout(settings)
    report = asyncio.run(scout._scan_narrative_revision_inbox(include_external=include_external))
    assert report['status'] == 'ok' and report['checked_rolls'] == 1
    assert report['external_scout_status'] == ('unavailable' if include_external else 'disabled')
    assert report['external_model'] == '' and report['external_base_url'] == ''
    assert scout.inbox.scan_metadata()['checked_rolls'] == 1


def test_disabled_persona_history_remains_readable_and_after_reply_skips(deployment, monkeypatch):
    from serein.chat_features import after_reply
    from serein.compat.persona_engine import PersonaStateEngine
    from serein.core.store import Store
    from serein.deployment import save_settings
    settings, client = deployment
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    save_settings(settings.database, {'assignments': {'persona': CODEX_MODEL_ID},
                                      'features': {'persona': True, 'anti_retreat': True}})
    engine = PersonaStateEngine({'serein_database': settings.database})
    engine._ensure_session_state('synthetic-history', engine._now())
    with Store(settings.database) as store:
        store.conn.execute("UPDATE persona_session_state SET inner_thought=? WHERE session_id=?",
                           ('合成的既有心绪', 'synthetic-history'))
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '0')
    result = client.get('/v1/companion/persona?session_id=synthetic-history')
    assert result.status_code == 200 and result.json()['model_configured'] is False
    assert result.json()['session']['inner_thought'] == '合成的既有心绪'
    monkeypatch.setattr('serein.chat_features.persona_engine', lambda *a: pytest.fail('Disabled evaluations must not start'))
    asyncio.run(after_reply(settings.database, 'synthetic-history', '合成问题', '合成回复', [], round_id=3))


def test_disabled_tagger_preserves_cue_fingerprint_without_callable_fallback(deployment, monkeypatch):
    from serein.deployment import save_settings
    from serein.recall.legacy_indexes import cue_index
    settings, _ = deployment
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    save_settings(settings.database, {'assignments': {'operit_tagging': CODEX_MODEL_ID}})
    profile = {'model': 'synthetic-embedding', 'document_instruction': ''}
    active = cue_index(settings, profile=profile)
    assert callable(active.binder.bind)
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '0')
    stopped = cue_index(settings, profile=profile)
    assert stopped.binder.model == active.binder.model and not hasattr(stopped.binder, 'bind')
    with pytest.raises(ValueError, match='not enabled'):
        cue_index(settings, writable=True, profile=profile)


def test_disabled_tagger_does_not_repeat_index_outbox_or_fall_back(deployment, monkeypatch):
    from dataclasses import replace
    from serein.core.store import Store
    from serein.deployment import save_settings
    from serein.recall import worker
    settings, _ = deployment
    settings = replace(settings, embedding={'endpoint': 'https://api.invalid/embeddings'},
                       background={'germany_config_file': 'synthetic.yml'},
                       recall={'germany_policy_file': 'synthetic-policy.json'})
    with Store(settings.database) as store:
        store.create('synthetic-pending', 'scene', '合成标题', '合成正文')
        store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', ('synthetic-pending',))
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '1')
    save_settings(settings.database, {'assignments': {'operit_tagging': CODEX_MODEL_ID}})
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '0')
    calls = []
    monkeypatch.setattr('serein.configured_models.effective_settings', lambda s: s)
    monkeypatch.setattr('serein.recall.passage_layouts.prepare_layouts', lambda *a: None)
    monkeypatch.setattr(worker, 'prepare_passages', lambda *a, **k: None)
    monkeypatch.setattr(worker, 'fill_vectors', lambda *a, **k: calls.append('vectors'))
    monkeypatch.setattr(worker, 'rebuild_entities', lambda *a, **k: None)
    monkeypatch.setattr('serein.compat.entity_scope.update_scope', lambda *a: [])
    monkeypatch.setattr(worker.RecallPolicy, 'from_config', lambda *a: SimpleNamespace(passages_enabled=False))
    monkeypatch.setattr('serein.recall.legacy_indexes.refresh', lambda *a: pytest.fail('No legacy model fallback'))
    assert worker.update_pending(settings)['status'] == 'updated'
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM index_outbox').fetchone()[0] == 0
    assert worker.update_pending(settings)['status'] == 'current' and calls == ['vectors']


def test_optional_model_availability_does_not_swallow_external_errors(monkeypatch):
    from serein import deployment as configuration
    monkeypatch.setenv('SEREIN_CODEX_TASKS_ENABLED', '0')
    monkeypatch.setattr(configuration, 'read_settings', lambda *a: {'assignments': {'persona': 'missing-api'}})
    def unavailable(*args):
        raise ValueError('The selected model is not configured')
    monkeypatch.setattr(configuration, 'task_model', unavailable)
    with pytest.raises(ValueError, match='not configured'):
        configuration.available_task_model('unused', 'persona')
