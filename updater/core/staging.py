# -*- coding: utf-8 -*-
"""版本比较、带恢复日志的逐文件替换及回滚。"""
import os
import shutil
import json
from pathlib import Path

JOURNAL = '.update-transaction.json'


def _save(root, state):
    temporary = root / (JOURNAL + '.tmp')
    with temporary.open('w', encoding='utf-8') as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, root / JOURNAL)


def _destination(root, relative):
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('事务路径超出目标目录')
    return path


def _restore(root, state):
    count = 0
    for row in reversed(state['files']):
        if row['phase'] == 'queued':
            continue
        dest = _destination(root, row['path'])
        backup = dest.with_name(dest.name + '.old')
        if row['existed']:
            if backup.exists():
                os.replace(backup, dest)
                count += 1
            elif not dest.exists():
                raise RuntimeError('旧文件和备份同时缺失，保留事务日志等待处理')
        elif row['phase'] in ('installing', 'installed') and dest.exists():
            dest.unlink()
            count += 1
        row['phase'] = 'queued'
        _save(root, state)
    (root / JOURNAL).unlink()
    return count


def recover_pending(target):
    root = Path(target).resolve()
    journal = root / JOURNAL
    if not journal.exists():
        return 0
    state = json.loads(journal.read_text(encoding='utf-8'))
    if state['status'] == 'committed':
        return 0
    return _restore(root, state)


def version_tuple(v: str):
    return tuple(int(x) for x in str(v).split(".") if x.isdigit())


def is_newer(remote: str, local: str) -> bool:
    return version_tuple(remote) > version_tuple(local)


def apply_staging(staging: str, target: str) -> list:
    """Validated staging files only. Exceptions roll back; crashes leave a journal."""
    stage, root = Path(staging).resolve(), Path(target).resolve()
    if not stage.is_dir():
        raise ValueError('staging 目录不存在')
    if stage == root or stage.is_relative_to(root) or root.is_relative_to(stage):
        raise ValueError('staging 与目标目录必须互不嵌套')
    root.mkdir(parents=True, exist_ok=True)
    recover_pending(root)
    if (root / JOURNAL).exists():
        raise RuntimeError('上次事务备份尚未确认；请先校验并清理备份或回滚')
    rows = []
    for source in sorted(stage.rglob('*')):
        if source.is_symlink():
            raise ValueError('staging 不允许符号链接')
        if not source.is_file():
            continue
        relative = source.relative_to(stage).as_posix()
        dest = _destination(root, relative)
        if relative in (JOURNAL, JOURNAL + '.tmp') or dest.name.endswith('.old'):
            raise ValueError('清单占用更新器保留文件名')
        if dest.exists() and not dest.is_file():
            raise ValueError('目标不是普通文件')
        if dest.with_name(dest.name + '.old').exists():
            raise RuntimeError('不能覆盖已有回滚备份')
        rows.append({'path': relative, 'existed': dest.exists(), 'phase': 'queued'})
    if not rows:
        return []
    state = {'status': 'applying', 'files': rows}
    _save(root, state)
    try:
        for row in rows:
            dest = _destination(root, row['path'])
            dest.parent.mkdir(parents=True, exist_ok=True)
            row['phase'] = 'backing-up'
            _save(root, state)
            if row['existed']:
                os.replace(dest, dest.with_name(dest.name + '.old'))
            row['phase'] = 'installing'
            _save(root, state)
            shutil.move(str(stage / row['path']), str(dest))
            row['phase'] = 'installed'
            _save(root, state)
        state['status'] = 'committed'
        _save(root, state)
    except Exception:
        _restore(root, state)
        raise
    return [str(_destination(root, row['path'])) for row in rows]


def rollback(target: str) -> int:
    """把所有 .old 备份还原（更新失败/新版本异常时调用）。"""
    root = Path(target).resolve()
    journal = root / JOURNAL
    if journal.exists():
        return _restore(root, json.loads(journal.read_text(encoding='utf-8')))
    n = 0
    for root, _, files in os.walk(target):
        for name in files:
            if name.endswith(".old"):
                os.replace(os.path.join(root, name),
                           os.path.join(root, name[:-4]))
                n += 1
    return n


def clean_backups(target: str) -> int:
    """确认新版本稳定后清理 .old。"""
    root = Path(target).resolve()
    journal = root / JOURNAL
    if journal.exists():
        state = json.loads(journal.read_text(encoding='utf-8'))
        if state['status'] != 'committed':
            raise RuntimeError('未提交事务必须先恢复，不可清理备份')
        count = 0
        for row in state['files']:
            dest = _destination(root, row['path'])
            backup = dest.with_name(dest.name + '.old')
            if row['existed'] and backup.exists():
                backup.unlink()
                count += 1
        journal.unlink()
        return count
    n = 0
    for root, _, files in os.walk(target):
        for name in files:
            if name.endswith(".old"):
                os.remove(os.path.join(root, name))
                n += 1
    return n
