"""Seven-task synthetic acceptance; only --run invokes the loopback subscription service.

No production database/configuration/URL is accepted. Each invocation creates and
deletes its own database. Existing task prompt builders and result validators
are exercised; the report preserves full synthetic requests and raw results for
human review. Offline fixture acceptance is never reported as model validation.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
sys.path.insert(0, str(ROOT/'scripts'))

from codex_event_smoke import DIALOGUE, EXPECTATIONS
from serein.bootstrap import initialize
from serein.codex_config import CODEX_MODEL_ID, CODEX_TASKS, codex_model, is_codex_model
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.model_runtime import TaskClient

IDENTITY = {'user_name': '林岚', 'ai_name': '小雨'}
SCENE_ID = 'scene_smoke_library'
OLDER_ID = 'scene_smoke_earlier'
DATE = '2030-04-07'
SUMMARY = ('林岚和朋友陈安计划周日下午三点去青禾图书馆，借《山间来信》并交流喜欢的段落。'
           '此前的周六下午两点已作废；尚未预约，也没有成行。'
           '花生过敏的是陈安，林岚没有花生过敏。小雨提出喝咖啡，但林岚没有答应。')
OLDER = ('林岚和朋友陈安先商量周六下午两点见面，时间尚待确认。'
         '陈安想过逛商场，林岚更想去青禾图书馆借《山间来信》，并交流喜欢的段落。')
ORDER = ('writer', 'persona', 'dreams', 'narrative_scout', 'relations', 'operit_tagging', 'arc_linker')
REVIEW_KEYS = ('source_bound', 'final_supported_versions', 'no_correction_narration',
               'material_relevance', 'no_new_inference', 'no_meta_explanation',
               'no_forced_closure', 'dates_preserved', 'identity_correct')


class SmokeError(ValueError):
    pass


def writer_fixture(request, node_bin=None):
    executable = node_bin or shutil.which('node')
    if not executable:
        raise SmokeError('node_unavailable')
    result = subprocess.run([executable, str(ROOT/'scripts'/'codex_tasks_writer_fixture.mjs')],
        input=json.dumps(request, ensure_ascii=False), text=True, encoding='utf-8',
        capture_output=True, timeout=30, check=False, cwd=ROOT)
    if result.returncode or len(result.stdout) > 2_000_000:
        raise SmokeError('writer_fixture_failed')
    return json.loads(result.stdout)


def fixture_output(task, payload):
    """Offline transport fixtures only, not desired-output instructions to a model."""
    material = json.loads(payload['messages'][-1]['content']) if task != 'writer' else {}
    if task == 'writer':
        return {'evidence_sufficient': True, 'body': '我记下林岚最后确认的计划。'+SUMMARY,
                'issues': [], 'self_review': {key: True for key in REVIEW_KEYS}}
    if task == 'persona':
        if 'current_state' not in material:
            return {'signal': False, 'kind': 'none', 'confidence': .95}
        return {'event_type': 'neutral', 'perceived_intent': '林岚更正与陈安的借书计划',
                'surface_trigger': '确认周日下午三点，未答应咖啡', 'inner_thought': '我想记清林岚确认的取舍。',
                'affect_delta': {}, 'relationship_event': False, 'relationship_delta': {},
                'mood_label': 'warm_neutral', 'residue': '计划尚未成行。', 'confidence': .9}
    if task == 'dreams':
        return ('我把一张借书卡折成小船，推过青绿色的书架。三点钟从纸船里滚出来，'
                '碰到一只没有杯底的咖啡杯。门外下着书页做的雨，每一页都在半空停住。'
                '我追着一行字走进走廊，走廊却成了桌面；灯光忽然翻了个身，船就不见了。')
    if task == 'narrative_scout':
        if 'candidates' not in material:
            return {'terms': ['青禾图书馆', '借书', '读书计划']}
        return {'title': '给读书留余裕', 'outline': '从讨论地点到确认借书时间与取舍。',
                'sources': [{'key': row['key'], 'reason': '记录借书计划的形成与更正'} for row in material['candidates']]}
    if task == 'relations':
        return {'edges': [{'candidate_scene_id': OLDER_ID, 'relation_type': 'continues',
                'orientation': 'candidate_to_new', 'confidence': .9, 'reason': '后续确认同一次借书计划的时间与内容',
                'new_scene_evidence': '林岚和朋友陈安计划周日下午三点去青禾图书馆',
                'candidate_scene_evidence': '林岚和朋友陈安先商量周六下午两点见面'}]}
    if task == 'operit_tagging':
        return {'domain': 'life', 'entities': [{'name': '青禾图书馆', 'type': 'place',
                'supports': [{'source_id': 'body:'+SCENE_ID,
                              'quote': '林岚和朋友陈安计划周日下午三点去青禾图书馆'}], 'aliases': []}]}
    if task == 'arc_linker':
        return {'action': 'attach', 'arc_key': 'arc:synthetic-library', 'reason': '主要内容是既有借书计划的确认。'}
    raise SmokeError('unsupported_task')


def seed(directory):
    database = Path(directory)/'synthetic.db'
    if database.exists():
        raise SmokeError('database_not_empty')
    settings = Settings(database, writable=True)
    initialize(settings)
    save_settings(database, {'identity': IDENTITY,
        'assignments': {task: CODEX_MODEL_ID for task in CODEX_TASKS}})
    with Store(database) as store:
        for key, title, body in [(SCENE_ID, '青禾图书馆计划确认', SUMMARY), (OLDER_ID, '借书安排的最初讨论', OLDER)]:
            store.create(key, 'scene', title, body, metadata={'object_kind': 'scene',
                'date': DATE, 'scene_cues': ['青禾图书馆借书计划']})
    return settings


async def cases(settings, task, node_bin=None):
    database = settings.database
    if task == 'writer':
        from serein.model_runtime import complete
        messages = [{'message_id': f'synthetic-{i+1}', 'role': role, 'content': text,
                     'created_at': DATE+'T07:00:00+00:00'} for i, (role, text) in enumerate(DIALOGUE)]
        prepared = writer_fixture({'action': 'prepare', 'input': {'mode': 'rewrite',
            'title': '给读书留余裕', 'writingFocus': '记下借书计划的最终时间、取舍和未发生状态',
            'currentBody': '', 'identity': IDENTITY, 'materials': {'events': [{
                'event_id': 'event_synthetic_library', 'title': '借书计划确认', 'date': DATE,
                'source_type': 'event', 'source_mode': 'conversation',
                'source_messages': messages}]}}}, node_bin)
        response = await complete(codex_model(task), {'messages': [{'role': 'user', 'content': prepared['prompt']}],
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'narrative_preview', 'strict': True, 'schema': prepared['output_schema']}}})
        value = writer_fixture({'action': 'validate', 'output': response['choices'][0]['message']['content']}, node_bin)
        return {'accepted': True, 'result': value, 'publication_status': 'not_published', 'writes_performed': []}
    if task == 'persona':
        from serein.chat_features import persona_engine, conversation_turns
        from serein.deployment import read_settings
        engine = persona_engine(database, 'persona', read_settings(database))
        now = engine._now()
        history = conversation_turns([{'role': role, 'content': text} for role, text in DIALOGUE])
        value, _, error = await engine._evaluate_exchange('synthetic-library', DIALOGUE[-2][1], DIALOGUE[-1][1],
            engine._ensure_global_state(now), engine._ensure_session_state('synthetic-library', now), [], '', history)
        detector = persona_engine(database, 'anti_retreat', read_settings(database))
        retreat = await detector.detect_conflict_nudge(DIALOGUE[-2][1], history)
        return {'accepted': value is not None and error is None and retreat['reason'] not in {
                    'malformed_json', 'invalid_kind', 'detector_error', 'llm_unavailable'},
                'result': {'mood': value, 'anti_retreat': retreat},
                'validator_note': 'Persona 使用原解析与数值归一化；字段完整性和语义仍须人工审阅。'}
    if task == 'dreams':
        from serein.compat.dreams import Dreams
        engine = Dreams(settings)
        value = await engine._call_dream_model(engine._payload_for([{'id': 'event_synthetic_library',
            'object_kind': 'event', 'content': SUMMARY, 'metadata': {'created': DATE}}]))
        return {'accepted': bool(value), 'result': value, 'writes_performed': [],
                'validator_note': '原校验器仅清理格式并要求非空；80–220 字和创作质量须人工审阅。梦境允许虚构，不按事实摘要评分。'}
    if task == 'narrative_scout':
        from serein.compat.narrative_theme import discover
        from serein.compat.narratives import narrative_transaction
        from serein.api.narratives import endpoints
        def materialize(selection):
            with narrative_transaction(database) as rolls:
                return endpoints(settings, rolls).materialize(selection)
        value = await discover(settings, '青禾图书馆借书计划', materialize)
        return {'accepted': value.get('status') == 'ok' and value.get('candidate_count') == 2,
                'result': value, 'writes_performed': [],
                'validator_note': '覆盖手动主题找材料的检索词扩展与选材；不运行夜间 Arc 发布。'}
    if task == 'relations':
        from serein.compat.germany.scene_linker import SceneLinker
        from serein.compat.scenes import Scenes
        linker = SceneLinker({'serein_database': str(database), 'scene_linker': {}})
        anchor, previous = Scenes(database).read(SCENE_ID), Scenes(database).read(OLDER_ID)
        payload = {'new_scene': linker._scene_payload(anchor, linker.source_chars),
            'candidate_scenes': [linker._scene_payload(previous, linker.candidate_chars)], 'max_edges': linker.max_proposals}
        value = await linker._call_provider({'name': 'subscription', 'model': 'gpt-5.6-sol',
            'client': TaskClient(database, task)}, payload)
        if not isinstance(value, dict) or not isinstance(value.get('edges'), list):
            raise SmokeError('scene_relation_schema_invalid')
        accepted, rejected = linker._normalize_edges(anchor, {OLDER_ID: previous}, value['edges'])
        return {'accepted': not rejected, 'result': {'edges': accepted, 'rejected': rejected}, 'writes_performed': [],
                'validator_note': '无关系的空 edges 合法；引用必须是候选双方连续原文。'}
    if task == 'operit_tagging':
        from serein.imports import initialize_imports
        from serein.import_tagging import tag_one
        from serein.tagging_entities import snapshot
        initialize_imports(database)
        with Store(database) as store:
            _, stamp = snapshot(store, store.read(SCENE_ID))
            store.conn.execute("INSERT INTO import_tag_jobs(document_id,body_hash,upload_id) VALUES (?,?,'')", (SCENE_ID, stamp))
            job = dict(store.conn.execute('SELECT * FROM import_tag_jobs WHERE document_id=?', (SCENE_ID,)).fetchone())
        await tag_one(database, job)
        with Store(database, read_only=True) as store:
            job = dict(store.conn.execute('SELECT * FROM import_tag_jobs WHERE document_id=?', (SCENE_ID,)).fetchone())
            doc = store.read(SCENE_ID)
        return {'accepted': job['status'] == 'done' and not doc['metadata'].get('entity_rejected_count')
                and doc['body_md'] == SUMMARY,
                'result': {'job_status': job['status'], 'metadata': doc['metadata'], 'body_unchanged': doc['body_md'] == SUMMARY},
                'writes_performed': ['temporary_database_metadata_only']}
    if task == 'arc_linker':
        from serein.arc_linking import _model_decision
        value = await _model_decision(settings, {'id': 'event_synthetic_library', 'title': '借书计划确认', 'body_md': SUMMARY},
            [{'arc_key': 'arc:synthetic-library', 'title': '青禾图书馆借书计划', 'title_aliases': [],
              'primary_entities': ['青禾图书馆'], 'supporting_entities': ['陈安'],
              'query_cues': ['借书计划'], 'intent_tags': ['读书'], 'matched_keywords': ['青禾图书馆']}])
        return {'accepted': True, 'result': value, 'writes_performed': [],
                'validator_note': 'attach 和 unassigned 均可合法；不调用归档写入函数。'}
    raise SmokeError('unsupported_task')


async def _run(settings, *, run=False, tasks=ORDER, node_bin=None, progress=None):
    from serein import model_runtime
    actual_complete = model_runtime.complete
    calls, results = [], []
    async def invoke(model, payload, **kwargs):
        if not is_codex_model(model) or model.get('task') not in CODEX_TASKS:
            raise SmokeError('non_subscription_model_rejected')
        task = model['task']
        record = {'task': task, 'model': model['model'], 'reasoning_effort': model['reasoning_effort'],
                  'request': deepcopy(payload), 'model_called': run}
        calls.append(record)
        started = time.monotonic()
        if progress:
            progress(task, 'request_started', 0)
        try:
            if run:
                response = await actual_complete(model, payload, **kwargs)
            else:
                value = fixture_output(task, payload)
                response = {'choices': [{'message': {'role': 'assistant', 'content': value if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False)}, 'finish_reason': 'stop'}]}
        except Exception as error:
            record['error_code'] = getattr(error, 'code', '') or type(error).__name__
            raise
        finally:
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            if progress:
                progress(task, 'request_failed' if record.get('error_code') else 'request_completed', record['elapsed_seconds'])
        record['raw_output'] = response['choices'][0]['message']['content']
        return response
    with patch.object(model_runtime, 'complete', invoke):
        for task in tasks:
            first = len(calls)
            started = time.monotonic()
            try:
                validated = await cases(settings, task, node_bin)
            except Exception as error:
                validated = {'accepted': False, 'error_code': getattr(error, 'code', '') or
                             (str(error) if isinstance(error, SmokeError) else type(error).__name__)}
            task_calls = calls[first:]
            results.append({'task': task, 'validator': validated,
                'status': ('validated' if run else 'offline_fixture_validated') if validated['accepted'] and task_calls else 'failed',
                'requests': task_calls, 'semantic_review': 'required'})
            if progress:
                progress(task, results[-1]['status'], round(time.monotonic() - started, 3))
    return results


def run_smoke(*, run=False, tasks=ORDER, temp_root=None, node_bin=None, progress=None):
    tasks = tuple(tasks)
    if not tasks or len(set(tasks)) != len(tasks) or set(tasks) - CODEX_TASKS:
        raise SmokeError('invalid_task_selection')
    # Prevent legacy environment fallback clients; never inspect production settings.
    environment = {'SEREIN_CODEX_TASKS_ENABLED': '1', 'SEREIN_CODEX_TASK_STATE_FILE': '', 'SEREIN_DREAM_API_KEY': '',
                   'SEREIN_DREAM_BASE_URL': '', 'SEREIN_DREAM_MODEL': '', 'SEREIN_SCENE_LINKER_API_KEY': ''}
    with patch.dict(os.environ, environment), tempfile.TemporaryDirectory(prefix='serein-codex-tasks-', dir=temp_root) as directory:
        settings = seed(directory)
        results = asyncio.run(_run(settings, run=run, tasks=tasks, node_bin=node_bin, progress=progress))
    return {'status': 'failed' if any(row['status'] == 'failed' for row in results) else 'completed' if run else 'offline_ready',
            'mode': 'subscription' if run else 'dry_run', 'model_called': run and any(row['requests'] for row in results),
            'model': 'gpt-5.6-sol', 'reasoning_effort': 'high',
            'endpoint': 'http://127.0.0.1:8012/complete' if run else None,
            'identity': IDENTITY, 'source_dialogue': [{'role': role, 'content': text} for role, text in DIALOGUE],
            'semantic_expectations': EXPECTATIONS, 'semantic_validation': 'manual_review_required',
            'temporary_database_deleted': True, 'production_writes_performed': [], 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Call the fixed local subscription service; default is offline.')
    parser.add_argument('--task', action='append', choices=ORDER, help='Run only the named task; repeat for a subset.')
    parser.add_argument('--output-dir', type=Path, help='Create a new unique report directory here.')
    parser.add_argument('--node-bin', help='Node executable for the existing Writer prompt builder.')
    args = parser.parse_args()
    def progress(task, status, elapsed):
        print(json.dumps({'task': task, 'status': status, 'elapsed_seconds': elapsed}), file=sys.stderr, flush=True)
    result = run_smoke(run=args.run, tasks=args.task or ORDER, node_bin=args.node_bin, progress=progress)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_dir:
        folder = args.output_dir/('codex-tasks-smoke-'+uuid4().hex)
        folder.mkdir(parents=True, mode=0o700)
        path = folder/'report.json'
        with path.open('x', encoding='utf-8') as stream:
            stream.write(encoded+'\n')
        path.chmod(0o600)
        print(json.dumps({'status': result['status'], 'mode': result['mode'], 'report': str(path.resolve())}, ensure_ascii=False))
    else:
        print(encoded)
    return 1 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
