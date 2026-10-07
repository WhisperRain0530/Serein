"""No model or credential reads: exercise the real temporary HTTP/pipeline path."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_public_features import output_for

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('codex_event_smoke', ROOT / 'scripts' / 'codex_event_smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_default_only_freezes_local_task(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Offline mode must never launch Codex')
    monkeypatch.setattr(smoke, 'launch_worker', forbidden)
    monkeypatch.setenv('SEREIN_AGENT_URL', 'https://production.invalid')
    monkeypatch.setenv('SEREIN_AGENT_TOKEN_FILE', str(tmp_path / 'must-not-read'))
    result = smoke.run_smoke(temp_root=tmp_path)
    assert result['status'] == 'ready' and result['model_called'] is False
    assert result['first_role'] == 'track_router'
    assert result['seeded_messages'] == 6
    assert list(tmp_path.iterdir()) == []


def test_real_http_pipeline_binds_exact_synthetic_sources(tmp_path, monkeypatch):
    import codex_event_worker as worker
    calls = []

    def fake_launch(url, token, state, **options):
        assert url.startswith('http://127.0.0.1:')
        def run(task):
            calls.append(task['role'])
            return output_for(task['role'], task['request'])
        client = smoke.SereinClient(url, token, opener=smoke.build_opener(smoke.ProxyHandler({}), smoke.NoRedirect()))
        runner = SimpleNamespace(model='synthetic-offline', run=run)
        return worker.Worker(client, runner, state).run(once=True, include_recent=True, max_jobs=24)

    monkeypatch.setattr(smoke, 'launch_worker', fake_launch)
    result = smoke.run_smoke(run_worker=True, temp_root=tmp_path)
    assert result['status'] == 'structural_pass'
    assert result['event_count'] == 1 and result['bound_source_count'] == 6
    assert calls == ['track_router', 'event_curator', 'event_writer']
    assert result['semantic_review']['status'] == 'required'
    assert list(tmp_path.iterdir()) == []


def test_no_events_is_failure_and_database_cannot_be_reused(tmp_path):
    settings, rows = smoke.seed(tmp_path)
    with pytest.raises(smoke.SmokeError, match='no_event_created'):
        smoke.verify(settings, rows)
    with pytest.raises(smoke.SmokeError, match='database_not_empty'):
        smoke.seed(tmp_path)


def test_worker_failure_is_machine_readable_and_temp_is_removed(tmp_path, monkeypatch):
    def fake_launch(url, token, state, **options):
        (state / 'status.json').write_text('{"last_error":"codex_login_required"}', encoding='utf-8')
        return 1
    monkeypatch.setattr(smoke, 'launch_worker', fake_launch)
    result = smoke.run_smoke(run_worker=True, temp_root=tmp_path)
    assert result['status'] == 'failed' and result['error'] == 'codex_login_required'
    assert result['temporary_data_removed'] is True
    assert list(tmp_path.iterdir()) == []
