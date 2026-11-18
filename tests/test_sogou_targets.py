"""Check the destination of one utterance without using a real clipboard or chat."""
from functools import partial
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from chat_binding import BindingError, bind_target, copied_thread_id
from sogou_chat import SogouChat


FIRST = '11111111-1111-4111-8111-111111111111'
SECOND = '22222222-2222-4222-8222-222222222222'


class TargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.binding = self.root / 'private' / 'voice-chat.json'
        self.threads = {
            thread_id: {'id': thread_id, 'cwd': str(self.root / name), 'name': name,
                        'status': {'type': 'idle'}}
            for thread_id, name in [(FIRST, 'project-a'), (SECOND, 'project-b')]
        }

    def session(self):
        session = SogouChat()
        session.client.connect = AsyncMock()
        session.client.request = AsyncMock(side_effect=self.rpc)
        return session

    def rpc(self, method, params):
        if method == 'thread/read':
            return {'thread': self.threads[params['threadId']]}
        if method == 'turn/start':
            return {'turn': {'id': 'test-turn'}}
        self.fail(f'Unexpected RPC: {method}')

    async def test_binding_survives_an_ordinary_copy_and_switches_across_projects(self):
        for candidate, expected in [(FIRST, FIRST), (None, FIRST), (SECOND, SECOND), (None, SECOND)]:
            with self.subTest(candidate=candidate, expected=expected):
                client = self.session().client
                target = await bind_target(client, candidate, self.binding)
                expected_cwd = self.threads[expected]['cwd']
                self.assertEqual((target['id'], client.thread, str(client.cwd)),
                                 (expected, expected, expected_cwd))
                self.assertEqual(json.loads(self.binding.read_text()),
                                 {'id': expected, 'cwd': expected_cwd})
                client.request.assert_awaited_once_with(
                    'thread/read', {'threadId': expected, 'includeTurns': False})

    async def test_failed_switch_cannot_fall_back_after_an_ordinary_copy(self):
        client = self.session().client
        await bind_target(client, FIRST, self.binding)
        self.threads[SECOND]['status'] = {'type': 'notLoaded'}
        with self.assertRaises(BindingError):
            await bind_target(client, SECOND, self.binding)
        client.request.reset_mock()
        with self.assertRaises(BindingError):
            await bind_target(client, None, self.binding)
        client.request.assert_not_awaited()

    def test_ordinary_pasteboard_text_is_classified_without_reading_its_contents(self):
        from AppKit import NSPasteboardTypeString
        board = MagicMock()
        board.types.return_value = [NSPasteboardTypeString]
        board.changeCount.return_value = 1
        board.detectPatternsForPatterns_completionHandler_.side_effect = (
            lambda patterns, callback: callback(set(), None))
        self.assertIsNone(copied_thread_id(board))
        board.stringForType_.assert_not_called()

    def test_a_codex_chat_link_is_recognized_as_text_or_a_typed_url(self):
        from AppKit import NSPasteboardTypeString, NSPasteboardTypeURL
        for flavor in (NSPasteboardTypeString, NSPasteboardTypeURL):
            with self.subTest(flavor=flavor):
                board = MagicMock()
                board.types.return_value = [flavor]
                board.changeCount.return_value = 1
                board.detectPatternsForPatterns_completionHandler_.side_effect = (
                    lambda patterns, callback: callback(patterns, None))
                link = f'codex://threads/{FIRST}'
                for suffix in ('', '?hostId=local', '?hostId=local&threadAccess=shared'):
                    board.stringForType_.return_value = link + suffix
                    self.assertEqual(copied_thread_id(board), FIRST)
                for invalid in ('codex://settings', link + '?hostId=remote',
                                link + '?hostId=local&hostId=remote'):
                    board.stringForType_.return_value = invalid
                    with self.assertRaises(BindingError):
                        copied_thread_id(board)

    async def test_start_and_send_use_bound_thread_without_a_picker(self):
        session = self.session()
        pad = MagicMock()
        pad.stdin.drain = AsyncMock()
        pad.stdout.readline = AsyncMock(return_value=b'{"event":"ready"}\n')
        with patch('sogou_chat.BINDING_FILE', self.binding), \
             patch('sogou_chat.bind_target', partial(bind_target, path=self.binding)), \
             patch('sogou_chat.copied_thread_id', return_value=SECOND) as copy, \
             patch('asyncio.create_subprocess_exec', new=AsyncMock(return_value=pad)), \
             patch('sogou_voice.start_session_voice', new=AsyncMock(return_value=frozenset({1}))):
            await session.start()
            pad.stdin.write.assert_called_once()
            payload = json.loads(pad.stdin.write.call_args.args[0])
            self.assertEqual(payload, {'target': {'id': SECOND, 'cwd': self.threads[SECOND]['cwd'],
                                                 'title': self.threads[SECOND]['name']}})
            self.assertEqual(await session.send('测试'), 'test-turn')
            copy.assert_called_once_with()
        read = call('thread/read', {'threadId': SECOND, 'includeTurns': False})
        self.assertEqual(session.client.request.await_args_list, [
            read, read, call('turn/start', {'threadId': SECOND,
                                            'input': [{'type': 'text', 'text': '测试'}]})])

    async def test_unreadable_link_stops_dictation_and_invalidates_previous_binding(self):
        session = self.session()
        await bind_target(session.client, FIRST, self.binding)
        session.client.request.reset_mock()
        with patch('sogou_chat.BINDING_FILE', self.binding), \
             patch('sogou_chat.bind_target', partial(bind_target, path=self.binding)), \
             patch('sogou_chat.copied_thread_id', side_effect=[BindingError('失效链接'), None]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn:
            for _ in range(2):
                with self.assertRaises(BindingError):
                    await session.start()
            spawn.assert_not_awaited()
        session.client.request.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
