"""Browse existing user-started local chats on the shared desktop backend."""
import asyncio
from pathlib import Path
import unicodedata

from chat_binding import BINDING_FILE, BindingError, bind_target
from codex_push import CodexPush

ROOT = Path(__file__).resolve().parents[1]
MENU_LIMIT = 12
MENU_BINDING = BINDING_FILE.with_name("device-chat.json")


def device_text(value):
    # The device font has a small fixed Chinese glyph set.
    from Foundation import NSString, NSStringTransformToLatin

    latin = NSString.stringWithString_(value).stringByApplyingTransform_reverse_(
        NSStringTransformToLatin, False)
    ascii_text = unicodedata.normalize('NFKD', str(latin or value)).encode('ascii', 'ignore').decode()
    return ' '.join(ascii_text.split())[:80] or 'Codex chat'


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


class ChatMenu:
    def __init__(self):
        self.entries = []

    async def refresh(self):
        self.entries = []
        client = CodexPush('', ROOT)
        try:
            await client.connect(verify=False)
            loaded = await client.request('thread/loaded/list', {'limit': 48})
            for start in range(0, len(loaded['data']), 4):
                batch = loaded['data'][start:start + 4]
                chats = await asyncio.gather(
                    *(read_user_chat(client, thread_id) for thread_id in batch),
                    return_exceptions=True)
                for chat in chats:
                    if isinstance(chat, Exception):
                        continue
                    project = device_text(Path(chat['cwd']).name)[:18]
                    title = device_text(chat.get('name') or chat.get('preview') or 'Codex chat')[:47]
                    self.entries.append((chat['id'], f'{project} / {title}'))
                    if len(self.entries) == MENU_LIMIT:
                        return
        finally:
            await client.close()

    def entry(self, index):
        if not 0 <= index < len(self.entries):
            return b'empty'
        label = self.entries[index][1]
        return f'{index + 1}/{len(self.entries)}\n{label}'.encode('ascii')

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
