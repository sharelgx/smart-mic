"""Resolve a deliberately copied Codex deep link at the start of an utterance."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Event

THREAD_ID = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)
LOCAL_LINK = re.compile(
    rf'codex://threads/({THREAD_ID.pattern})(?:\?hostId=local(?:&threadAccess=shared)?)?'
)
BINDING_FILE = Path(__file__).resolve().parents[1] / 'work/private/voice-chat.json'


class BindingError(RuntimeError):
    """A fixed, user-readable explanation without pasteboard or server content."""


def copied_thread_id(board=None):
    """Read a URL candidate only; let AppKit classify plain text before reading."""
    import AppKit as A
    import objc

    with objc.autorelease_pool():
        if board is None:
            board = A.NSPasteboard.generalPasteboard()
        version = board.changeCount()
        types = set(board.types() or ())
        if A.NSPasteboardTypeURL in types:
            flavor = A.NSPasteboardTypeURL
        elif A.NSPasteboardTypeString in types:
            flavor = A.NSPasteboardTypeString
            done = Event()
            state = {}
            pattern = A.NSPasteboardDetectionPatternProbableWebURL

            def classified(matches, error):
                state['ok'] = error is None
                state['url'] = pattern in (matches or ())
                done.set()

            board.detectPatternsForPatterns_completionHandler_({pattern}, classified)
            if not done.wait(2) or not state.get('ok'):
                raise BindingError('无法检查聊天链接，请在 Codex 重新按 ⌘⌥L')
            if board.changeCount() != version:
                raise BindingError('剪贴板正在变化，请再试一次')
            if not state['url']:
                return None
        else:
            return None

        value = board.stringForType_(flavor)
        if board.changeCount() != version or value is None:
            raise BindingError('未能读取同一条链接，请重新复制聊天链接')
        value = str(value).strip()
        found = LOCAL_LINK.fullmatch(value)
        if found:
            return found[1].lower()
        if value.lower().startswith('codex://'):
            raise BindingError('请在一个已打开的本地聊天里按 ⌘⌥L')
        return None


@contextmanager
def _binding_lock(path):
    # The JSON file is replaced on update. Lock its stable private directory.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _version(stat):
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns


def _valid_id(value):
    return isinstance(value, str) and bool(THREAD_ID.fullmatch(value))


def _read_binding(path):
    try:
        with path.open() as stream:
            data = json.load(stream)
            version = _version(os.fstat(stream.fileno()))
    except FileNotFoundError:
        return {}, None
    except (ValueError, UnicodeError):
        raise BindingError('无法读取本机聊天绑定') from None
    if isinstance(data, dict):
        if set(data) == {'switch_from'} and (data['switch_from'] is None or _valid_id(data['switch_from'])):
            return data, version
        if _valid_id(data.get('id')) and isinstance(data.get('cwd'), str) and Path(data['cwd']).is_absolute():
            return data, version
    raise BindingError('本机聊天绑定格式无效')


def save_binding(path, binding):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.voice-chat-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(binding, stream)
        os.replace(name, path)
        return _version(path.stat())
    finally:
        Path(name).unlink(missing_ok=True)


def arm_chat_switch(previous_id=None, path=BINDING_FILE):
    """Stop using the old recipient until a different chat has been verified."""
    if previous_id is not None and not _valid_id(previous_id):
        raise BindingError('原聊天 ID 无效')
    with _binding_lock(path):
        state, _ = _read_binding(path)
        old_id = state.get('id') or state.get('switch_from')
        if previous_id and old_id and old_id != previous_id:
            raise BindingError('绑定目标已变化，请核对后再切换')
        save_binding(path, {'switch_from': old_id or previous_id})


def invalidate_binding(path=BINDING_FILE):
    """Require an explicit fresh candidate, preserving a switch already in progress."""
    with _binding_lock(path):
        state, _ = _read_binding(path)
        save_binding(path, {'switch_from': state.get('switch_from')})


def verify_bound_target(client, path=BINDING_FILE):
    """Reject an in-flight utterance if a switch invalidated its destination."""
    with _binding_lock(path):
        state, version = _read_binding(path)
        if (version is None or version != getattr(client, 'binding_version', None)
                or state != {'id': client.thread, 'cwd': str(client.cwd)}):
            raise BindingError('听写期间聊天绑定已变化，请重新开始听写')


async def bind_target(client, candidate, path=BINDING_FILE):
    with _binding_lock(path):
        previous, version = _read_binding(path)
        old_id = previous.get('id')
        if old_id and candidate is not None and old_id != candidate:
            # Record the switch before RPC so a failed lookup cannot resurrect old_id.
            previous = {'switch_from': old_id}
            version = save_binding(path, previous)
        if previous.get('switch_from') and (not candidate or candidate == previous['switch_from']):
            raise BindingError('正在切换聊天，请在另一个目标聊天复制深度链接')
        thread_id = candidate if candidate is not None else old_id
    if not _valid_id(thread_id):
        raise BindingError('请先在目标聊天按 ⌘⌥L，再按设备第三键')
    await client.connect(verify=False)
    result = await client.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
    thread = result.get('thread', {})
    cwd = thread.get('cwd')
    if (thread.get('id') != thread_id or not cwd or not Path(cwd).is_absolute()
            or thread.get('status', {}).get('type') not in ('idle', 'active')):
        raise BindingError('请打开目标聊天，再复制聊天链接')
    binding = {'id': thread_id, 'cwd': str(Path(cwd).resolve())}
    if not candidate and previous != binding:
        raise BindingError('目标项目已变化，请重新复制聊天链接')
    with _binding_lock(path):
        _, current = _read_binding(path)
        if version != current:
            raise BindingError('核对时聊天绑定已变化，请重新开始听写')
        if binding != previous:
            version = save_binding(path, binding)
        journal = thread.get('path')
        if isinstance(journal, str) and Path(journal).is_absolute() and Path(journal).suffix == '.jsonl':
            client.reply_journal = Path(journal)
        client.thread = thread_id
        client.cwd = Path(binding['cwd'])
        client.binding_version = version
    title = thread.get('name') or thread.get('preview', '') or thread_id
    return {**binding, 'title': ' '.join(title.split())[:60]}
