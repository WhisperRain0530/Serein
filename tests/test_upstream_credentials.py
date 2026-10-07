"""Credentials must not silently follow an edited API address."""
from copy import deepcopy
import json

import httpx
import pytest

from test_public_settings import deployment
from serein.deployment import configured_models, read_settings, save_settings


OLD_BASE = 'https://old.example/v1'
NEW_BASE = 'https://new.example/v1'
OLD_KEY = 'synthetic-old-provider-key'
NEW_KEY = 'synthetic-new-provider-key'
OLD_ENV = 'SEREIN_TEST_OLD_PROVIDER_KEY'
NEW_ENV = 'SEREIN_TEST_NEW_PROVIDER_KEY'


def connection(kind, base=OLD_BASE, **credentials):
    if kind == 'upstreams':
        item = {'id':'provider', 'name':'Provider', 'base_url':base,
                'models':[{'id':'chat', 'upstream_model':'chat'}], **credentials}
    elif kind == 'models':
        item = {'id':'chat', 'label':'Chat', 'model':'chat', 'base_url':base, **credentials}
    else:
        item = {'base_url':base, 'model':'chat', **credentials}
    return {kind: item if kind == 'upstream' else [item]}


def stored_connection(settings, kind):
    state = read_settings(settings.database)
    return state[kind] if kind == 'upstream' else state[kind][0]


@pytest.mark.parametrize('kind', ['upstreams', 'models', 'upstream'])
@pytest.mark.parametrize('new_base', [NEW_BASE, 'http://old.example/v1', 'https://old.example/other'])
def test_address_change_without_new_key_is_rejected_atomically(deployment, kind, new_base):
    settings, client = deployment
    save_settings(settings.database, connection(kind, api_key=OLD_KEY))
    before = read_settings(settings.database)
    # Use the HTTP schema, including an unrelated edit that must roll back too.
    result = client.patch('/v1/settings', json={
        'identity':{'user_name':'Must not be saved'}, **connection(kind, new_base)})
    assert result.status_code == 400, result.text
    assert '上游地址已改变' in result.json()['detail']
    assert OLD_KEY not in result.text and new_base not in result.text
    assert read_settings(settings.database) == before


@pytest.mark.parametrize('kind', ['upstreams', 'models', 'upstream'])
@pytest.mark.parametrize('key', [NEW_KEY, ''])
def test_address_change_accepts_explicit_key_or_no_auth(deployment, kind, key):
    settings, client = deployment
    save_settings(settings.database, connection(kind, api_key=OLD_KEY))
    result = client.patch('/v1/settings', json=connection(kind, NEW_BASE, api_key=key))
    assert result.status_code == 200, result.text
    saved = stored_connection(settings, kind)
    assert saved['base_url'] == NEW_BASE and saved['api_key'] == key
    assert not saved.get('api_key_env')
    assert OLD_KEY not in result.text and NEW_KEY not in result.text


@pytest.mark.parametrize('kind', ['upstreams', 'models', 'upstream'])
def test_same_address_and_trailing_slash_preserve_key(deployment, kind):
    settings, client = deployment
    save_settings(settings.database, connection(kind, api_key=OLD_KEY))
    result = client.patch('/v1/settings', json=connection(kind, OLD_BASE + '/'))
    assert result.status_code == 200, result.text
    assert stored_connection(settings, kind)['api_key'] == OLD_KEY


@pytest.mark.parametrize('kind', ['upstreams', 'models', 'upstream'])
def test_unauthenticated_address_can_still_be_changed(deployment, kind):
    settings, client = deployment
    save_settings(settings.database, connection(kind))
    result = client.patch('/v1/settings', json=connection(kind, NEW_BASE))
    assert result.status_code == 200, result.text
    saved = stored_connection(settings, kind)
    assert saved['base_url'] == NEW_BASE and not saved.get('api_key') and not saved.get('api_key_env')


def test_cleared_legacy_address_does_not_implicitly_rebind_residual_key(deployment):
    settings, client = deployment
    save_settings(settings.database, connection('upstream', '', api_key=OLD_KEY))
    before = read_settings(settings.database)
    result = client.patch('/v1/settings', json=connection('upstream', NEW_BASE))
    assert result.status_code == 400, result.text
    assert read_settings(settings.database) == before


@pytest.mark.parametrize('inline_key', ['', OLD_KEY])
@pytest.mark.parametrize('env_available', [False, True])
def test_changed_address_cannot_keep_same_env_even_if_unset(deployment, monkeypatch, inline_key, env_available):
    settings, client = deployment
    monkeypatch.delenv(OLD_ENV, raising=False)
    if env_available:
        monkeypatch.setenv(OLD_ENV, 'synthetic-env-key')
    save_settings(settings.database, connection('upstreams', api_key=inline_key, api_key_env=OLD_ENV))
    before = read_settings(settings.database)
    for extra in ({}, {'api_key':''}):
        result = client.patch('/v1/settings', json=connection(
            'upstreams', NEW_BASE, api_key_env=OLD_ENV, **extra))
        assert result.status_code == 400, result.text
        assert read_settings(settings.database) == before


def test_replacement_credentials_remove_old_fallback_and_precedence(deployment, monkeypatch):
    settings, client = deployment
    monkeypatch.setenv(OLD_ENV, 'synthetic-old-env-key')
    monkeypatch.setenv(NEW_ENV, 'synthetic-new-env-key')
    seed = connection('upstreams', api_key=OLD_KEY, api_key_env=OLD_ENV)
    save_settings(settings.database, seed)
    result = client.patch('/v1/settings', json=connection(
        'upstreams', NEW_BASE, api_key=NEW_KEY, api_key_env=OLD_ENV))
    assert result.status_code == 200, result.text
    saved = stored_connection(settings, 'upstreams')
    assert saved['api_key'] == NEW_KEY and saved['api_key_env'] == ''
    # Clearing the new inline key later must never resurrect the former key.
    result = client.patch('/v1/settings', json=connection('upstreams', NEW_BASE, api_key=''))
    assert result.status_code == 200, result.text
    assert configured_models(read_settings(settings.database))[0]['api_key'] == ''

    save_settings(settings.database, seed)
    result = client.patch('/v1/settings', json=connection('upstreams', NEW_BASE, api_key_env=NEW_ENV))
    assert result.status_code == 200, result.text
    saved = stored_connection(settings, 'upstreams')
    assert saved['api_key'] == '' and saved['api_key_env'] == NEW_ENV
    assert configured_models(read_settings(settings.database))[0]['api_key'] == 'synthetic-new-env-key'

    save_settings(settings.database, seed)
    result = client.patch('/v1/settings', json=connection(
        'upstreams', NEW_BASE, api_key=NEW_KEY, api_key_env=NEW_ENV))
    assert result.status_code == 200, result.text
    saved = stored_connection(settings, 'upstreams')
    assert saved['api_key'] == NEW_KEY and saved['api_key_env'] == NEW_ENV
    assert configured_models(read_settings(settings.database))[0]['api_key'] == NEW_KEY


def test_explicit_clear_removes_both_credentials_and_same_address_keeps_env(deployment, monkeypatch):
    settings, client = deployment
    monkeypatch.setenv(OLD_ENV, 'synthetic-old-env-key')
    save_settings(settings.database, connection('upstreams', api_key=OLD_KEY, api_key_env=OLD_ENV))
    result = client.patch('/v1/settings', json=connection('upstreams', api_key_env=OLD_ENV))
    assert result.status_code == 200, result.text
    saved = stored_connection(settings, 'upstreams')
    assert saved['api_key'] == OLD_KEY and saved['api_key_env'] == OLD_ENV
    result = client.patch('/v1/settings', json=connection(
        'upstreams', NEW_BASE, api_key='', api_key_env=''))
    assert result.status_code == 200, result.text
    assert configured_models(read_settings(settings.database))[0]['api_key'] == ''


def test_legacy_models_group_migration_cannot_rebind_saved_key(deployment):
    settings, client = deployment
    save_settings(settings.database, connection('models', api_key=OLD_KEY))
    group = client.get('/v1/settings').json()['upstreams'][0]
    group.pop('api_key_configured')
    assert group['id'].startswith('legacy-upstream-')
    original = deepcopy(group)
    group['base_url'] = NEW_BASE
    before = read_settings(settings.database)
    result = client.patch('/v1/settings', json={'models':[], 'upstreams':[group]})
    assert result.status_code == 400, result.text
    assert read_settings(settings.database) == before
    result = client.patch('/v1/settings', json={'models':[], 'upstreams':[original]})
    assert result.status_code == 200, result.text
    assert configured_models(read_settings(settings.database))[0]['api_key'] == OLD_KEY


def test_template_name_match_cannot_rebind_saved_key(deployment):
    settings, client = deployment
    save_settings(settings.database, connection('upstreams', api_key=OLD_KEY))
    item = connection('upstreams', NEW_BASE)['upstreams'][0]
    item.pop('id')
    before = read_settings(settings.database)
    result = client.post('/v1/settings/upstreams-template', json={'template':json.dumps({'upstreams':[item]})})
    assert result.status_code == 400, result.text
    assert '上游地址已改变' in result.json()['detail']
    assert read_settings(settings.database) == before
    item['api_key'] = NEW_KEY
    result = client.post('/v1/settings/upstreams-template', json={'template':json.dumps({'upstreams':[item]})})
    assert result.status_code == 200, result.text
    assert configured_models(read_settings(settings.database))[0]['api_key'] == NEW_KEY


def test_legacy_model_env_cannot_survive_address_replacement(deployment):
    settings, client = deployment
    # Older stored model records can contain env references even though their
    # current HTTP schema only exposes API Key; the grouped editor exposes both.
    save_settings(settings.database, connection('models', api_key='', api_key_env=OLD_ENV))
    result = client.patch('/v1/settings', json=connection('models', NEW_BASE))
    assert result.status_code == 400, result.text
    result = client.patch('/v1/settings', json=connection('models', NEW_BASE, api_key=NEW_KEY))
    assert result.status_code == 200, result.text
    saved = stored_connection(settings, 'models')
    assert saved['api_key'] == NEW_KEY and saved['api_key_env'] == ''


def test_chat_never_sends_old_key_to_replacement_address(deployment, monkeypatch):
    settings, client = deployment
    save_settings(settings.database, {'pipeline':{'auto_enabled':False},
        **connection('upstreams', api_key=OLD_KEY, api_key_env=OLD_ENV)})
    seen = []
    def upstream(request):
        seen.append((str(request.url), request.headers.get('authorization')))
        return httpx.Response(200, json={'id':'synthetic', 'object':'chat.completion',
            'choices':[{'message':{'role':'assistant', 'content':'Synthetic reply'}}]})
    real_client = httpx.AsyncClient
    def fake_client(*args, **kwargs):
        return real_client(*args, **kwargs, transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(httpx, 'AsyncClient', fake_client)
    blocked = client.patch('/v1/settings', json=connection('upstreams', NEW_BASE, api_key_env=OLD_ENV))
    assert blocked.status_code == 400, blocked.text
    assert not seen
    saved = client.patch('/v1/settings', json=connection(
        'upstreams', NEW_BASE, api_key=NEW_KEY, api_key_env=OLD_ENV))
    assert saved.status_code == 200, saved.text
    chat = client.post('/v1/chat/completions', json={'model':'Provider/chat',
        'messages':[{'role':'user', 'content':'Synthetic test'}], 'stream':False})
    assert chat.status_code == 200, chat.text
    assert seen == [(NEW_BASE + '/chat/completions', 'Bearer ' + NEW_KEY)]
