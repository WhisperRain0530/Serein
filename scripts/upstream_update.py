"""Pull the public main branch with Git and rebuild the existing installation."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import types
from uuid import uuid4
import zipfile

# Keep this fork's fixes when the installation menu pulls an update.
REPOSITORY = 'WhisperRain0530/Serein'
REMOTE = f'https://github.com/{REPOSITORY}.git'
BRANCH = 'main'
MAX_SOURCE_BYTES = 300 * 1024 * 1024
REQUIRED = {'release-files.json', 'scripts/manage.py',
            'scripts/upstream_update.py', 'scripts/runtime_backup.py', 'scripts/one_click.sh', 'scripts/one_click.ps1'}
DENIED = {'.git', '.env', '.local', '.runtime', '.venv', 'node_modules', '__pycache__',
          'runtime', 'secrets', 'backups', 'output', 'config.toml', 'installation.json'}


def git(*args):
    try:
        result = subprocess.run(['git', *map(str, args)], check=True, capture_output=True, timeout=300,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'})
        return result.stdout.decode('utf-8').strip()
    except FileNotFoundError as exc:
        raise ValueError('更新需要 Git，请先安装 Git 后重试') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Git 拉取超时，当前服务和源码尚未更新') from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', errors='replace').strip()
        raise ValueError('Git 拉取未完成：' + detail) from exc


def latest_commit():
    ref = 'refs/heads/' + BRANCH
    rows = git('ls-remote', '--exit-code', REMOTE, ref).splitlines()
    for row in rows:
        parts = row.split()
        if len(parts) == 2 and parts[1] == ref and re.fullmatch(r'[0-9a-f]{40}', parts[0]):
            return parts[0]
    raise ValueError('无法确定上游 main 的提交，未开始更新')


def safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('源码清单含无效路径')
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('源码清单含越界路径')
    if any(p.endswith(('.', ' ')) or re.search(r'[\x00-\x1f<>"|?*]', p) or
           re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p) for p in path.parts):
        raise ValueError('源码清单含不兼容的文件名')
    if path.parts[0].lower() == 'data' or any(p.lower() in DENIED for p in path.parts) or path.suffix.lower() in ('.db', '.sqlite', '.sqlite3', '.log'):
        raise ValueError('源码清单包含实例数据或配置')
    if path.parts[0] == 'deploy' and name not in ('deploy/compose.yaml', 'deploy/Gateway.Dockerfile'):
        raise ValueError('源码清单包含未允许的部署文件')
    return name


def destination(root, name):
    safe_name(name)
    root = Path(root).resolve()
    target = root / name
    # Resolve each existing component: reject symlinks and Windows junctions.
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink() or current.resolve() != current:
            raise ValueError('源码目标含符号链接或目录联接：' + name)
    if not target.resolve().is_relative_to(root):
        raise ValueError('源码目标超出当前目录')
    return target


def manifest(content):
    names = json.loads(content)
    if not isinstance(names, list) or not names or len(names) > 10000:
        raise ValueError('源码清单格式无效')
    names = [safe_name(name) for name in names]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError('源码清单包含重复或大小写冲突路径')
    return names


def prepare(commit, folder):
    if not re.fullmatch(r'[0-9a-f]{40}', commit or ''):
        raise ValueError('上游提交编号无效')
    staged = Path(folder) / 'source'
    git('init', '--quiet', '--template=', staged)
    # A fresh shallow fetch also works for extracted installs and rewritten
    # public history. Never merge old checkout history into the running source.
    git('-C', staged, '-c', 'fetch.fsckObjects=true', 'fetch', '--quiet', '--depth=1', REMOTE, commit)
    git('-C', staged, '-c', 'core.autocrlf=false', 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
    if git('-C', staged, 'rev-parse', 'HEAD') != commit:
        raise ValueError('拉取的代码与选定提交不一致')
    modes = {}
    for entry in git('-C', staged, 'ls-tree', '-r', '-z', '--full-tree', 'HEAD').split('\0'):
        if entry:
            metadata, name = entry.split('\t', 1)
            modes[name] = metadata.split()[0]
    if modes.get('release-files.json') not in ('100644', '100755'):
        raise ValueError('上游源码缺少普通文件形式的源码清单')
    names = manifest((staged / 'release-files.json').read_text(encoding='utf-8'))
    if not REQUIRED.issubset(names):
        raise ValueError('上游源码清单缺少更新所需文件')
    total = 0
    for name in names:
        if modes.get(name) not in ('100644', '100755'):
            raise ValueError('源码清单包含未跟踪文件、链接或子模块：' + name)
        source = destination(staged, name)
        total += source.stat().st_size
        if total > MAX_SOURCE_BYTES:
            raise ValueError('上游源码过大，未开始更新')
        source.chmod(0o755 if modes[name] == '100755' else 0o644)
    return staged, names


def plan_files(root, names, staged=None):
    root = Path(root).resolve()
    old_names = manifest((root / 'release-files.json').read_text(encoding='utf-8'))
    state_path = root / 'deploy' / 'update-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    managed = state.get('source_hashes')
    interrupted = state.get('phase') == 'applying' and managed and staged is not None
    if interrupted:
        old_names = [safe_name(name) for name in managed]
    all_names = sorted(set(old_names) | set(names))
    for name in all_names:
        target = destination(root, name)
        if target.exists() and not target.is_file():
            raise ValueError('源码目标不是普通文件：' + name)
        if target.exists() and name not in old_names and not interrupted:
            raise ValueError('新版文件与本地自建文件重名，请先移开：' + name)
    if interrupted:
        # After a power loss, files may be either the previous or target release.
        # Accept only those two known versions; unrelated edits still stop here.
        for name in all_names:
            target = destination(root, name)
            actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
            incoming = hashlib.sha256((staged / name).read_bytes()).hexdigest() if name in names else None
            if actual not in (managed.get(name), incoming):
                raise ValueError('中断更新的源码包含额外修改，请先保存：' + name)
    elif managed:
        if managed != source_hashes(root):
            raise ValueError('安装源码在上次更新后被修改，请先保存；未覆盖本地源码')
    elif (root / '.git').exists():
        dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no'])
        if dirty.strip():
            raise ValueError('此 Git 工作区有未提交改动，请先保存；未覆盖本地源码')
    return all_names, sorted(set(old_names) - set(names))


def source_hashes(root):
    names = manifest((root / 'release-files.json').read_text(encoding='utf-8'))
    return {name: hashlib.sha256(destination(root, name).read_bytes()).hexdigest()
            if destination(root, name).is_file() else None for name in names}


def backup_sources(root, names):
    folder = Path(root) / 'deploy' / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    if folder.resolve() != folder or folder.is_symlink():
        raise ValueError('备份目录不能是符号链接或目录联接')
    path = folder / ('source-' + uuid4().hex + '.zip')
    with path.open('xb') as handle:
        os.chmod(path, 0o600)
        with zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                target = destination(root, name)
                if target.is_file():
                    archive.write(target, name)
    return path


def replace_file(target, data, mode=0o644):
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(target.name + '.update-' + uuid4().hex)
    try:
        pending.write_bytes(data)
        pending.chmod(mode)
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def apply_files(root, staged, names, obsolete, backup):
    try:
        for name in names:
            source = staged / name
            replace_file(destination(root, name), source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
        for name in obsolete:
            destination(root, name).unlink(missing_ok=True)
    except BaseException:
        with zipfile.ZipFile(backup) as archive:
            saved = set(archive.namelist())
            for name in set(names) | set(obsolete):
                target = destination(root, name)
                if name in saved:
                    info = archive.getinfo(name)
                    replace_file(target, archive.read(name), stat.S_IMODE(info.external_attr >> 16))
                else:
                    target.unlink(missing_ok=True)
        raise


def load_source(path):
    path = Path(path)
    module = types.ModuleType('updated_serein_' + path.stem)
    module.__file__ = str(path)
    # Read the new source, not a potentially cached pre-update .pyc file.
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def load_manager(root):
    return load_source(Path(root) / 'scripts' / 'manage.py')


def backup_runtime(staged, deploy):
    # Use the verified new backup helper, including lock-file handling, even
    # when the installed updater is from an older release.
    path = load_source(staged / 'scripts' / 'runtime_backup.py').snapshot(deploy)
    if path:
        print('数据备份已保存：' + str(path), flush=True)
    return path


def run_update(manager):
    root = Path(manager.ROOT).resolve()
    if not (root / 'deploy' / 'config.toml').is_file():
        raise ValueError('请先部署当前实例，再检查上游更新')
    commit = latest_commit()
    state_path = root / 'deploy' / 'update-state.json'
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    current = previous.get('commit', '')
    print(f'当前提交：{current[:12] if current else "尚未记录（首次接入 Git 更新）"}；main 最新：{commit[:12]}')
    if current == commit and previous.get('phase') == 'complete':
        if previous.get('source_hashes') != source_hashes(root):
            raise ValueError('源码在上次更新后被修改，请先保存；未覆盖本地源码')
        print('当前已是 main 最新提交，无须重复拉取和构建。')
        return False
    choice = manager.choose('拉取 main 代码并重建', [('1', '先备份再更新（默认）'),
                            ('0', '跳过数据备份直接更新')], default='1', back=True)
    if choice == 'r':
        return False
    manager.ensure_tools()
    print('用 Git 拉取 main 代码；这一阶段保持当前服务运行。', flush=True)
    with tempfile.TemporaryDirectory(prefix='serein-update-') as temporary:
        staged, names = prepare(commit, temporary)
        all_names, obsolete = plan_files(root, names, staged)
        backup = backup_sources(root, all_names)
        state = {'commit': commit, 'branch': BRANCH, 'repository': REPOSITORY, 'source_backup': str(backup)}
        def record(phase):
            state['phase'] = phase
            manager.private_file(state_path, json.dumps({**state, 'source_hashes': source_hashes(root)}))
        record('prepared')
        fresh = None
        try:
            manager.service_action('stop')
            if choice == '1':
                state['data_backup'] = str(backup_runtime(staged, root / 'deploy') or '')
            record('applying')
            apply_files(root, staged, names, obsolete, backup)
            fresh = load_manager(root)
            record('building')
            fresh.ensure_tools()
            fresh.build_runtime(backup=False)
            record('starting')
            fresh.service_action('up')
            fresh.check_memory()
            record('complete')
        except BaseException:
            if fresh and state.get('phase') == 'starting':
                try:
                    fresh.service_action('stop')
                except Exception:
                    print('服务启动失败，停止命令也未成功；请用菜单 4 检查当前服务状态。')
            record('failed')
            print('更新未完成。源码备份：' + str(backup) + '；数据备份（如选择）在 deploy/backups。')
            print('请查看错误后重试更新；构建失败不会自动启动半更新服务。', flush=True)
            raise
    print('上游更新完成：main @ ' + commit[:12] + '。重新输入 se 可打开新版管理菜单。')
    return True


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='在原安装目录内接入或运行 Serein 上游更新')
    parser.add_argument('--root', type=Path, required=True, help='已有实例的安装目录（包含 deploy 和 scripts）')
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        manager = load_manager(root)
        locks = load_source(root / 'src' / 'serein' / 'file_lock.py')
        with locks.exclusive_lock(root / 'deploy' / 'runtime' / 'installer.lock'):
            run_update(manager)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('更新未完成：' + str(exc))
        return 1
    except KeyboardInterrupt:
        print('已取消更新。')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
