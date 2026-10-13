"""Host-owned subscription task model and credential-free worker status."""
import json
import math
import os
import stat
import time
from datetime import datetime
from pathlib import Path

CODEX_MODEL_ID = 'serein-codex-sol'
CODEX_TASKS = frozenset({'writer', 'persona', 'dreams', 'narrative_scout',
                         'relations', 'operit_tagging', 'arc_linker'})
CODEX_TASK_TIMEOUT_SECONDS = 360
CODEX_STATUS_MAX_AGE_SECONDS = 90
_STATES = {'starting', 'idle', 'working', 'retrying', 'stopped'}
_ERRORS = {'codex_login_required', 'codex_usage_limit', 'codex_failed', 'codex_timeout',
           'codex_queue_timeout', 'codex_cancelled', 'codex_unavailable', 'codex_state_invalid',
           'codex_output_too_large', 'missing_codex_output', 'invalid_codex_output',
           'invalid_codex_events', 'incomplete_codex_turn', 'model_rerouted',
           'codex_cli_warning', 'codex_tool_activity', 'task_service_busy',
           'codex_service_error', 'codex_queue_full',
           'invalid_task_request', 'unsupported_task', 'invalid_messages',
           'invalid_response_format', 'invalid_timeout', 'worker_configuration_error'}


def codex_tasks_enabled():
    return os.environ.get('SEREIN_CODEX_TASKS_ENABLED', '').strip().lower() in {'1', 'true', 'yes'}


def is_codex_model(model):
    return isinstance(model, dict) and model.get('id') == CODEX_MODEL_ID and model.get('transport') == 'codex_subscription'


def codex_model(task=None):
    if task is not None and task not in CODEX_TASKS:
        raise ValueError('Codex subscription is not available for this task')
    model = {'id': CODEX_MODEL_ID, 'label': 'GPT-5.6 Sol', 'upstream_name': 'Codex 订阅',
             'transport': 'codex_subscription', 'model': 'gpt-5.6-sol',
             'reasoning_effort': 'high', 'base_url': '', 'api_key': '',
             'request_timeout_seconds': CODEX_TASK_TIMEOUT_SECONDS,
             'allowed_tasks': sorted(CODEX_TASKS)}
    return {**model, **({'task': task} if task is not None else {})}


def _timestamp(value):
    try:
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return stamp.timestamp() if stamp.tzinfo is not None else None
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    except (ValueError, OverflowError, OSError):
        return None


def codex_task_status(*, now=None):
    """Read a bounded status snapshot; never open auth files or invoke the CLI."""
    enabled = codex_tasks_enabled()
    result = {'enabled': enabled, 'state': 'unavailable' if enabled else 'disabled',
              'stale': True, 'online': False, 'subscription_authenticated': False}
    filename = os.environ.get('SEREIN_CODEX_TASK_STATE_FILE', '').strip()
    if not enabled or not filename:
        return result
    try:
        path = Path(filename)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            return result
        with path.open('rb') as stream:
            content = stream.read(16385)
        if len(content) > 16384:
            return result
        saved = json.loads(content)
        if not isinstance(saved, dict):
            return result
    except (OSError, ValueError, UnicodeError):
        return result
    stamp = _timestamp(saved.get('updated_at'))
    clock = time.time() if now is None else now
    fresh = stamp is not None and -5 <= clock - stamp <= CODEX_STATUS_MAX_AGE_SECONDS
    state = saved.get('state')
    state = state if isinstance(state, str) and state in _STATES else 'unavailable'
    result.update(state=state if fresh else 'stale', stale=not fresh,
                  online=fresh and state in {'idle', 'working'} and saved.get('subscription_authenticated') is True,
                  subscription_authenticated=fresh and saved.get('subscription_authenticated') is True)
    if stamp is not None:
        result['updated_at'] = stamp
    if saved.get('model') == 'gpt-5.6-sol':
        result['model'] = 'gpt-5.6-sol'
    if saved.get('reasoning_effort') == 'high':
        result['reasoning_effort'] = 'high'
    task = saved.get('current_task')
    result['current_task'] = task if isinstance(task, str) and task in CODEX_TASKS else ''
    error = saved.get('last_error')
    result['last_error'] = error if isinstance(error, str) and error in _ERRORS else ('worker_error' if error else '')
    retry = _timestamp(saved.get('retry_at'))
    if retry is not None:
        result['retry_at'] = retry
    return result
