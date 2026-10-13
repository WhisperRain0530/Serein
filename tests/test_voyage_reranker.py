"""Voyage and Atlas protocol checks with synthetic inputs and no live API calls."""
import json

import pytest

httpx = pytest.importorskip('httpx')

from serein.adapters.reranker import RerankerClient, RerankerProviderError


DOCUMENTS = [
    {'ref': 'event:plan', 'title': '见面', 'body': '周六计划已改为周日。'},
    {'ref': 'scene:friend', 'title': '朋友', 'body': '陈安对花生过敏，林岚不过敏。'},
]


@pytest.mark.parametrize('host', [
    'api.voyageai.com', 'ai.mongodb.com', 'us.ai.mongodb.com', 'eu.ai.mongodb.com',
])
def test_voyage_protocol_preserves_inputs_and_maps_native_data(host):
    question = '林岚过敏吗？\n见面时间后来改成了哪天？'
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload == {
            'model': 'rerank-3', 'query': question,
            'documents': ['见面\n周六计划已改为周日。', '朋友\n陈安对花生过敏，林岚不过敏。'],
            'top_k': 2, 'truncation': False,
        }
        assert request.headers['authorization'] == 'Bearer synthetic-secret'
        return httpx.Response(200, json={
            'object': 'list', 'model': 'rerank-3', 'usage': {'total_tokens': 42},
            'data': [{'index': 1, 'relevance_score': 1}, {'index': 0, 'relevance_score': 0}],
        })

    scorer = RerankerClient(f'https://{host}/v1/rerank', 'rerank-3', api_key='synthetic-secret')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert scorer(question, DOCUMENTS, client=client) == {'scene:friend': 1, 'event:plan': 0}
    assert len(calls) == 1


@pytest.mark.parametrize('host', [
    'rerank.example', 'api.voyageai.com.example', 'ai.mongodb.com.example',
    'us.ai.mongodb.com.example', 'eu.ai.mongodb.com.example',
])
def test_voyage_model_name_or_lookalike_host_does_not_change_other_protocols(host):
    def handle(request):
        payload = json.loads(request.content)
        assert payload == {
            'model': 'rerank-3', 'query': '问题',
            'documents': ['见面\n周六计划已改为周日。'],
            'top_n': 1, 'return_documents': False,
        }
        return httpx.Response(200, json={'results': [{'index': 0, 'relevance_score': .8}]})

    scorer = RerankerClient(f'https://{host}/v1/rerank', 'rerank-3', api_key='synthetic-secret')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert scorer('问题', DOCUMENTS[:1], client=client) == {'event:plan': .8}


@pytest.mark.parametrize('data', [
    [{'index': -1, 'relevance_score': .5}],
    [{'index': 2, 'relevance_score': .5}],
    [{'index': True, 'relevance_score': .5}],
    [{'index': 0, 'relevance_score': True}],
    [{'index': 0, 'relevance_score': '0.5'}],
    [{'index': 0, 'relevance_score': -.1}],
    [{'index': 0, 'relevance_score': 1.1}],
    [{'index': 0, 'relevance_score': .5}] * 2,
])
def test_voyage_retains_index_and_score_validation(data):
    scorer = RerankerClient('https://ai.mongodb.com/v1/rerank', 'rerank-3', api_key='synthetic-secret')
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'data': data}))) as client:
        with pytest.raises(ValueError) as error:
            scorer('问题', DOCUMENTS, client=client)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('body', [
    'private provider text synthetic-secret',
    '{"results": [{"index": 0, "relevance_score": 1}]}',
    '{"data": null}',
    '{"data": {"private": "synthetic-secret"}}',
    '{"data": ["private synthetic-secret"]}',
    '{"data": [{"index": 0}]}',
])
def test_voyage_malformed_responses_have_safe_diagnostics(body):
    scorer = RerankerClient('https://ai.mongodb.com/v1/rerank', 'rerank-3', api_key='synthetic-secret')
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))) as client:
        with pytest.raises(RerankerProviderError) as error:
            scorer('问题', DOCUMENTS, client=client)
    assert error.value.code == 'invalid_response'
    assert 'private' not in str(error.value) and 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('failure,code', [(400, 'http_400'), (401, 'http_401'), (429, 'http_429'), ('timeout', 'timeout')])
def test_voyage_provider_errors_are_safe_and_never_retry(failure, code):
    calls = []

    def handle(request):
        calls.append(request)
        assert json.loads(request.content)['truncation'] is False
        if failure == 'timeout':
            raise httpx.ReadTimeout('private synthetic-secret', request=request)
        return httpx.Response(failure, text='private synthetic-secret')

    scorer = RerankerClient('https://ai.mongodb.com/v1/rerank', 'rerank-3', api_key='synthetic-secret')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RerankerProviderError) as error:
            scorer('问题', DOCUMENTS, client=client)
    assert error.value.code == code
    assert 'private' not in str(error.value) and 'synthetic-secret' not in str(error.value)
    assert len(calls) == 1


def test_voyage_window_scores_keep_the_best_exact_source_excerpt():
    class SyntheticWindow:
        def fits(self, text, *, query):
            return False

        def spans(self, text, *, prefix, query, max_chars):
            yield 0, 4
            yield 4, len(text)

        def count(self, text, *, query):
            return len(text) + len(query)

    body = '周六作废周日见面'
    document = {'ref': 'event:plan', 'title': '见面', 'body': body, 'source_body': body}
    scorer = RerankerClient('https://ai.mongodb.com/v1/rerank', 'rerank-3', api_key='synthetic-secret')
    scorer.window = SyntheticWindow()

    def handle(request):
        payload = json.loads(request.content)
        assert payload['documents'] == ['title: 见面\nbody: 周六作废', 'title: 见面\nbody: 周日见面']
        assert payload['top_k'] == 2
        return httpx.Response(200, json={'data': [
            {'index': 1, 'relevance_score': .9}, {'index': 0, 'relevance_score': .2},
        ]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        scores = scorer('见面时间', [document], client=client)
    assert scores == {'event:plan': .9}
    assert scores.evidence['event:plan']['text'] == '周日见面'
    assert scores.evidence['event:plan']['source_spans'] == [{'start_offset': 4, 'end_offset': 8}]
