"""Speak only the final reply to a turn acknowledged for this device."""
import asyncio
from collections import OrderedDict
from contextlib import closing, contextmanager
import os
from pathlib import Path
import re
import select
import tempfile
import wave

from wifi_protocol import RATE, SPEECH, SPEECH_END

REPLY_TIMEOUT = 600


@contextmanager
def watch_journal(path, changed):
    """Use macOS file-write notifications. Reply content still comes from RPC."""
    if not path or not hasattr(select, 'kqueue'):
        yield
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        yield
        return
    loop = asyncio.get_running_loop()
    try:
        with closing(select.kqueue()) as queue:
            queue.control([select.kevent(
                fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND,
            )], 0, 0)

            def on_write():
                if queue.control(None, 1, 0):
                    changed()

            loop.add_reader(queue.fileno(), on_write)
            try:
                yield
            finally:
                loop.remove_reader(queue.fileno())
    finally:
        os.close(fd)


class TurnReplies:
    def __init__(self, client):
        self.client = client
        self.turns = OrderedDict()
        self.changed = asyncio.Event()
        self.check_snapshot = True

    def observe(self, method, params):
        if params.get('threadId') != self.client.thread:
            return
        if method == 'item/completed':
            item = params.get('item', {})
            if (item.get('type'), item.get('phase')) != ('agentMessage', 'final_answer'):
                return
            turn_id = params.get('turnId')
            body = item.get('text')
            if isinstance(body, str) and body.strip():
                self.save(turn_id, body[:12000], None)
        elif method == 'turn/completed':
            turn = params.get('turn', {})
            turn_id = turn.get('id')
            reply, _ = self.turns.get(turn_id, (None, None))
            if not reply:
                reply = self.final_text(turn)
            self.save(turn_id, reply, turn.get('status') or 'unknown')
        elif method == 'thread/status/changed' and params.get('status', {}).get('type') == 'idle':
            # Separate Unix clients may receive only the broadcast status change.
            self.check_snapshot = True
            self.changed.set()

    @staticmethod
    def final_text(turn):
        for item in reversed(turn.get('items', [])):
            body = item.get('text')
            if (item.get('type') == 'agentMessage' and item.get('phase') == 'final_answer'
                    and isinstance(body, str) and body.strip()):
                return body[:12000]
        return None

    def save(self, turn_id, reply, status):
        if not turn_id:
            return
        self.turns[turn_id] = (reply, status)
        self.turns.move_to_end(turn_id)
        if len(self.turns) > 8:
            self.turns.popitem(last=False)
        self.changed.set()

    def journal_changed(self):
        self.check_snapshot = True
        self.changed.set()

    async def wait(self, turn_id):
        journal = getattr(self.client, 'reply_journal', None)
        with watch_journal(journal, self.journal_changed):
            async with asyncio.timeout(REPLY_TIMEOUT):
                return await self.wait_for_reply(turn_id)

    async def wait_for_reply(self, turn_id):
        while True:
            reply, status = self.turns.get(turn_id, (None, None))
            if status and (status != 'completed' or reply):
                return reply if status == 'completed' else None
            if self.client.reader.done():
                return None
            if self.check_snapshot or status == 'completed':
                self.check_snapshot = False
                result = await self.client.request('thread/turns/list', {
                    'threadId': self.client.thread, 'limit': 4,
                    'sortDirection': 'desc', 'itemsView': 'summary',
                })
                turn = next((turn for turn in result.get('data', []) if turn.get('id') == turn_id), {})
                state = turn.get('status')
                if state == 'completed' or status == 'completed':
                    reply = self.final_text(turn)
                    if reply:
                        return reply
                    result = await self.client.request('thread/items/list', {
                        'threadId': self.client.thread, 'turnId': turn_id,
                        'limit': 16, 'sortDirection': 'desc',
                        'deferOutput': {'commandExecution': True, 'mcpToolCall': True},
                    })
                    items = [entry['item'] for entry in reversed(result.get('data', []))]
                    return self.final_text({'items': items})
                if state and state != 'inProgress':
                    return None
                # A notification can arrive while the read is in flight.
                if self.changed.is_set():
                    self.changed.clear()
                    continue
            self.changed.clear()
            changed = asyncio.create_task(self.changed.wait())
            try:
                await asyncio.wait((changed, self.client.reader), return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)


def spoken_text(text):
    text = re.sub(r'^\s*`{3,}[^\n]*\n.*?(?:^\s*`{3,}[^\n]*$|\Z)', '（代码见聊天）', text, flags=re.M | re.S)
    text = re.sub(r'!?\[([^\]\n]+)\]\([^\n)]*\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'^\s{0,3}(?:#{1,6}|>|[-*])\s+', '', text, flags=re.M)
    text = ' '.join(text.replace('**', '').replace('`', '').split())
    return text if len(text) <= 1200 else text[:1200] + '。剩余内容请查看聊天。'


async def stream_reply(session, reader, writer, frames):
    async def send_audio():
        text = await session.reply_text()
        if not text or not (text := spoken_text(text)):
            print('本轮没有可朗读的最终回复。', flush=True)
            return
        with tempfile.TemporaryDirectory(prefix='passport-voice-') as directory:
            path = Path(directory) / 'reply.wav'
            source = path.with_suffix('.txt')
            source.write_text(text, encoding='utf-8')
            process = await asyncio.create_subprocess_exec(
                '/usr/bin/say', '-v', 'Tingting', '-o', str(path), '--file-format=WAVE',
                '--data-format=LEI16@16000', '-f', str(source),
                stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(process.wait(), 45)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if process.returncode:
                raise RuntimeError('语音合成失败')
            with wave.open(str(path)) as audio:
                if (audio.getframerate(), audio.getsampwidth(), audio.getnchannels()) != (RATE, 2, 1):
                    raise ValueError('语音格式错误')
                loop = asyncio.get_running_loop()
                deadline = loop.time()
                print(f'设备开始接收朗读音频（{audio.getnframes() / RATE:.1f} 秒）。', flush=True)
                while pcm := audio.readframes(512):
                    session.verify_reply_target()
                    writer.write(frames.encode(SPEECH, pcm))
                    await asyncio.wait_for(writer.drain(), 5)
                    deadline = max(deadline, loop.time() - 0.10) + len(pcm) / (RATE * 2)
                    await asyncio.sleep(max(0, deadline - loop.time()))
                print('朗读音频已传输。', flush=True)

    playback = asyncio.create_task(send_audio())
    peer = asyncio.create_task(reader.read(1))
    try:
        await asyncio.wait((playback, peer), return_when=asyncio.FIRST_COMPLETED)
        if peer.done():
            raise ConnectionError('设备取消朗读或已断开')
        try:
            playback.result()
        except ConnectionError:
            raise
        except Exception as error:
            print(f'朗读已结束（{type(error).__name__}）。', flush=True)
        peer.cancel()
        await asyncio.gather(peer, return_exceptions=True)
        writer.write(frames.encode(SPEECH_END))
        await writer.drain()
    finally:
        for task in (playback, peer):
            task.cancel()
        await asyncio.gather(playback, peer, return_exceptions=True)
