"""Private, non-streaming transport for configured Codex subscription tasks.

Original messages and response contracts are forwarded unchanged. The CLI does
not implement API temperature/max_tokens controls; they are not translated into
new prompts or represented as equivalent sampling behavior.
"""
import os
from pathlib import Path

import httpx

from .codex_config import CODEX_TASKS, CODEX_TASK_TIMEOUT_SECONDS


ENDPOINT = 'http://127.0.0.1:8012/complete'
SERVICE_TIMEOUT_SECONDS = 345


class CodexTaskError(ValueError):
    """Only fixed diagnostics, never service bodies, credentials or model text."""
    def __init__(self, code, *, status_code=None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _token():
    filename = os.environ.get('SEREIN_CODEX_TASK_TOKEN_FILE', '')
    if not filename:
        raise CodexTaskError('codex_task_token_not_configured')
    try:
        value = Path(filename).read_text('utf-8').strip()
    except (OSError, UnicodeError):
        raise CodexTaskError('codex_task_token_unavailable') from None
    if not value or len(value) > 4096 or any(not 33 <= ord(char) <= 126 for char in value):
        raise CodexTaskError('codex_task_token_invalid')
    return value


async def complete(model, payload):
    task = model.get('task')
    if task not in CODEX_TASKS:
        raise CodexTaskError('codex_task_not_allowed')
    if payload.get('stream') or payload.get('tools') or payload.get('tool_choice'):
        raise CodexTaskError('codex_task_unsupported_request')
    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages:
        raise CodexTaskError('codex_task_invalid_messages')
    body = {'task': task, 'messages': messages, 'timeout_seconds': SERVICE_TIMEOUT_SECONDS}
    if payload.get('response_format') is not None:
        body['response_format'] = payload['response_format']
    # No environment proxies, redirect following or retries. Cancellation closes
    # this request so the service can cancel queued/running CLI work as well.
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(CODEX_TASK_TIMEOUT_SECONDS, connect=5, pool=5),
            trust_env=False, follow_redirects=False,
        ) as client:
            response = await client.post(ENDPOINT, headers={'Authorization': 'Bearer ' + _token()}, json=body)
    except httpx.TimeoutException:
        raise CodexTaskError('codex_task_timeout') from None
    except httpx.HTTPError:
        raise CodexTaskError('codex_task_unavailable') from None
    if not response.is_success:
        code = ('codex_task_authentication' if response.status_code in (401, 403) else
                'codex_task_timeout' if response.status_code == 504 else
                'codex_task_unavailable' if response.status_code >= 500 else 'codex_task_rejected')
        raise CodexTaskError(code, status_code=response.status_code)
    try:
        result = response.json()
        choices = result['choices']
        message = choices[0]['message']
        if (not isinstance(choices, list) or len(choices) != 1
                or not isinstance(message.get('content'), str) or not message['content'].strip()
                or message.get('tool_calls') or choices[0].get('finish_reason') != 'stop'):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise CodexTaskError('codex_task_invalid_response') from None
    return result
