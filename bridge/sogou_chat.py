"""One physical utterance, one native dictation field, one existing Codex chat."""
import asyncio
import json
from pathlib import Path
import sys

from chat_binding import (BINDING_FILE, BindingError, invalidate_binding, bind_target,
                          copied_thread_id, verify_bound_target)
from codex_push import CodexPush
from speech_text import simplify
from wifi_protocol import RATE

ROOT = Path(__file__).resolve().parents[1]


class SogouChat:
    def __init__(self):
        self.client = CodexPush('', ROOT)
        self.use_clipboard = True
        self.binding_path = None
        self.pad = None
        self.voice_pids = None
        self.starting_voice = None
        self.stage = '核对当前聊天'

    async def event(self, name, timeout):
        raw = await asyncio.wait_for(self.pad.stdout.readline(), timeout)
        if not raw:
            raise RuntimeError('听写框已关闭')
        event = json.loads(raw)
        if event.get('event') != name:
            raise RuntimeError('听写框未就绪或未取得本次已上屏文字')
        return event

    async def start(self):
        from sogou_voice import start_session_voice

        try:
            candidate = await asyncio.to_thread(copied_thread_id) if self.use_clipboard else None
        except BindingError as error:
            # An unreadable or unsupported new destination must not fall back to the old one.
            invalidate_binding(path=self.binding_path or BINDING_FILE)
            print(str(error), flush=True)
            raise
        try:
            if self.use_clipboard:
                target = await bind_target(self.client, candidate)
            else:
                target = await bind_target(self.client, candidate, path=self.binding_path)
        except BindingError as error:
            print(str(error), flush=True)
            raise
        print('本次听写使用复制的聊天链接。' if candidate else '本次听写沿用已保存的聊天绑定。', flush=True)
        self.stage = '打开本次听写框'
        self.pad = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / 'bridge/sogou_pad.py'),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self.pad.stdin.write((json.dumps({'target': target}, ensure_ascii=False)+'\n').encode())
        await self.pad.stdin.drain()
        await self.event('ready', 8)
        self.stage = '启动搜狗并确认使用 BlackHole 2ch'
        self.starting_voice = asyncio.create_task(start_session_voice())
        self.voice_pids = await asyncio.shield(self.starting_voice)
        self.starting_voice = None
        self.stage = '实时听写'

    async def finalize(self):
        from sogou_voice import finish_voice

        pids, self.voice_pids = self.voice_pids, None
        self.stage = '结束搜狗收音'
        await finish_voice(pids)
        self.stage = '等待本次已上屏文字'
        self.pad.stdin.write(b'{"cmd":"finish"}\n')
        await self.pad.stdin.drain()
        response = await self.event('final', 12)
        text = response.get('text')
        if not isinstance(text, str) or len(text) > 8000:
            raise RuntimeError('本次听写结果无效')
        return simplify(text)

    async def send(self, text):
        self.stage = '等待当前聊天的发送回执'
        return await self.client.push(
            text, before_submit=lambda: verify_bound_target(self.client, path=self.binding_path or BINDING_FILE))

    async def close(self):
        try:
            if self.starting_voice:
                outcome, = await asyncio.gather(self.starting_voice, return_exceptions=True)
                if not isinstance(outcome, BaseException):
                    self.voice_pids = outcome
                self.starting_voice = None
            if self.voice_pids:
                from sogou_voice import finish_voice
                pids, self.voice_pids = self.voice_pids, None
                await finish_voice(pids)
        finally:
            if self.pad and self.pad.returncode is None:
                self.pad.stdin.close()
                try:
                    await asyncio.wait_for(self.pad.wait(), 1)
                except asyncio.TimeoutError:
                    self.pad.kill()
                    await self.pad.wait()
            await self.client.close()


class LiveDictation:
    """Read Wi-Fi while Sogou starts. Play the buffered onset at microphone speed."""
    def __init__(self, sink, session):
        self.sink = sink
        self.session = session
        self.queue = asyncio.Queue()
        self.buffered = 0
        self.generation = None
        self.task = asyncio.create_task(self.play())

    async def play(self):
        await self.session.start()
        self.generation = self.sink.begin()
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            pcm = await self.queue.get()
            if pcm is None:
                break
            self.buffered -= len(pcm)
            await asyncio.sleep(max(0, deadline - loop.time()))
            self.sink.feed(pcm)
            deadline = max(deadline, loop.time() - 0.04) + len(pcm) / (2 * RATE)
        await asyncio.to_thread(self.sink.finish, self.generation)

    def feed(self, pcm):
        if self.task.done():
            self.task.result()
            raise RuntimeError('听写已结束')
        self.buffered += len(pcm)
        if self.buffered > 2 * RATE * 25:
            raise RuntimeError('搜狗启动超时，本段收音已取消')
        self.queue.put_nowait(pcm)

    async def finish(self, still_connected):
        self.queue.put_nowait(None)
        await self.task
        text = await self.session.finalize()
        if not text:
            return b'silent'
        if not still_connected():
            raise RuntimeError('设备已断开')
        await self.session.send(text)
        return b'sent'

    async def close(self):
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        if self.generation is not None:
            self.sink.cancel()
        await self.session.close()
