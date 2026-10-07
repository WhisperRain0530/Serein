"""One persistent Zeabur instance; supervise core and gateway without logging secrets."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler


ROOT = Path(__file__).resolve().parents[1]
MEMORY_PORT = 8011
PREVIEW_PORT = 4173


def _write_private_once(path, text):
    """Create without replacing existing instance configuration or credentials."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
        target.write(text)


def _password_hash(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32).hex()


def prepare(data_dir, *, root=ROOT, username='kitty'):
    """Initialize files only; launch.py initializes the database before serving."""
    data_dir = Path(data_dir).resolve()
    if not username or ':' in username or any(ord(char) < 32 for char in username) or len(username) > 128:
        raise ValueError('Web username must be 1–128 characters without colons or control characters.')
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret_dir = data_dir / 'secrets'
    secret_dir.mkdir(exist_ok=True, mode=0o700)
    files = [secret_dir / name for name in ('api-token', 'web-password', 'web-auth.json')]
    if (data_dir / 'serein.db').exists() and not all(path.is_file() for path in files):
        raise RuntimeError('Existing instance credentials are missing; restore its secrets from backup before starting.')
    for path in files:
        if path.is_symlink():
            raise RuntimeError('Credential files must not be symbolic links.')
    _write_private_once(files[0], secrets.token_urlsafe(36))
    _write_private_once(files[1], secrets.token_urlsafe(32))
    password = files[1].read_text('utf-8').strip()
    if not password or not files[0].read_text('utf-8').strip():
        raise RuntimeError('Existing instance credentials are empty; restore them before starting.')
    if not files[2].exists():
        salt = secrets.token_bytes(16)
        _write_private_once(files[2], json.dumps({'username':username, 'salt':salt.hex(),
                                                'hash':_password_hash(password, salt)}))
    config = data_dir / 'config.toml'
    if not config.exists():
        text = (root / 'config.example.toml').read_text('utf-8')
        text = text.replace('./.runtime/serein.db', (data_dir / 'serein.db').as_posix())
        text = text.replace('./.runtime/recall-index.sqlite', (data_dir / 'recall-index.sqlite').as_posix())
        _write_private_once(config, text)
    (data_dir / 'legacy-input').mkdir(exist_ok=True, mode=0o700)
    # This volume contains original dialogue and plaintext provider credentials.
    if os.name != 'nt':
        data_dir.chmod(0o700)
        secret_dir.chmod(0o700)
        for path in [*files, config]:
            path.chmod(0o600)
    read_credentials(data_dir)  # Detect an incomplete or inconsistent restore.
    return config


def read_credentials(data_dir):
    """No writes and no output; only the explicit credentials command prints this."""
    directory = Path(data_dir) / 'secrets'
    try:
        token = (directory / 'api-token').read_text('utf-8').strip()
        password = (directory / 'web-password').read_text('utf-8').strip()
        auth = json.loads((directory / 'web-auth.json').read_text('utf-8'))
        valid = hmac.compare_digest(auth['hash'], _password_hash(password, bytes.fromhex(auth['salt'])))
        if not token or not password or not auth['username'] or not valid:
            raise ValueError('Invalid credentials')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('Saved connection credentials are unavailable or inconsistent; restore the matching secrets files.') from exc
    return {'username':auth['username'], 'password':password, 'gateway_key':token}


def gateway_port(env):
    try:
        port = int(env.get('PORT') or '8080')
    except (TypeError, ValueError):
        raise ValueError('PORT must be a valid gateway port.') from None
    if not 1 <= port <= 65535 or port in (MEMORY_PORT, PREVIEW_PORT):
        raise ValueError('PORT must be 1–65535 and different from the internal service ports.')
    return port


def runtime_environment(data_dir, env):
    data_dir = Path(data_dir).resolve()
    result = dict(env)
    # Always use this volume's credentials, never an unrelated inherited token.
    result.pop('SEREIN_HTTP_TOKEN', None)
    result.pop('SEREIN_MEMORY_TOKEN', None)
    result.update(PYTHONUNBUFFERED='1', PYTHONUTF8='1',
        SEREIN_HTTP_TOKEN_FILE=str(data_dir / 'secrets' / 'api-token'),
        SEREIN_MEMORY_TOKEN_FILE=str(data_dir / 'secrets' / 'api-token'),
        SEREIN_WEB_AUTH_FILE=str(data_dir / 'secrets' / 'web-auth.json'),
        SEREIN_MEMORY_URL=f'http://127.0.0.1:{MEMORY_PORT}',
        SEREIN_PREVIEW_PORT=str(PREVIEW_PORT),
        SEREIN_GATEWAY_PORT=str(gateway_port(env)), SEREIN_GATEWAY_BIND='0.0.0.0',
        SEREIN_ROUTE_DRAFT_FILE=str(data_dir / 'semantic-route-draft.json'),
        SEREIN_MIGRATION_PATH_ROOT=str(data_dir / 'legacy-input'))
    return result


def http_ready(url):
    try:
        # Loopback checks must never go through a hosting provider's HTTP proxy.
        with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
            return response.status == 200
    except (OSError, HTTPError, URLError):
        return False


def wait_for_core(child, stop, *, timeout=60):
    deadline = time.monotonic() + timeout
    while not stop.is_set():
        if child.poll() is not None:
            raise RuntimeError('Memory service exited before becoming ready.')
        if http_ready(f'http://127.0.0.1:{MEMORY_PORT}/health'):
            return True
        if time.monotonic() >= deadline:
            raise RuntimeError('Memory service did not become ready within the startup timeout.')
        stop.wait(.2)
    return False


def stop_children(children):
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 10
    for child in reversed(children):
        try:
            child.wait(timeout=max(.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def supervise(data_dir, stop, *, root=ROOT, env=None):
    child_env = runtime_environment(data_dir, os.environ if env is None else env)
    commands = [
        [sys.executable, '-m', 'serein.launch', '--config', str(Path(data_dir) / 'config.toml'),
         'http', '--live', '--host', '127.0.0.1', '--port', str(MEMORY_PORT)],
        ['node', str(root / 'web' / 'server' / 'gateway.mjs')],
    ]
    children = []
    try:
        core = subprocess.Popen(commands[0], cwd=root, env=child_env)
        children.append(core)
        if not wait_for_core(core, stop):
            return 0
        children.append(subprocess.Popen(commands[1], cwd=root, env=child_env))
        print('Serein started; connection credentials are available through the explicit credentials command.', flush=True)
        while not stop.wait(.25):
            for name, child in zip(('Memory', 'Gateway'), children):
                code = child.poll()
                if code is not None:
                    print(f'{name} service exited; stopping this instance for restart.', file=sys.stderr, flush=True)
                    return 1
        return 0
    finally:
        stop_children(children)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='start', choices=('start', 'init', 'credentials', 'healthcheck'))
    parser.add_argument('--data-dir', type=Path, default=Path('/data'))
    args = parser.parse_args(argv)
    try:
        if args.action == 'healthcheck':
            return 0 if http_ready(f'http://127.0.0.1:{gateway_port(os.environ)}/ready') else 1
        if args.action == 'credentials':
            credentials = read_credentials(args.data_dir)
            origin = os.environ.get('SEREIN_PUBLIC_ORIGIN', '').rstrip('/')
            if origin:
                credentials.update(web_url=origin, api_base_url=origin + '/v1')
            print(json.dumps(credentials, ensure_ascii=False, indent=2))
            return 0
        from serein.file_lock import exclusive_lock
        args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive_lock(args.data_dir / 'service.lock'):
            prepare(args.data_dir, username=os.environ.get('SEREIN_WEB_USERNAME', 'kitty'))
            if args.action == 'init':
                print('Serein instance files are ready. No services were started.')
                return 0
            stop = threading.Event()
            previous = {sig:signal.signal(sig, lambda _signum, _frame:stop.set())
                        for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                return supervise(args.data_dir, stop)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    except (OSError, ValueError, RuntimeError) as exc:
        # Never print generated secrets, child environments or credential records.
        print(f'Serein startup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
