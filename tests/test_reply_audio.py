import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import select
import tempfile
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from reply_audio import TurnReplies, spoken_text
from test_sogou_delivery import Session, Sink
from wifi_microphone import MicrophoneBridge
from wifi_protocol import (AUDIO, BIND, END, Frames, READY, RESULT, SPEECH, SPEECH_END,
                           START, receive, session_keys)


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_acknowledged_turn_final_answer(self):
        client = SimpleNamespace(thread='target', reader=asyncio.get_running_loop().create_future())
        replies = TurnReplies(client)
        def item(thread, turn, phase, text):
            replies.observe('item/completed', {
                'threadId': thread, 'turnId': turn,
                'item': {'type': 'agentMessage', 'phase': phase, 'text': text},
            })
        def complete(turn, status):
            replies.observe('turn/completed', {
                'threadId': 'target', 'turn': {'id': turn, 'status': status},
            })
        item('target', 'wanted', 'final_answer', '这是最终回复')
        item('target', 'wanted', 'commentary', '进度')
        item('other', 'wanted', 'final_answer', '其他聊天')
        item('target', 'unrelated', 'final_answer', '其他轮次')
        complete('unrelated', 'completed')
        complete('wanted', 'completed')
        self.assertEqual(await replies.wait('wanted'), '这是最终回复')
        item('target', 'retry', 'final_answer', '未完成')
        complete('retry', 'interrupted')
        self.assertIsNone(await replies.wait('retry'))
        client.reader.set_result(None)
        self.assertIsNone(await replies.wait('missing'))
        self.assertNotIn('print', spoken_text('说明\n```python\nprint(1)\n```\n结束'))

    async def test_broadcast_idle_reads_matching_turn_once(self):
        reader = asyncio.get_running_loop().create_future()
        active_read = asyncio.Event()
        calls = []

        async def request(method, params):
            calls.append((method, params))
            active_read.set()
            item = {'type': 'agentMessage', 'phase': 'final_answer', 'text': '正确轮次'}
            return {'data': [
                {'id': 'newer', 'status': 'completed', 'items': [{**item, 'text': '错误轮次'}]},
                {'id': 'wanted', 'status': 'completed' if len(calls) > 1 else 'inProgress',
                 'items': [item]},
            ]}

        replies = TurnReplies(SimpleNamespace(thread='target', reader=reader, request=request))
        result = asyncio.create_task(replies.wait('wanted'))
        try:
            await asyncio.wait_for(active_read.wait(), 1)
            status = {'status': {'type': 'idle'}}
            replies.observe('thread/status/changed', {**status, 'threadId': 'other'})
            self.assertFalse(result.done())
            replies.observe('thread/status/changed', {**status, 'threadId': 'target'})
            self.assertEqual(await asyncio.wait_for(result, 1), '正确轮次')
            self.assertEqual([method for method, _ in calls], ['thread/turns/list'] * 2)
        finally:
            result.cancel()
            reader.cancel()
            await asyncio.gather(result, return_exceptions=True)

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'macOS vnode notification required')
    async def test_journal_write_wakes_wait_without_rpc_notifications(self):
        loop = asyncio.get_running_loop()
        disconnected = loop.create_future()
        first_check = asyncio.Event()
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'chat.jsonl'
            journal.touch()

            async def request(method, params):
                first_check.set()
                item = {'type': 'agentMessage', 'phase': 'final_answer', 'text': '回复已完成'}
                return {'data': [{'id': 'wanted', 'status': 'completed' if journal.stat().st_size else 'inProgress',
                                  'items': [item]}]}

            client = SimpleNamespace(thread='target', reader=disconnected, request=request, reply_journal=journal)
            replies = TurnReplies(client)
            task = asyncio.create_task(replies.wait('wanted'))
            try:
                await asyncio.wait_for(first_check.wait(), 2)
                journal.write_text('{}\n')
                self.assertEqual(await asyncio.wait_for(task, 2), '回复已完成')
            finally:
                task.cancel()
                disconnected.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @unittest.skipUnless(Path('/usr/bin/say').exists(), 'macOS speech synthesis required')
    async def test_real_chinese_tts_over_paired_link_and_next_recording(self):
        class SpeakingSession(Session):
            speaking = False
            def enable_reply_audio(self):
                self.speaking = True
            async def reply_text(self):
                assert self.speaking and self.sent
                return '你好。'
            def verify_reply_target(self):
                pass

        key = b'x' * 32
        bridge = MicrophoneBridge(key, Sink(), chat_factory=SpeakingSession, menu_factory=object)
        server = await asyncio.start_server(bridge.handle, '127.0.0.1', 0)
        async with server:
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
            try:
                challenge = await reader.readexactly(20)
                nonce = b'y' * 16
                writer.write(nonce + hmac.digest(key, challenge + nonce, 'sha256'))
                up, down = [Frames(k) for k in session_keys(key, challenge[4:], nonce)]
                self.assertEqual(await receive(reader, down), (READY, b'virtual-mic-menu'))
                writer.write(up.encode(BIND, b'reply-audio'))
                self.assertEqual(await receive(reader, down), (RESULT, b'yes'))

                async def send_recording(speech):
                    fmt = dict(rate=16000, bits=16, channels=1)
                    if speech:
                        fmt['replyAudio'] = True
                    pcm = b'\x01\0' * 320
                    end = dict(bytes=len(pcm), sha256=hashlib.sha256(pcm).hexdigest())
                    writer.write(up.encode(START, json.dumps(fmt).encode()))
                    writer.write(up.encode(AUDIO, pcm))
                    writer.write(up.encode(END, json.dumps(end).encode()))
                    await writer.drain()

                await send_recording(True)
                self.assertEqual(await receive(reader, down, 8), (RESULT, b'sent-voice'))
                audio = bytearray()
                async with asyncio.timeout(15):
                    while True:
                        kind, data = await receive(reader, down)
                        if kind == SPEECH_END:
                            self.assertEqual(data, b'')
                            break
                        self.assertEqual(kind, SPEECH)
                        self.assertTrue(0 < len(data) <= 1024 and len(data) % 2 == 0)
                        audio.extend(data)
                self.assertGreater(len(audio), 3200)
                self.assertTrue(any(audio))
                await send_recording(False)
                self.assertEqual(await receive(reader, down), (RESULT, b'sent'))
            finally:
                writer.close()
                await writer.wait_closed()
