"""Isolated synthetic Event acceptance check; default never invokes a model.

Run with the Serein HTTP dependencies installed. After a separate Codex login,
--run-worker invokes codex_event_worker.py against only this disposable instance.
CODEX_HOME is passed through without reading credentials. No production URL,
configuration, or database is accepted. stdout is one JSON result; generated
synthetic Event text is included for human semantic review, not graded by regex.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from codex_event_worker import DEFAULT_MODEL, NoRedirect, SereinClient, child_environment
from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.compat.raw_archive import raw_archive
from serein.config import Settings
from serein.core.store import Store, digest
from serein.deployment import save_settings

SOURCE = 'codex-event-smoke-synthetic'
SESSION = 'synthetic-library-visit'
DIALOGUE = (
    ('user', '我是林岚，你叫小雨。我和朋友陈安先约了周六下午两点见面，但还在商量去哪儿。陈安提过逛商场，我想去青禾图书馆借《山间来信》，找个安静空间。我担心只借一本书显得这次见面太单薄，又不想为了凑行程一直逛。你怎么看这两个选择？'),
    ('assistant', '我是小雨。我更倾向于按你想怎样度过这次见面来选：商场适合边走边聊，图书馆适合各自翻书、分享读到的一小段。如果你想安静地待一会儿，只借一本也有明确的目的，不必把逛了多少地方当作这次见面的分量。不过这是我对安排的看法，去见陈安的人是你。'),
    ('user', '对，我选图书馆。我想和陈安各找一段喜欢的文字交流，不是按书架逐个打卡；《山间来信》也只借这一本，留点余裕读进去。还有，陈安对花生过敏，我本人没有花生过敏；我不选商场是因为怕吵、容易被逛街带走，不是因为我过敏。借书还没发生，也还没预约。'),
    ('assistant', '这样安排有了取舍依据：重点是安静读一点、交流具体文字，而不是完成一长串地点。我喜欢你说的“留点余裕读进去”，借一本和认真相处并不冲突。花生过敏属于陈安，不能当作你的身体情况。我另有个建议：借完书可以去喝咖啡；这只是我的备选提议，还不是你同意的安排。'),
    ('user', '更正一下：刚刚陈安确认只能周日下午三点，所以改成周日三点去青禾图书馆，周六两点作废。我仍然选只借《山间来信》、交流喜欢的段落，不加打卡项目。我没有答应喝咖啡，这次就想给读书留点余裕；我们还没预约，也还没成行。'),
    ('assistant', '那这次商量落在了你真正想要的节奏上：林岚和朋友陈安计划周日下午三点去青禾图书馆，只借《山间来信》，交流喜欢的文字，不为凑行程追加打卡。周六两点的安排已作废，借书尚未预约或成行。喝咖啡仍只是我提过、你未接受的建议；花生过敏的是陈安，不是你。'),
)
EXPECTATIONS = [
    '林岚是用户，小雨是助手，陈安是朋友；不得交换身份或经历归属。',
    '最终计划为周日下午三点，周六下午两点已经作废。',
    '借书尚未预约、尚未发生，不能写成已成行或已借到。',
    '花生过敏的是陈安，不能写成林岚过敏。',
    '喝咖啡只是助手建议且未获同意，不能写成用户决定。',
    '正文应忠实记事，不添加原话中没有的劝诫、诊断或防御性评价。',
]


class SmokeError(Exception):
    pass


def seed(directory):
    """Only a new, explicitly supplied temporary directory is initialized."""
    database = Path(directory) / 'synthetic.db'
    if database.exists():
        raise SmokeError('database_not_empty')
    settings = Settings(database, writable=True, extensions={'handoff': {'enabled': True}})
    initialize(settings)
    save_settings(database, {'identity': {'user_name': '林岚', 'ai_name': '小雨'},
                            'pipeline': {'execution_mode': 'agent', 'auto_enabled': True}})
    start = datetime.now(timezone.utc) - timedelta(minutes=15)
    rows = [dict(source_event_id=f'synthetic-{index + 1}', session_id=SESSION,
                 role=role, text=text, created_at=(start + timedelta(minutes=index)).isoformat())
            for index, (role, text) in enumerate(DIALOGUE)]
    raw_archive(settings).ingest(rows, source=SOURCE)
    return settings, rows


@contextmanager
def local_server(settings, token):
    """Keep the reserved socket open: no find-free-port/rebind race."""
    import uvicorn
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(settings, token=token, live=True),
            host='127.0.0.1', port=port, access_log=False, log_level='critical',
            log_config=None, timeout_graceful_shutdown=5))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise SmokeError('temporary_server_failed')
                time.sleep(.02)
            yield f'http://127.0.0.1:{port}'
        finally:
            server.should_exit = True
            thread.join(10)
            if thread.is_alive():
                server.force_exit = True
                thread.join(5)
            if thread.is_alive():
                raise SmokeError('temporary_server_shutdown_failed')


def launch_worker(url, token, state, *, model, executable, timeout):
    env = child_environment()
    # Ignore all inherited Serein settings, token files and production endpoints.
    env.update(SEREIN_AGENT_URL=url, SEREIN_AGENT_TOKEN=token,
               NO_PROXY='127.0.0.1,localhost,::1', PYTHONUTF8='1')
    command = [sys.executable, str(ROOT / 'scripts' / 'codex_event_worker.py'),
               '--once', '--include-recent', '--state-dir', str(state),
               '--model', model, '--codex-bin', executable, '--max-jobs', '24',
               '--timeout', str(timeout)]
    with subprocess.Popen(command, env=env, cwd=state, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        try:
            return process.wait(timeout=timeout * 24 + 60)
        finally:
            if process.poll() is None:
                process.terminate()  # The worker terminates its active Codex child.
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def verify(settings, seeded):
    """Verify real stored Event/source relations, without guessing semantics."""
    expected = {row['source_event_id']: row for row in seeded}
    events = []
    bound = set()
    with Store(settings.database, read_only=True) as store:
        originals = store.conn.execute('SELECT * FROM raw_events ORDER BY id').fetchall()
        if len(originals) != len(seeded) or any(
            row['source'] != SOURCE or row['session_id'] != SESSION
            or row['source_event_id'] not in expected
            or row['text'] != expected[row['source_event_id']]['text']
            or row['role'] != expected[row['source_event_id']]['role'] for row in originals
        ):
            raise SmokeError('synthetic_archive_changed')
        for item in store.conn.execute("SELECT id FROM documents WHERE kind='event' AND lifecycle='active'"):
            document = store.read(item['id'])
            bindings = store.conn.execute(
                'SELECT s.content,s.content_sha256,s.source_key,b.metadata_json '
                'FROM evidence_bindings b JOIN sources s ON s.id=b.source_id '
                'WHERE b.document_id=? AND b.active=1', (item['id'],)).fetchall()
            if not bindings or not document['body_md'].strip():
                raise SmokeError('event_without_body_or_sources')
            source_ids = []
            for binding in bindings:
                key = json.loads(binding['source_key'])
                metadata = json.loads(binding['metadata_json'])
                if not isinstance(key, list) or len(key) != 3 or key[:2] != [SOURCE, SESSION] or key[2] not in expected:
                    raise SmokeError('foreign_source_binding')
                row = expected[key[2]]
                if (binding['content'] != row['text'] or binding['content_sha256'] != digest(row['text'])
                        or metadata.get('role') != row['role']):
                    raise SmokeError('incorrect_source_binding')
                bound.add(key[2])
                source_ids.append(key[2])
            events.append({'id': item['id'], 'title': document['title'],
                           'body': document['body_md'], 'source_ids': sorted(source_ids)})
        if not events:
            raise SmokeError('no_event_created')
        # Both the corrected user statement and final assistant confirmation must
        # remain available as exact sources; this does not grade the generated prose.
        if not {'synthetic-5', 'synthetic-6'} <= bound:
            raise SmokeError('correction_sources_not_bound')
        unfinished = store.conn.execute("SELECT count(*) FROM pipeline_batches WHERE status='pending'").fetchone()[0]
        if unfinished:
            raise SmokeError('pipeline_not_drained')
    return {'status': 'structural_pass', 'event_count': len(events), 'bound_source_count': len(bound),
            'events': events, 'semantic_review': {'status': 'required', 'expectations': EXPECTATIONS}}


def run_smoke(*, run_worker=False, temp_root=None, model=DEFAULT_MODEL, executable='codex', timeout=900):
    with tempfile.TemporaryDirectory(prefix='serein-codex-smoke-', dir=temp_root) as temporary:
        directory = Path(temporary)
        settings, rows = seed(directory)
        state = directory / 'worker'
        state.mkdir(mode=0o700)
        token = secrets.token_urlsafe(32)
        with local_server(settings, token) as url:
            client = SereinClient(url, token, timeout=30,
                                  opener=build_opener(ProxyHandler({}), NoRedirect()))
            task = client.call('pipeline_next', {'include_recent': True})
            if task.get('status') != 'awaiting_agent' or task.get('role') != 'track_router':
                raise SmokeError('unexpected_first_pipeline_task')
            if not run_worker:
                result = {'status': 'ready', 'model_called': False, 'seeded_messages': len(rows),
                          'first_role': task['role'], 'event_acceptance': 'not_run'}
            else:
                exit_code = launch_worker(url, token, state, model=model, executable=executable, timeout=timeout)
                if exit_code:
                    # Error codes are worker-owned fixed strings; never dump process output.
                    saved = json.loads((state / 'status.json').read_text('utf-8')) if (state / 'status.json').exists() else {}
                    code = saved.get('last_error', '')
                    result = {'status': 'failed', 'error': code if isinstance(code, str) and code.replace('_', '').isalnum()
                              else 'worker_failed', 'worker_exit_code': exit_code}
                else:
                    result = verify(settings, rows)
                    result['model'] = model
        result.update(isolated=True, temporary_data_removed=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-worker', action='store_true', help='Explicitly use the logged-in Codex subscription.')
    parser.add_argument('--temp-root', type=Path, help='Parent for a new disposable directory; never an existing database.')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--codex-bin', default='codex')
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args(argv)
    try:
        if args.timeout < 1:
            raise SmokeError('invalid_timeout')
        result = run_smoke(run_worker=args.run_worker, temp_root=args.temp_root,
                           model=args.model, executable=args.codex_bin, timeout=args.timeout)
    except KeyboardInterrupt:
        result = {'status': 'failed', 'error': 'interrupted'}
    except SmokeError as error:
        result = {'status': 'failed', 'error': str(error)}
    except Exception:
        result = {'status': 'failed', 'error': 'smoke_runtime_error'}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
