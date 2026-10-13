"""Existing Germany model jobs read the current canonical database."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import yaml

from ..core.reader import Reader
from .scenes import scene_payload, Scenes


def germany_config(settings):
    filename=settings.background.get('germany_config_file')
    config=yaml.safe_load(Path(filename).read_text('utf-8')) if filename else {}
    if filename and config.get('narrative_rolls',{}).get('scout_role_file'):
        role=Path(config['narrative_rolls']['scout_role_file'])
        if not role.is_absolute():
            config['narrative_rolls']['scout_role_file']=str((Path(filename).parent/role).resolve())
    from ..deployment import identity, available_task_model, read_settings
    from ..codex_config import CODEX_MODEL_ID, codex_tasks_enabled
    result={**config,'serein_database':str(settings.database), 'identity': identity(settings.database)}
    assignments=read_settings(settings.database)['assignments']
    def available(task):
        # Unrelated jobs must still load their configuration after a host disables
        # the subscription service. Never fall back to a legacy API provider.
        if assignments.get(task)==CODEX_MODEL_ID and not codex_tasks_enabled():
            section='scene_linker' if task=='relations' else 'dream'
            result[section]={**config.get(section,{}),'enabled':False,'auto_enabled':False,
                             'models':[],'model':'','base_url':'','api_key':''}
            return None
        return available_task_model(settings.database,task)
    selected=available('relations')
    if selected:
        result['scene_linker']={**config.get('scene_linker',{}),'enabled':True,'auto_enabled':True,
            'models':[{'name':'selected','model':selected['model'],'base_url':selected['base_url'],
                       'protocol':selected.get('protocol','openai'),'api_key':''}]}
    selected=available('dreams')
    if selected:
        result['dream']={**config.get('dream',{}),'enabled':True,'auto_enabled':True,
            'model':selected['model'],'base_url':'','api_key':''}
    return result


class SceneReader:
    def __init__(self,database):
        self.database=database

    async def get(self,key):
        result=Scenes(self.database).read(key)
        return None if result.get('status')=='not_found' else result

    async def list_all(self,include_archive=False):
        with Reader(self.database) as reader:
            items=[]
            for row in reader.store.conn.execute("SELECT id FROM documents WHERE kind='scene' AND lifecycle!='deleted'"):
                doc=reader.read(row[0],kind='scene',with_evidence=False)['document']
                if include_archive or doc['lifecycle']=='active':items.append(scene_payload(doc))
            return items


class DreamMaterialReader:
    """Only newly created, readable prose; no recall or edit timestamps."""

    def __init__(self, database):
        self.database = database

    async def recent(self, now, window_hours=48, limit=5):
        end = now.astimezone(timezone.utc)
        start = end - timedelta(hours=window_hours)
        cutoff = start.isoformat(timespec='seconds')
        ceiling = end.isoformat(timespec='seconds')
        with Reader(self.database) as reader:
            rows = reader.store.conn.execute(
                "SELECT id,kind,created_at FROM documents WHERE kind IN ('event','scene') "
                "AND lifecycle='active' AND julianday(created_at)>=julianday(?) "
                "AND julianday(created_at)<=julianday(?) "
                "ORDER BY julianday(created_at) DESC,id DESC", (cutoff, ceiling))
            materials = []
            for row in rows:
                if len(materials) >= limit:
                    break
                result = reader.read(row['id'], kind=row['kind'], with_evidence=False)
                if not result['readable'] or not result['document']['body_md'].strip():
                    continue
                doc = result['document']
                materials.append({'id': row['id'], 'content': doc['body_md'].strip(),
                    'object_kind': row['kind'], 'metadata': {**doc['metadata'], 'created': row['created_at']}})
            if materials:
                return materials
            # Diaries are a fallback only when no new Event or Scene exists.
            rows = reader.store.conn.execute(
                "SELECT id,created_at FROM diary_entries WHERE kind='diary' AND visibility='active' "
                "AND deleted_at='' AND julianday(created_at)>=julianday(?) "
                "AND julianday(created_at)<=julianday(?) "
                "ORDER BY julianday(created_at) DESC,id DESC", (cutoff, ceiling))
            for row in rows:
                if len(materials) >= limit:
                    break
                result = reader.read(str(row['id']), kind='diary', with_evidence=False, at=end)
                if not result['readable'] or not result['document']['body_md'].strip():
                    continue
                doc = result['document']
                materials.append({'id': 'diary:'+str(row['id']), 'content': doc['body_md'].strip(),
                    'object_kind': 'diary', 'metadata': {'created': row['created_at']}})
            return materials


class EmbeddingCandidates:
    def __init__(self,settings):
        from ..configured_models import effective_settings
        settings=effective_settings(settings)
        self.settings=settings
        self.enabled=bool(settings.embedding)

    async def search_similar(self,query,top_k=12):
        return await asyncio.to_thread(self._search,query,top_k)

    def _search(self,query,limit):
        from ..adapters.embedding import EmbeddingClient
        from ..recall.index import Search
        settings=self.settings
        embedded=EmbeddingClient(settings.database,settings.index,**settings.embedding).query(query)
        with Search(settings.database,settings.index) as search:
            rows=search.search(query,kind='scene',mode='surface',limit=limit,query_embedding=embedded,min_cosine=-1)['items']
        return [(row['id'],row['score']) for row in rows]
