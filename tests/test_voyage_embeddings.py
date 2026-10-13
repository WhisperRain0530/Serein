"""Native Voyage contracts, using only synthetic local data and mock HTTP."""
import hashlib
import json
import sqlite3

import pytest

httpx = pytest.importorskip('httpx')

from serein.adapters.embedding import EmbeddingClient, embedding_profile_contract
from serein.bootstrap import initialize
from serein.config import Settings
from serein.configured_models import model_index, prepare_selected
from serein.core import Store
from serein.deployment import save_settings
from serein.recall.index import build_index
from serein.semantic_setup import prepare


HOSTS = ('api.voyageai.com', 'ai.mongodb.com', 'us.ai.mongodb.com', 'eu.ai.mongodb.com')


def make_client(tmp_path, host='ai.mongodb.com', *, contract=True):
    database, index = tmp_path / 'memory.db', tmp_path / 'index.db'
    with Store(database):
        pass
    build_index(database, index)
    endpoint = f'https://{host}/v1/embeddings'
    profile = dict(model='voyage-4-large', provider_host=host, document_instruction='',
                   query_instruction='', max_chars=12000)
    if contract:
        profile.update(embedding_profile_contract(endpoint))
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2048')")
    return database, index, endpoint


def response_for(inputs):
    texts = inputs if isinstance(inputs, list) else [inputs]
    return {'object': 'list', 'model': 'voyage-4-large', 'usage': {'total_tokens': 2},
            'data': [{'object': 'embedding', 'index': position,
                      'embedding': [0.] * position + [1.] + [0.] * (2047 - position)}
                     for position in reversed(range(len(texts)))]}


@pytest.mark.parametrize('host', HOSTS)
def test_native_query_and_documents_use_distinct_types_and_explicit_float_dimension(tmp_path, host):
    args = make_client(tmp_path, host)
    client = EmbeddingClient(*args, api_key='synthetic-key')
    seen = []

    def handle(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.headers['authorization'] == 'Bearer synthetic-key'
        assert payload['output_dimension'] == 2048
        assert payload['output_dtype'] == 'float'
        assert payload['truncation'] is False
        assert 'encoding_format' not in payload
        return httpx.Response(200, json=response_for(payload['input']))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        query = client.query('图书馆改到周日了吗？', client=http)
        documents = client.documents(['约定改到周日。', '小雨的朋友对花生过敏。'], client=http)
    assert [row['input_type'] for row in seen] == ['query', 'document']
    assert seen[0]['input'] == '图书馆改到周日了吗？'
    assert seen[1]['input'] == ['约定改到周日。', '小雨的朋友对花生过敏。']
    assert query['embedding'] == [1.] + [0.] * 2047
    assert documents == [[1.] + [0.] * 2047, [0., 1.] + [0.] * 2046]


@pytest.mark.parametrize('bad_result', [
    {'data': [{'index': 0, 'embedding': [1.] * 2048}]},
    {'model': 'other', 'data': [{'index': 0, 'embedding': [1.] * 2048}]},
    {'model': 'voyage-4-large', 'data': []},
    {'model': 'voyage-4-large', 'data': [{'embedding': [1.] * 2048}]},
    {'model': 'voyage-4-large', 'data': [{'index': True, 'embedding': [1.] * 2048}]},
    {'model': 'voyage-4-large', 'data': [{'index': 1, 'embedding': [1.] * 2048}]},
    {'model': 'voyage-4-large', 'data': [{'index': 0, 'embedding': [1.] * 1024}]},
    {'model': 'voyage-4-large', 'data': [{'index': 0, 'embedding': 'encoded-vector'}]},
    {'model': 'voyage-4-large', 'data': [{'index': 0, 'embedding': [float('nan')] * 2048}]},
])
def test_native_response_identity_positions_count_and_dimension_are_required(tmp_path, bad_result):
    client = EmbeddingClient(*make_client(tmp_path), api_key='synthetic-key')
    response = httpx.Response(200, content=json.dumps(bad_result))
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(ValueError):
            client.query('合成查询', client=http)


def test_native_batch_rejects_duplicate_positions(tmp_path):
    client = EmbeddingClient(*make_client(tmp_path), api_key='synthetic-key')
    result = response_for(['甲', '乙'])
    result['data'][1]['index'] = result['data'][0]['index']
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result))) as http:
        with pytest.raises(ValueError, match='positions'):
            client.documents(['甲', '乙'], client=http)


def test_native_legacy_profile_requires_separate_preparation_before_any_request(tmp_path):
    with pytest.raises(ValueError, match='input contract changed'):
        EmbeddingClient(*make_client(tmp_path, contract=False), api_key='synthetic-key')


@pytest.mark.parametrize('host', ('embedding.example', 'ai.mongodb.com.example', 'custom.voyageai.com'))
def test_unlisted_hosts_keep_the_openai_contract(tmp_path, host):
    client = EmbeddingClient(*make_client(tmp_path, host), api_key='synthetic-key')
    def handle(request):
        assert json.loads(request.content) == {
            'model': 'voyage-4-large', 'input': '查询', 'encoding_format': 'float'}
        result = response_for('查询')
        del result['data'][0]['index']  # Preserved single-item compatibility.
        return httpx.Response(200, json=result)
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        assert len(client.query('查询', client=http)['embedding']) == 2048


@pytest.mark.parametrize('host', (*HOSTS, 'embedding.example'))
def test_native_index_path_is_distinct_from_old_untyped_vectors_only_for_native_hosts(tmp_path, host):
    settings = Settings(tmp_path / 'memory.db')
    model = dict(id='embedding', model='voyage-4-large', base_url=f'https://{host}/v1',
                 dimension=2048, query_instruction='', document_instruction='')
    old_hash = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()[:24]
    old_index = tmp_path / 'model-indexes' / old_hash
    assert (model_index(settings, model) != old_index) is (host in HOSTS)


def test_native_preparation_uses_document_probe_query_routes_and_preserves_body_contract(tmp_path, monkeypatch):
    settings = Settings(tmp_path / 'memory.db', tmp_path / 'unused.db', writable=True)
    initialize(settings)
    with Store(settings.database) as store:
        store.create('synthetic-event', 'event', '图书馆约定', '约定已经从周六改到周日。')
    model = dict(id='voyage', model='voyage-4-large', label='Voyage', protocol='openai',
                 base_url='https://ai.mongodb.com/v1', dimension=2048, api_key='synthetic-key')
    save_settings(settings.database, {'models': [model], 'assignments': {'embedding': 'voyage'}})
    real_client, seen = httpx.Client, []
    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body['output_dimension'] == 2048
        assert body['output_dtype'] == 'float' and body['truncation'] is False
        assert 'encoding_format' not in body
        return httpx.Response(200, json=response_for(body['input']))
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    result = prepare_selected(settings)
    assert result['status'] == 'ready' and result['dimension'] == 2048
    assert seen[0]['input'] == 'dimension probe' and seen[0]['input_type'] == 'document'
    assert any(row['input_type'] == 'query' for row in seen[1:])
    assert all(row['input_type'] == 'query' for row in seen[1:] if isinstance(row['input'], str))
    assert any(row['input_type'] == 'document' and row['input'] == ['约定已经从周六改到周日。'] for row in seen[1:])
    root = model_index(settings, model)
    profile = json.loads((root / 'profile.json').read_text())
    routes = json.loads((root / 'routes.json').read_text())
    assert profile['input_contract'] == routes['profile']['input_contract'] == 'voyage-retrieval-v1'
    assert profile['document_instruction'] == profile['query_instruction'] == ''
    assert (root / 'ready.json').is_file()


def test_preparation_rejects_missing_contract_before_writing_profile(tmp_path):
    database, index, endpoint = make_client(tmp_path, contract=False)
    with sqlite3.connect(index) as conn:
        profile = json.loads(conn.execute("SELECT value FROM settings WHERE key='embedding_profile'").fetchone()[0])
    profile['dimension'] = 2048
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(profile))
    settings = Settings(database, index, embedding={'endpoint': endpoint, 'api_key': 'synthetic-key'})
    with pytest.raises(ValueError, match='complete embedding profile'):
        prepare(settings, path, tmp_path / 'unused-examples.json')


@pytest.mark.parametrize('fault', ('missing_model', 'wrong_model', 'missing_index', 'extra_row', 'dimension'))
def test_discovery_rejects_invalid_native_reply_without_marking_index_ready(tmp_path, monkeypatch, fault):
    settings = Settings(tmp_path / 'memory.db', writable=True)
    initialize(settings)
    model = dict(id='voyage', model='voyage-4-large', protocol='openai',
                 base_url='https://ai.mongodb.com/v1', dimension=2048)
    save_settings(settings.database, {'models': [model], 'assignments': {'embedding': 'voyage'}})
    result = response_for('dimension probe')
    if fault == 'missing_model':
        del result['model']
    elif fault == 'wrong_model':
        result['model'] = 'other'
    elif fault == 'missing_index':
        del result['data'][0]['index']
    elif fault == 'extra_row':
        result['data'].append(result['data'][0])
    else:
        result['data'][0]['embedding'] = [1.] * 1024
    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result)), **kwargs))
    with pytest.raises(ValueError):
        prepare_selected(settings)
    root = model_index(settings, model)
    assert not (root / 'ready.json').exists()
    assert not (root / 'profile.json').exists()

