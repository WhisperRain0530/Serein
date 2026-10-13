"""Private loopback transport for seven optional text tasks, using ChatGPT login.

No task prompts, provider keys, account details or model output enter logs/status.
This service owns no task scheduler and never retries a model invocation itself.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import hmac
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from codex_event_worker import (DEFAULT_MODEL, WorkerError, child_environment,
    cli_execution, private_write, run_codex_text, strict_json)


TASKS = {'writer', 'persona', 'dreams', 'narrative_scout', 'relations',
         'operit_tagging', 'arc_linker'}
MAX_REQUEST_BYTES = 4 * 1024 * 1024
QUEUE_SECONDS = 45
EXECUTION_SECONDS = 300
TOTAL_SECONDS = 345


def unpack_request(body):
    """Transport labels only: all authored message content remains unchanged."""
    if not isinstance(body, dict) or set(body) - {'task', 'messages', 'response_format', 'timeout_seconds'}:
        raise WorkerError('invalid_task_request')
    task = body.get('task')
    if not isinstance(task, str) or task not in TASKS:
        raise WorkerError('unsupported_task')
    messages = body.get('messages')
    if not isinstance(messages, list) or not messages or len(messages) > 512:
        raise WorkerError('invalid_messages')
    parts = []
    for item in messages:
        if (not isinstance(item, dict) or set(item) != {'role', 'content'}
                or item.get('role') not in {'system', 'developer', 'user', 'assistant'}
                or not isinstance(item.get('content'), str)):
            raise WorkerError('invalid_messages')
        parts.append('<message role="' + item['role'] + '">\n' + item['content'] + '\n</message>')
    if 'response_format' in body:
        response_format = body['response_format']
        if (not isinstance(response_format, dict)
                or response_format.get('type') not in {'text', 'json_object', 'json_schema'}):
            raise WorkerError('invalid_response_format')
        parts.append('<response_format>\n' + json.dumps(response_format, ensure_ascii=False, allow_nan=False)
                     + '\n</response_format>')
    timeout = body.get('timeout_seconds', TOTAL_SECONDS)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= TOTAL_SECONDS):
        raise WorkerError('invalid_timeout')
    return task, '\n'.join(parts), float(timeout)


def authenticated(executable, environ):
    """Inspect CLI login status, never read auth files or return account text."""
    try:
        result = subprocess.run([executable, '-c', 'cli_auth_credentials_store="file"',
            '-c', 'forced_login_method="chatgpt"', 'login', 'status'],
            env=child_environment(environ), capture_output=True, timeout=20)
        return result.returncode == 0 and b'chatgpt' in (result.stdout + result.stderr).lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


class TaskService:
    def __init__(self, directory, token, *, environ=None, executable='codex', probe=authenticated):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise ValueError('Invalid state directory')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == 'posix':
            self.directory.chmod(0o700)
        self.environ = dict(os.environ if environ is None else environ)
        self.lock_file = self.environ.get('SEREIN_CODEX_LOCK_FILE', str(self.directory / 'cli.lock'))
        self.token, self.executable, self.probe = token, executable, probe
        self.mutex = threading.Lock()
        self.active_task = ''
        self.login_ready = False
        self.last_error, self.retry_at = '', 0
        self.cancels = set()
        self.pending = 0
        self.status_path = self.directory / 'status.json'
        if self.status_path.is_file():
            try:
                saved = strict_json(self.status_path.read_bytes())
                # Only fixed codes produced here can be carried across restarts.
                if saved.get('last_error') in {'codex_failed', 'codex_timeout', 'codex_usage_limit',
                    'codex_login_required', 'model_rerouted', 'codex_cli_warning', 'codex_tool_activity',
                    'codex_unavailable', 'incomplete_codex_turn', 'missing_codex_output',
                    'invalid_codex_output', 'codex_output_too_large'}:
                    retry = saved.get('retry_at', 0)
                    if isinstance(retry, (int, float)) and math.isfinite(retry):
                        self.last_error, self.retry_at = saved['last_error'], retry
            except (OSError, ValueError, WorkerError, AttributeError):
                pass
        self.write_status(starting=True)

    def write_status(self, *, starting=False):
        with self.mutex:
            state = ('starting' if starting else 'working' if self.active_task else
                     'retrying' if self.retry_at > time.time() or not self.login_ready else 'idle')
            value = {'state': state, 'updated_at': datetime.now(timezone.utc).isoformat(),
                'model': DEFAULT_MODEL, 'reasoning_effort': 'high',
                'subscription_authenticated': self.login_ready,
                'last_error': self.last_error, 'retry_at': self.retry_at,
                'current_task': self.active_task}
            private_write(self.status_path, value)
            return value

    def refresh_login(self):
        ready = self.probe(self.executable, self.environ)
        with self.mutex:
            self.login_ready = ready
            if ready and self.last_error == 'codex_login_required':
                self.last_error, self.retry_at = '', 0
            elif not ready and not self.last_error:
                self.last_error = 'codex_login_required'
        self.write_status()

    def run(self, task, prompt, deadline, cancel):
        with self.mutex:
            remaining = self.retry_at - time.time()
            if remaining > 0:
                raise WorkerError(self.last_error, retry_after=math.ceil(remaining))
            if not self.login_ready:
                raise WorkerError('codex_login_required', retry_after=60)
        with cli_execution(self.lock_file, timeout=min(QUEUE_SECONDS, max(0, deadline - time.monotonic())),
                           cancel=cancel):
            if cancel.is_set():
                raise WorkerError('codex_cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError('codex_timeout')
            with self.mutex:
                cooling = self.retry_at - time.time()
                if cooling > 0:
                    raise WorkerError(self.last_error, retry_after=math.ceil(cooling))
                self.active_task = task
            self.write_status()
            try:
                with tempfile.TemporaryDirectory(prefix='task-', dir=self.directory) as directory:
                    return run_codex_text(directory, prompt, [], model=DEFAULT_MODEL,
                        executable=self.executable, timeout=min(EXECUTION_SECONDS, remaining),
                        environ=self.environ, cancel=cancel)
            finally:
                with self.mutex:
                    self.active_task = ''
                self.write_status()

    def record_error(self, error):
        if error.code not in {'codex_cancelled', 'codex_queue_timeout', 'codex_queue_full'}:
            with self.mutex:
                self.last_error = error.code
                self.retry_at = time.time() + max(60, error.retry_after)
                if error.code == 'codex_login_required':
                    self.login_ready = False
            self.write_status()


def error_response(error, status=503):
    headers = {'Retry-After': str(max(1, math.ceil(error.retry_after)))} if error.retry_after else None
    return JSONResponse({'error': {'code': error.code, 'message': error.code}}, status_code=status, headers=headers)


def create_app(service):
    async def heartbeat():
        while True:
            await asyncio.to_thread(service.refresh_login)
            for _ in range(6):
                await asyncio.sleep(10)
                service.write_status()

    @asynccontextmanager
    async def lifespan(app):
        beat = asyncio.create_task(heartbeat())
        try:
            yield
        finally:
            for event in list(service.cancels):
                event.set()
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(request):
        value = request.headers.get('authorization', '')
        return bool(service.token) and hmac.compare_digest(value.encode(), ('Bearer ' + service.token).encode())

    @app.get('/health')
    async def health(request: Request):
        if not authorized(request):
            return error_response(WorkerError('unauthorized'), 401)
        return service.write_status()

    @app.post('/complete')
    async def complete(request: Request):
        if not authorized(request):
            return error_response(WorkerError('unauthorized'), 401)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
                    return error_response(WorkerError('request_too_large'), 413)
                raw.extend(chunk)
            task, prompt, timeout = unpack_request(strict_json(raw))
        except (WorkerError, ValueError, TypeError, UnicodeError):
            return error_response(WorkerError('invalid_task_request'), 400)
        if service.pending >= 8:
            return error_response(WorkerError('codex_queue_full'), 503)
        service.pending += 1
        cancel = threading.Event()
        service.cancels.add(cancel)
        job = asyncio.create_task(asyncio.to_thread(service.run, task, prompt, time.monotonic() + timeout, cancel))
        try:
            while not job.done():
                if await request.is_disconnected():
                    cancel.set()
                    with suppress(WorkerError):
                        await asyncio.shield(job)
                    return error_response(WorkerError('codex_cancelled'), 499)
                await asyncio.wait({job}, timeout=.1)
            content = job.result()
            with service.mutex:
                service.last_error, service.retry_at = '', 0
            service.write_status()
            return {'id': 'codex-' + uuid.uuid4().hex, 'object': 'chat.completion',
                'created': int(time.time()), 'model': DEFAULT_MODEL,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                             'finish_reason': 'stop'}]}
        except WorkerError as error:
            service.record_error(error)
            return error_response(error, 504 if error.code in {'codex_timeout', 'codex_queue_timeout'} else 503)
        except asyncio.CancelledError:
            cancel.set()
            with suppress(WorkerError, asyncio.CancelledError):
                await asyncio.shield(job)
            raise
        except Exception:
            error = WorkerError('codex_service_error', retry_after=60)
            service.record_error(error)
            return error_response(error)
        finally:
            cancel.set()
            service.cancels.discard(cancel)
            service.pending -= 1

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--codex-bin', default='codex')
    args = parser.parse_args(argv)
    try:
        token = Path(os.environ['SEREIN_AGENT_TOKEN_FILE']).read_text('utf-8').strip()
        if not token or any(c in token for c in '\r\n'):
            raise ValueError()
        service = TaskService(args.state_dir, token, executable=args.codex_bin)
        import uvicorn
        uvicorn.run(create_app(service), host='127.0.0.1', port=8012, access_log=False,
                    log_level='critical', limit_concurrency=16, timeout_graceful_shutdown=5)
        return 0
    except (OSError, KeyError, ValueError, WorkerError):
        print('Serein subscription task service: configuration_error.', flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
