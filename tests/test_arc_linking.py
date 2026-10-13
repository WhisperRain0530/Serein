import asyncio
import json

import pytest

from serein.arc_linking import candidate_arcs, enqueue, initialize, process
from serein.bootstrap import initialize as bootstrap
from serein.compat.events import Events
from serein.compat.narratives import narrative_transaction
from serein.config import Settings
from serein.core.store import Store


def setup(tmp_path):
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True)
    bootstrap(settings)
    with narrative_transaction(settings.database,write=True) as rolls:
        result=rolls.publish(narrative_id='narrative_garden',expected_revision=0,title='雨夜花园',
            document='# 雨夜花园\n\n## 第一人称叙事\n\n- diary:1\n- diary:2\n',
            source_diary_ids=[1,2],publication_status='collecting',
            arc_key='arc:rain-garden',query_cues=['花园','雨夜'],primary_entities=['Serein'])
        assert result['status']=='created'
    result=Events(settings.database).write_many([{'type':'event','title':'花园流程图完成',
        'body':'我们把雨夜花园的公开流程图画完了。','origin_id':'assistant_bridge:arc-test','recallable':True,
        'source_refs':[{'source_system':'test','session_id':'one','message_id':'1','role':'user',
            'created_at':'2026-09-15T10:00:00+08:00','content':'把花园流程图画完了',
            'binding_method':'test'}]}])
    event_id=result['items'][0]['item_id']
    event=Events(settings.database).read(event_id)
    initialize(settings.database)
    with Store(settings.database) as store:enqueue(store.conn,event_id,event['fingerprint'])
    return settings,event_id


def test_linker_reads_event_and_keyword_arc_cards_without_originals(tmp_path):
    settings,event_id=setup(tmp_path);seen=[]
    async def decide(payload):
        seen.append(payload)
        assert payload['event']['body']=='我们把雨夜花园的公开流程图画完了。'
        assert payload['candidate_arcs'][0]['arc_key']=='arc:rain-garden'
        assert 'source_refs' not in str(payload) and 'tagged_entities' not in str(payload)
        return {'action':'attach','arc_key':'arc:rain-garden','reason':'主要经历直接推进雨夜花园'}
    with narrative_transaction(settings.database) as rolls:before=rolls.read('narrative_garden')
    result=asyncio.run(process(settings,invoke_model=decide))
    assert result=={'status':'linked','event_id':event_id,'arc_key':'arc:rain-garden','processed':1}
    assert len(seen)==1
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT arc_key FROM fact_event_arc_links WHERE event_id=?',(event_id,)).fetchone()[0]=='arc:rain-garden'
        assert store.conn.execute('SELECT arc_key FROM event_arc_links WHERE event_id=?',(event_id,)).fetchone()[0]=='arc:rain-garden'
    with narrative_transaction(settings.database) as rolls:
        after=rolls.read('narrative_garden')
        assert after['full_document']==before['full_document'] and after['revision']==before['revision']


def test_no_keyword_candidate_skips_model_and_leaves_event_unbound(tmp_path):
    settings,event_id=setup(tmp_path)
    with Store(settings.database) as store:
        doc=store.read(event_id)
        store.revise(event_id,expected_revision=doc['revision'],title='早餐',body_md='今天吃了面包。',metadata=doc['metadata'])
        event=Events(settings.database).read(event_id)
        store.conn.execute('UPDATE pipeline_arc_links SET event_fingerprint=? WHERE event_id=?',(event['fingerprint'],event_id))
        assert candidate_arcs(store,store.read(event_id))==[]
    async def fail(_payload):
        raise AssertionError('No candidate must not call a model')
    result=asyncio.run(process(settings,invoke_model=fail))
    assert result['status']=='unassigned'
    with Store(settings.database) as store:
        assert not store.conn.execute('SELECT 1 FROM fact_event_arc_links WHERE event_id=?',(event_id,)).fetchone()


@pytest.mark.parametrize('internal', [True, False])
def test_decision_format_is_explicit_only_for_subscription_and_preserves_messages(tmp_path, monkeypatch, internal):
    from serein import arc_linking, model_runtime
    from serein.codex_config import codex_model
    # Frozen pre-change prompt: changing the transport contract must not rewrite it.
    original_prompt = '''你只判断一条已经正式落库的 Event 是否属于某条已有 Arc。
Arc 是长期主题、作品、项目或里程碑的材料目录，不是 Event 边界，也不是叙事正文。
你只能阅读 Event 的标题、正文，以及程序先按关键词找到的 Arc 卡；不会提供聊天原话或叙事卷正文。
只有 Event 的主要经历明确推进某条 Arc 时才能 attach。偶然提及、比喻、工具名、同一人物或泛泛相似都必须 unassigned。
只能选择候选中的一个 arc_key；不得创建 Arc、修改 Event 或撰写叙事卷。只返回合法 JSON。'''
    model = codex_model('arc_linker') if internal else {'id':'external-api', 'model':'synthetic-api'}
    monkeypatch.setattr(arc_linking, 'task_model', lambda database, task: model)
    event = {'id':'synthetic-event', 'title':'借书计划确认', 'body_md':'林岚和陈安计划周日三点借书，尚未成行。'}
    candidates = [{'arc_key':'arc:synthetic-library', 'title':'借书计划'}]
    expected = {'action':'attach','arc_key':'arc:synthetic-library','reason':'主要内容确认了既有借书计划。'}
    seen = []
    async def complete(selected, payload):
        assert selected == model
        seen.append(payload)
        return {'choices':[{'message':{'content':json.dumps(expected,ensure_ascii=False)}}]}
    monkeypatch.setattr(model_runtime, 'complete', complete)
    assert asyncio.run(arc_linking._model_decision(Settings(tmp_path/'unused.db'), event, candidates)) == expected
    assert seen[0]['messages'] == [
        {'role':'system','content':original_prompt},
        {'role':'user','content':json.dumps({'event':{'event_id':event['id'],'title':event['title'],
            'body':event['body_md']},'candidate_arcs':candidates},ensure_ascii=False)}]
    response_format = seen[0]['response_format']
    if not internal:
        assert response_format == {'type':'json_object'}
    else:
        assert response_format['type'] == 'json_schema'
        assert response_format['json_schema']['strict'] is True
        schema = response_format['json_schema']['schema']
        assert set(schema['properties']) == set(schema['required']) == {'action','arc_key','reason'}
        assert schema['additionalProperties'] is False
        assert schema['properties']['action']['enum'] == ['attach','unassigned']
        assert all(field['type'] == 'string' for field in schema['properties'].values())


@pytest.mark.parametrize('output', [
    {'decision':'attach','arc_key':'arc:synthetic-library'},
    {'action':'attach','arc_key':'arc:synthetic-library','reason':''},
    {'action':'attach','arc_key':'arc:unprovided','reason':'无此候选'},
])
def test_original_decision_validator_still_rejects_bad_shape_or_ungrounded_arc(output):
    from serein.arc_linking import normalize_decision
    with pytest.raises(ValueError):
        normalize_decision(output, [{'arc_key':'arc:synthetic-library'}])
