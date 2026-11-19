"""Browse existing user-started local chats on the shared desktop backend."""
import asyncio
import json
import os
from pathlib import Path
import unicodedata

from chat_binding import BINDING_FILE, BindingError, _binding_lock, bind_target, save_binding
from codex_push import CodexPush

ROOT = Path(__file__).resolve().parents[1]
MENU_LIMIT = 5
MENU_BINDING = BINDING_FILE.with_name("device-chat.json")
HIDDEN_CHATS = BINDING_FILE.with_name("hidden-device-chats.json")
CODEX_HOME = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()


def device_text(value):
    # The device font has a small fixed Chinese glyph set.
    from Foundation import NSString, NSStringTransformToLatin

    latin = NSString.stringWithString_(value).stringByApplyingTransform_reverse_(
        NSStringTransformToLatin, False)
    ascii_text = unicodedata.normalize('NFKD', str(latin or value)).encode('ascii', 'ignore').decode()
    return ' '.join(ascii_text.split())[:80] or 'Codex chat'


def text_bitmap(value, *, size, row_height, rows):
    from PIL import Image, ImageDraw, ImageFont

    width, height = 200, row_height * rows
    font = ImageFont.truetype('/System/Library/Fonts/Hiragino Sans GB.ttc', size)
    image = Image.new('1', (width, height))
    draw = ImageDraw.Draw(image)
    remaining = ' '.join(value.split())
    for row in range(rows):
        end = 1
        while end < len(remaining) and font.getlength(remaining[:end + 1]) <= width:
            end += 1
        text = remaining[:end]
        remaining = remaining[end:].lstrip()
        if row == rows - 1 and remaining:
            while text and font.getlength(text + '…') > width:
                text = text[:-1]
            text += '…'
        draw.text((0, row * row_height), text, font=font, fill=1, anchor='lt')
        if not remaining:
            break
    return image.tobytes()


async def read_user_chat(client, thread_id):
    result = await client.request('thread/read', {'threadId': thread_id, 'includeTurns': True})
    thread = result['thread']
    turns = thread.get('turns') or []
    if (thread.get('id') != thread_id
            or thread.get('source') not in ('vscode', 'appServer')
            or thread.get('status', {}).get('type') not in ('idle', 'active')
            or not turns or not any(item.get('type') == 'userMessage'
                                   for item in turns[0].get('items', []))):
        raise BindingError('请先在 Codex 中发起这个聊天')
    return thread


def hidden_chats():
    try:
        data = json.loads(HIDDEN_CHATS.read_text())
    except FileNotFoundError:
        return set()
    return set(data['ids'])


def sidebar_state():
    try:
        state = json.loads((CODEX_HOME / '.codex-global-state.json').read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def sidebar_project(chat, projects, state):
    thread_id = chat['id']
    assignment = state.get('thread-project-assignments', {}).get(thread_id) or {}
    legacy_id = assignment.get('projectId') if assignment.get('projectKind') == 'local' else None
    project_id = chat.get('projectId')
    if project_id in projects:
        return projects[project_id]['name']
    if thread_id in state.get('projectless-thread-ids', []):
        return '无项目'
    if legacy_id:
        local = state.get('local-projects', {}).get(legacy_id)
        ids_by_host = state.get('app-server-project-id-by-legacy-project-id-by-host', {})
        migrated_id = ids_by_host.get(f'local:{CODEX_HOME}', {}).get(legacy_id, legacy_id)
        if migrated_id in projects:
            return projects[migrated_id]['name']
        if local and local.get('name'):
            return local['name']
    cwd = chat.get('cwd')
    if cwd:
        path = Path(cwd)
        matches = [(len(root.parts), project['name'])
                   for project in projects.values()
                   for item in project.get('roots', [])
                   if item.get('path')
                   for root in [Path(item['path'])]
                   if path.is_relative_to(root)]
        if matches:
            depth = max(length for length, _ in matches)
            names = {name for length, name in matches if length == depth}
            if len(names) == 1:
                return names.pop()
    return '无项目'


class ChatMenu:
    def __init__(self):
        self.entries = []

    async def refresh(self):
        self.entries = []
        client = CodexPush('', ROOT)
        try:
            await client.connect(verify=False, experimental=True)
            state = sidebar_state()
            projects = {}
            cursor = None
            try:
                while True:
                    page = await client.request('project/list', {'limit': 100, 'cursor': cursor})
                    projects.update((project['id'], project) for project in page['data'])
                    cursor = page.get('nextCursor')
                    if not cursor:
                        break
            except RuntimeError:
                # Older shared backends can still use desktop sidebar assignments.
                pass
            hidden = hidden_chats()
            eligible = []
            cursor = None
            while True:
                loaded = await client.request('thread/loaded/list', {'limit': 48, 'cursor': cursor})
                for start in range(0, len(loaded['data']), 4):
                    batch = loaded['data'][start:start + 4]
                    chats = await asyncio.gather(
                        *(read_user_chat(client, thread_id) for thread_id in batch),
                        return_exceptions=True)
                    eligible.extend(chat for chat in chats
                                    if not isinstance(chat, Exception) and chat['id'] not in hidden)
                cursor = loaded.get('nextCursor')
                if not cursor:
                    break
            eligible.sort(
                key=lambda chat: (chat.get('recencyAt') or chat.get('updatedAt') or chat['createdAt'],
                                  chat['createdAt'], chat['id']),
                reverse=True,
            )
            for chat in eligible[:MENU_LIMIT]:
                project = sidebar_project(chat, projects, state)
                title = chat.get('name') or chat.get('preview') or '未命名聊天'
                self.entries.append((chat['id'], project, title))
        finally:
            await client.close()

    def entry(self, index, bitmap=False, *, include_project=False):
        if not 0 <= index < len(self.entries):
            return b'empty'
        _, project, title = self.entries[index]
        if bitmap:
            header = b'P' if include_project else b'Q'
            title_pixels = text_bitmap(title, size=32, row_height=36, rows=2)
            project_pixels = (text_bitmap(f'项目：{project}', size=16, row_height=20, rows=1)
                              if include_project else b'')
            return header + bytes([index, len(self.entries)]) + title_pixels + project_pixels
        label = f'{device_text(project)[:18]} / {device_text(title)[:47]}'
        return f'{index + 1}/{len(self.entries)}\n{label}'.encode('ascii')

    async def hide(self, index):
        if not 0 <= index < len(self.entries):
            raise BindingError('请重新打开聊天列表')
        thread_id = self.entries[index][0]
        with _binding_lock(HIDDEN_CHATS):
            hidden = hidden_chats()
            hidden.add(thread_id)
            save_binding(HIDDEN_CHATS, {'ids': sorted(hidden)})
        self.entries.pop(index)
        remaining = self.entries.copy()
        try:
            await self.refresh()
        except (RuntimeError, OSError):
            # Removal was already saved. Reopening the list will retry loading replacements.
            self.entries = remaining

    async def select(self, index):
        if not 0 <= index < len(self.entries):
            raise BindingError('请重新打开聊天列表')
        thread_id = self.entries[index][0]
        client = CodexPush('', ROOT)
        try:
            await client.connect(verify=False)
            await read_user_chat(client, thread_id)
        finally:
            await client.close()
        client = CodexPush('', ROOT)
        try:
            await bind_target(client, thread_id, path=MENU_BINDING)
        finally:
            await client.close()
