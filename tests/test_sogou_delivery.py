"""Check what a physical authenticated END is allowed to submit."""
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from chat_menu import ChatMenu, text_bitmap
from wifi_microphone import MicrophoneBridge
from wifi_protocol import AUDIO, BIND, CANCEL, END, Frames, RATE, READY, RESULT, START, receive, session_keys


class Sink:
    def begin(self):
        return 1
    def feed(self, payload):
        pass
    def finish(self, generation):
        pass
    def cancel(self):
        pass


class Session:
    def __init__(self, ack=True):
        self.sent = []
        self.closed = asyncio.Event()
        self.ack = ack

    async def start(self):
        pass
    async def finalize(self):
        return '测试本次听写'
    async def send(self, text):
        self.sent.append(text)
        if not self.ack:
            raise ConnectionError('ack lost')
    async def close(self):
        self.closed.set()


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, end, ack=True):
        key = b'x' * 32
        session = Session(ack)
        bridge = MicrophoneBridge(key, Sink(), chat_factory=lambda: session)
        server = await asyncio.start_server(bridge.handle, '127.0.0.1', 0)
        async with server:
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
            challenge = await reader.readexactly(20)
            nonce = b'y' * 16
            writer.write(nonce + hmac.digest(key, challenge + nonce, 'sha256'))
            up, down = [Frames(k) for k in session_keys(key, challenge[4:], nonce)]
            self.assertEqual((await receive(reader, down))[0], READY)
            writer.write(up.encode(START, b'{"rate":16000,"bits":16,"channels":1}'))
            pcm = b'\x01\0' * 320
            writer.write(up.encode(AUDIO, pcm))
            if end == 'disconnect':
                writer.close()
            elif end == 'cancel':
                writer.write(up.encode(CANCEL, b''))
                self.assertEqual(await receive(reader, down), (RESULT, b'cancelled'))
            else:
                sha = hashlib.sha256(pcm).hexdigest()
                if end == 'bad-digest':
                    sha = '0' * 64
                writer.write(up.encode(END, json.dumps(dict(bytes=len(pcm), sha256=sha)).encode()))
                if end == 'bad-digest' or not ack:
                    self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b'')
                else:
                    self.assertEqual(await receive(reader, down), (RESULT, b'sent'))
            await asyncio.wait_for(session.closed.wait(), 1)
            writer.close()
            await writer.wait_closed()
            return session.sent

    async def test_physical_end_submits_exactly_once(self):
        self.assertEqual(await self.capture('end'), ['测试本次听写'])

    async def test_cancel_disconnect_or_damaged_audio_never_submit(self):
        for end in ('cancel', 'disconnect', 'bad-digest'):
            with self.subTest(end=end):
                self.assertEqual(await self.capture(end), [])

    async def test_lost_ack_is_not_retried(self):
        self.assertEqual(await self.capture('end', ack=False), ['测试本次听写'])


    async def test_menu_selects_two_destinations(self):
        key = b'x' * 32
        sessions = [Session(), Session()]
        selected = None

        class Menu(ChatMenu):
            async def refresh(self):
                self.entries = [('a', '数学课堂', '整理练习'), ('b', '编程课堂', '检查作业')]

            def entry(self, index, bitmap=False, *, include_project=False):
                if bitmap:
                    return super().entry(index, bitmap, include_project=include_project)
                return (b'1/2\nProject A', b'2/2\nProject B')[index]

            async def select(self, index):
                nonlocal selected
                selected = sessions[index]

        bridge = MicrophoneBridge(key, Sink(), chat_factory=lambda: selected, menu_factory=Menu)
        server = await asyncio.start_server(bridge.handle, '127.0.0.1', 0)
        async with server:
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
            try:
                challenge = await reader.readexactly(20)
                nonce = b'y' * 16
                writer.write(nonce + hmac.digest(key, challenge + nonce, 'sha256'))
                up, down = [Frames(k) for k in session_keys(key, challenge[4:], nonce)]
                self.assertEqual(await receive(reader, down, 2), (READY, b'virtual-mic-menu'))
                for request, index, project in ((b'open3', 0, '数学课堂'),
                                                (bytes([0, 1]), 1, '编程课堂')):
                    writer.write(up.encode(BIND, request))
                    first, second, third = [await receive(reader, down, 2) for _ in range(3)]
                    self.assertEqual([frame[0] for frame in (first, second, third)], [RESULT] * 3)
                    self.assertEqual(first[1][:3], b'P' + bytes([index, 2]))
                    self.assertEqual((len(first[1]), len(second[1])), (903, 900))
                    self.assertEqual(third[1], text_bitmap(f'项目：{project}', size=16, row_height=20, rows=1))
                    self.assertTrue(any(third[1]))
                pcm = b'\x01\0' * 320
                for index in range(2):
                    writer.write(up.encode(BIND, b'open'))
                    self.assertEqual(await receive(reader, down, 2), (RESULT, b'1/2\nProject A'))
                    writer.write(up.encode(BIND, bytes([0, index])))
                    self.assertEqual(await receive(reader, down, 2), (RESULT, Menu().entry(index)))
                    writer.write(up.encode(BIND, bytes([1, index])))
                    self.assertEqual(await receive(reader, down, 2), (RESULT, b'bound'))
                    writer.write(up.encode(START, b'{"rate":16000,"bits":16,"channels":1,"selected":true}'))
                    writer.write(up.encode(AUDIO, pcm))
                    end = {'bytes': len(pcm), 'sha256': hashlib.sha256(pcm).hexdigest()}
                    writer.write(up.encode(END, json.dumps(end).encode()))
                    self.assertEqual(await receive(reader, down, 2), (RESULT, b'sent'))
                    self.assertEqual([len(session.sent) for session in sessions], [1, index])
                    self.assertFalse(selected.use_clipboard)
            finally:
                writer.close()
                await writer.wait_closed()
