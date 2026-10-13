"""Durable Scene work and existing Germany schedules, independent of chat windows."""

import asyncio
import logging
import json
from contextlib import ExitStack
import time

from ..core.store import Store, encode, now
from ..file_lock import exclusive_lock
from .background import SceneReader, DreamMaterialReader, EmbeddingCandidates, germany_config
from .germany.scene_linker import SceneLinker
from .scout import Scout
from .dreams import Dreams
from .dream_vectors import DreamVectors

logger = logging.getLogger(__name__)


def scene_job_lock(settings):
    return exclusive_lock(settings.database.with_suffix('.scene-jobs.lock'))


def scene_job_failures(settings):
    with Store(settings.database, read_only=True) as store:
        rows = store.conn.execute("""
            SELECT s.value_json, r.title FROM background_state s
            JOIN documents d ON s.name='scene-job:' || d.id
            JOIN revisions r ON r.document_id=d.id AND r.number=d.revision
            WHERE json_extract(s.value_json,'$.status') IN ('failed','interrupted')
              AND d.lifecycle='active'
              AND EXISTS (SELECT 1 FROM scene_jobs j WHERE j.scene_id=d.id)
            ORDER BY s.name LIMIT 100
        """).fetchall()
    return [{**json.loads(row['value_json']), 'title': row['title']} for row in rows]


def retry_scene_job(settings, scene_id, attempt_id):
    # An old page or repeated click must not authorize another paid attempt.
    with scene_job_lock(settings), Store(settings.database) as store, store.transaction(immediate=True):
        row = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', ('scene-job:'+scene_id,)).fetchone()
        saved = json.loads(row[0]) if row else {}
        if saved.get('status') not in ('failed','interrupted') or saved.get('attempt_id') != attempt_id:
            return {'status': 'conflict', 'error': '这条任务已变化，请刷新后再试。'}
        doc = store.read(scene_id)
        if doc is None or doc['lifecycle'] != 'active':
            return {'status': 'conflict', 'error': 'Scene 已归档或删除。'}
        if not store.conn.execute('SELECT 1 FROM scene_jobs WHERE scene_id=?', (scene_id,)).fetchone():
            return {'status': 'conflict', 'error': '这条任务已处理。'}
        saved.update(status='queued', through_sequence=0)
        store.conn.execute('UPDATE background_state SET value_json=? WHERE name=?', (encode(saved), 'scene-job:'+scene_id))
    return {'status': 'queued', 'scene_id': scene_id}


async def update_scene_jobs(settings, linker):
    with ExitStack() as stack:
        try:stack.enter_context(scene_job_lock(settings))
        except RuntimeError:return {'status': 'busy'}
        return await _update_scene_jobs(settings, linker)


async def _update_scene_jobs(settings, linker):
    from uuid import uuid4
    from ..work_tasks import failure_reason
    with Store(settings.database, read_only=True) as store:
        rows = store.conn.execute("""
            SELECT j.scene_id, MAX(j.sequence) sequence FROM scene_jobs j
            LEFT JOIN background_state s ON s.name='scene-job:' || j.scene_id
            GROUP BY j.scene_id
            HAVING MAX(j.sequence)>COALESCE(json_extract(s.value_json,'$.through_sequence'),0)
            ORDER BY MIN(j.sequence) LIMIT 100
        """).fetchall()
    scenes = SceneReader(settings.database)
    failed = 0
    for row in rows:
        key, sequence = row['scene_id'], row['sequence']
        # Persist before awaiting the provider. A crash/restart must not silently
        # repeat a request whose response or billing outcome may be unknown.
        state = {'scene_id': key, 'through_sequence': sequence, 'attempt_id': uuid4().hex,
                 'status': 'interrupted', 'updated_at': now(), 'attempts': [],
                 'error': '请求尚未完成，或服务在处理中中断；不会自动重复请求。'}
        def save_state():
            with Store(settings.database) as store:
                store.conn.execute('INSERT INTO background_state VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
                                   ('scene-job:'+key, encode(state)))
        save_state()
        result = {}
        try:
            with Store(settings.database, read_only=True) as store:
                doc = store.read(key)
            if doc is None or doc['lifecycle'] != 'active':
                linker.deactivate_scene_edges(key, reason='scene_inactive',
                    lifecycle_status='archived' if doc and doc['lifecycle']=='archived' else 'cancelled')
            else:
                result = await linker.handle_scene_content_changed(key, scenes)
                if result.get('normal_relink_required') and linker.enabled and linker.auto_enabled:
                    result = await linker.link_scene(key, scenes, EmbeddingCandidates(settings))
                    if result.get('status') in ('failed', 'unavailable'):
                        attempts = result.get('attempts', [])
                        labels = {'invalid_json_contract': '模型返回格式不符合要求（JSON / edges）',
                                  'evidence_contract_failed': '关系或原文证据未通过校验',
                                  'call_failed': '模型请求失败', 'unavailable': '未配置可用的关联模型'}
                        reasons = list(dict.fromkeys(labels.get(a.get('status'), a.get('status','未知错误')) for a in attempts))
                        rejection_labels = {'edge_not_object': '关系条目不是对象',
                            'candidate_not_allowed': 'Scene ID 不在候选中', 'relation_not_allowed': '关系类型无效',
                            'symmetric_orientation_required': '对称关系的方向不匹配',
                            'directed_orientation_required': '有向关系缺少有效方向',
                            'reason_missing': '缺少关联理由',
                            'evidence_not_verbatim': '证据为空或未匹配原文'}
                        reasons.extend(dict.fromkeys(rejection_labels.get(r.get('reason'), r.get('reason','未知原因'))
                            for a in attempts for r in a.get('rejections', [])))
                        state.update(status='failed', attempts=attempts,
                                     error='；'.join(reasons) or '关联处理未成功')
                        save_state();failed += 1
                        logger.warning('Scene relation paused: %s: %s', key, state['error'])
                        continue
            with Store(settings.database) as store, store.transaction(immediate=True):
                store.conn.execute('DELETE FROM scene_jobs WHERE scene_id=? AND sequence<=?', (key, sequence))
                state.update(status='completed', error='', attempts=result.get('attempts', []))
                store.conn.execute('UPDATE background_state SET value_json=? WHERE name=?', (encode(state), 'scene-job:'+key))
        except Exception as exc:
            state.update(status='failed', error=failure_reason(exc))
            save_state();failed += 1
            logger.exception('Scene relation paused; no automatic retry: %s', key)
    return {'status': 'updated' if rows else 'current', 'failed': failed}


class BackgroundJobs:
    def __init__(self, settings, *, features=None):
        self.settings = settings
        features = {'relations','narrative_revision','dreams'} if features is None else features
        from ..deployment import task_model
        from ..model_runtime import TaskClient
        clients=({'selected':TaskClient(settings.database,'relations')}
                 if 'relations' in features and task_model(settings.database,'relations') else {})
        self.linker = SceneLinker(germany_config(settings),clients=clients) if 'relations' in features else None
        if self.linker:
            self.linker.proposal_store(create=True)
        self.scout = Scout(settings) if {'narrative_revision','narrative_scout'} & features else None
        self.dreams = Dreams(settings) if 'dreams' in features else None

    async def run(self):
        next_scan = time.monotonic() + 45
        next_dream = time.monotonic() + 30
        try:
            while True:
                try:
                    if self.linker:
                        await update_scene_jobs(self.settings, self.linker)
                except Exception:
                    logger.exception('Scene background work failed; keeping pending work')
                if self.scout and time.monotonic() >= next_scan:
                    try:
                        await self.scout.run_due()
                    except Exception:
                        logger.exception('Narrative scan failed')
                    next_scan = time.monotonic() + self.scout._narrative_revision_scan_settings()['check_interval_seconds']
                if self.dreams and time.monotonic() >= next_dream:
                    try:
                        await self.dreams.run_due(DreamMaterialReader(self.settings.database),DreamVectors(self.settings))
                    except Exception:
                        logger.exception('Dream generation failed')
                    next_dream=time.monotonic()+self.dreams.check_interval_minutes*60
                await asyncio.sleep(5)
        finally:
            if self.dreams and self.dreams.client:
                await self.dreams.client.close()
            for provider in self.linker.providers if self.linker else []:
                if provider.get('client'):
                    await provider['client'].close()
