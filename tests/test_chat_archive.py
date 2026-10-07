import json

import httpx
import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.core.store import Store
from test_public_settings import deployment, configure


def rows(settings):
    with Store(settings.database, read_only=True) as store:
        return [dict(row) for row in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]


def respond(monkeypatch, content='Synthetic final answer', **fields):
    async def complete(*args, **kwargs):
        return {'choices': [{'message': {'role': 'assistant', 'content': content, **fields}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)


def send(client, messages, window='audit-window', **options):
    return client.post('/v1/chat/completions', json={
        'messages': messages, 'serein': {'window_id': window, 'memory': False}, **options})


def test_success_archives_current_pair_and_searchable_ids(deployment, monkeypatch):
    settings, client = deployment
    configure(client).raise_for_status()
    respond(monkeypatch, reasoning_content='hidden reasoning')
    history = [{'role': 'system', 'content': 'secret system'},
               {'role': 'user', 'content': 'Old question'},
               {'role': 'assistant', 'content': 'Old answer'},
               {'role': 'user', 'content': '<proxy_sender name="phone"/>【系统提示】Current question\n'
                '<attachment>private client context</attachment>'}]
    send(client, history).raise_for_status()
    saved = rows(settings)
    assert [(r['role'], r['text']) for r in saved] == [('user', 'Current question'), ('assistant', 'Synthetic final answer')]
    assert all(r['source'] == 'serein_chat' and r['session_id'] == 'audit-window' for r in saved)
    assert all(secret not in json.dumps(saved) for secret in ['secret system', 'hidden reasoning', 'Old question', 'Old answer', 'private client context'])
    found = client.post('/v1/host/messages/search', json={'messageIds': [r['id'] for r in saved]})
    found.raise_for_status()
    assert len(found.json()['items']) == 2
    assert client.post('/api/search-raw', json={'query': 'Current question'}).json()['items']
    from serein.extensions.pipeline import initialize
    initialize(settings.database)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
        payload = json.loads(store.conn.execute('SELECT payload_json FROM injection_debug ORDER BY id DESC LIMIT 1').fetchone()[0])
    assert payload['raw_archive']['message_ids'] == [r['id'] for r in saved]


def test_recall_injection_is_not_original_speech(deployment, monkeypatch):
    settings, client = deployment
    configure(client)
    monkeypatch.setattr('serein.configured_models.memory_ready', lambda _: True)
    monkeypatch.setattr('serein.application.Services.recall', lambda *a, **k: {
        'selected_refs': ['scene:synthetic'], 'context': 'SECRET_RECALLED_BODY', 'cards': []})
    async def complete(model, body, **kwargs):
        assert 'SECRET_RECALLED_BODY' in json.dumps(body)
        return {'choices': [{'message': {'role': 'assistant', 'content': 'Current answer'}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    client.post('/v1/chat/completions', json={'messages': [{'role': 'user', 'content': 'Current question'}],
        'serein': {'window_id': 'injection-window', 'memory': True}}).raise_for_status()
    assert [r['text'] for r in rows(settings)] == ['Current question', 'Current answer']
    assert 'SECRET_RECALLED_BODY' not in json.dumps(rows(settings))


def test_kelivo_conversations_keep_archive_and_recall_cooldown_separate(deployment, monkeypatch):
    from serein.chat_state import recent_deliveries
    settings, client = deployment
    configure(client).raise_for_status()
    monkeypatch.setattr('serein.configured_models.memory_ready', lambda _: True)
    cooldowns = []
    def recall(*args, **options):
        cooldowns.append(options['delivered_ids'])
        return {'context': 'Synthetic remembered source',
                'selected_refs': [f'scene:fixture-{len(cooldowns)}'], 'cards': []}
    monkeypatch.setattr('serein.application.Services.recall', recall)
    respond(monkeypatch)
    for window, question in [('kelivo-a', 'Same first question'),
                             ('kelivo-b', 'Same first question'),
                             ('kelivo-a', 'Next question')]:
        client.post('/v1/chat/completions', headers={'X-Conversation-Id': window}, json={
            'messages': [{'role': 'user', 'content': question}],
            'serein': {'memory': True}}).raise_for_status()
    assert cooldowns == [[], [], ['scene:fixture-1']]
    assert [(row['session_id'], row['role']) for row in rows(settings)] == [
        ('kelivo-a', 'user'), ('kelivo-a', 'assistant'),
        ('kelivo-b', 'user'), ('kelivo-b', 'assistant'),
        ('kelivo-a', 'user'), ('kelivo-a', 'assistant')]
    assert recent_deliveries(settings.database, 'kelivo-a') == ['scene:fixture-3', 'scene:fixture-1']
    assert recent_deliveries(settings.database, 'kelivo-b') == ['scene:fixture-2']
    assert recent_deliveries(settings.database, 'main') == []


def test_retry_restart_tools_and_regeneration(deployment, monkeypatch):
    settings, client = deployment
    configure(client)
    user = {'role': 'user', 'content': 'Look up the synthetic book'}
    tool_call = {'role': 'assistant', 'content': 'I will look it up', 'tool_calls': [
        {'id': 'call-1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]}
    respond(monkeypatch, tool_call['content'], tool_calls=tool_call['tool_calls'])
    send(client, [user]).raise_for_status()
    assert [(r['role'], r['text']) for r in rows(settings)] == [('user', user['content'])]
    continuation = [user, tool_call, {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'private tool data'}]
    respond(monkeypatch)
    send(client, continuation).raise_for_status()
    original = rows(settings)
    reopened = TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization': 'Bearer synthetic'})
    send(reopened, continuation).raise_for_status()
    assert rows(settings) == original
    respond(monkeypatch, 'A regenerated answer')
    send(reopened, [user]).raise_for_status()
    assert [r['role'] for r in rows(settings)] == ['user', 'assistant', 'assistant']
    assert 'private tool data' not in json.dumps(rows(settings))
    # The same words in a later turn/window are new messages, not global text duplicates.
    send(reopened, [user, {'role': 'assistant', 'content': 'A regenerated answer'}, user]).raise_for_status()
    assert len(rows(settings)) == 5
    send(reopened, [user], window='another-window').raise_for_status()
    assert len(rows(settings)) == 7


@pytest.mark.parametrize('ending,expected', [('', 0), ('data: [DONE]\n\n', 2),
    ('data: {"error":{"message":"upstream failed"}}\n\ndata: [DONE]\n\n', 0)])
def test_stream_only_archives_completed_answer(deployment, monkeypatch, ending, expected):
    settings, client = deployment
    configure(client)
    original = httpx.AsyncClient
    raw = 'data: {"choices":[{"index":0,"delta":{"content":"Streamed answer","reasoning_content":"hidden"}}]}\n\n' + ending
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=raw)), **kw))
    send(client, [{'role': 'user', 'content': 'Stream question'}], stream=True).raise_for_status()
    assert len(rows(settings)) == expected
    assert 'hidden' not in json.dumps(rows(settings))


def test_failure_and_empty_output_do_not_invent_raw_reply(deployment, monkeypatch):
    settings, client = deployment
    configure(client)
    async def fail(*args, **kwargs):
        raise httpx.ConnectError('upstream unavailable')
    monkeypatch.setattr('serein.api.chat.complete', fail)
    assert send(client, [{'role': 'user', 'content': 'Failed question'}]).status_code == 502
    respond(monkeypatch, '')
    send(client, [{'role': 'user', 'content': 'Empty question'}]).raise_for_status()
    assert rows(settings) == []


def test_injection_not_archived_and_images_remain_bound(deployment, monkeypatch):
    settings, client = deployment
    configure(client)
    respond(monkeypatch)
    user = {'role': 'user', 'id': 'client-message-1', 'content': [
        {'type': 'text', 'text': '<serein_live_context>secret recalled body</serein_live_context>\n\nCurrent user message:\nPicture question'},
        {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/synthetic.png'}},
        {'type': 'tool_result', 'text': 'secret tool output'}]}
    send(client, [user]).raise_for_status()
    saved = rows(settings)
    assert saved[0]['text'] == 'Picture question'
    metadata = json.loads(saved[0]['metadata_json'])
    assert metadata['attachments'] == [{'kind': 'image', 'url': 'https://example.invalid/synthetic.png'}]
    assert metadata['original_message_id'] == 'client-message-1'
    assert 'secret' not in json.dumps(saved)
    from serein.extensions.pipeline import message, writer_images
    projected = message(saved[0])
    images, missing = writer_images([projected], {projected['id']})
    assert images[0]['url'] == 'https://example.invalid/synthetic.png' and not missing
    # Stable client message IDs work even if earlier history is trimmed/expanded.
    send(client, [{'role': 'user', 'content': 'Earlier'}, user]).raise_for_status()
    assert rows(settings) == saved
    send(client, [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,synthetic'}}]}]).raise_for_status()
    assert rows(settings)[2]['text'] == '[图片]'


def test_archive_failure_visible_without_losing_reply(deployment, monkeypatch):
    settings, client = deployment
    configure(client)
    respond(monkeypatch)
    def fail(*args):
        raise OSError('private storage details')
    monkeypatch.setattr('serein.api.chat.archive_turn', fail)
    response = send(client, [{'role': 'user', 'content': 'Storage failure question'}])
    response.raise_for_status()
    assert response.json()['choices'][0]['message']['content'] == 'Synthetic final answer'
    with Store(settings.database, read_only=True) as store:
        payload = json.loads(store.conn.execute('SELECT payload_json FROM injection_debug ORDER BY id DESC LIMIT 1').fetchone()[0])
    assert payload['request_status'] == 'completed'
    assert payload['raw_archive'] == {'status': 'failed', 'reason': 'OSError'}
