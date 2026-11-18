import asyncio
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from chat_binding import BindingError
from wifi_microphone import MicrophoneBridge
from wifi_protocol import AUDIO, RATE, START


class BindingHintTests(unittest.IsolatedAsyncioTestCase):
    async def fail_recording(self, bridge, error):
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        live = Mock(close=AsyncMock())
        live.feed.side_effect = error
        frames = [(START, json.dumps({'rate': RATE, 'bits': 16, 'channels': 1}).encode()),
                  (AUDIO, b'\0\0')]
        with patch('wifi_microphone.authenticate', AsyncMock(return_value=(Mock(), Mock()))), \
             patch('wifi_microphone.receive', AsyncMock(side_effect=frames)), \
             patch('wifi_microphone.LiveDictation', return_value=live):
            await bridge.handle(Mock(), writer)
        writer.close.assert_called_once()
        live.close.assert_awaited_once()
        self.assertIsNone(bridge.owner)

    async def test_binding_failure_shows_one_fixed_hint_without_delaying_cleanup(self):
        bridge = MicrophoneBridge(b'', Mock(), chat_factory=Mock)
        dismiss = threading.Event()
        with patch('wifi_microphone.run_binding_hint', side_effect=lambda: dismiss.wait(3)) as show:
            try:
                for _ in range(2):
                    await self.fail_recording(bridge, BindingError('private peer value'))
                self.assertFalse(bridge.binding_hint_task.done())
            finally:
                dismiss.set()
                await bridge.binding_hint_task
            show.assert_called_once_with()

    async def test_unrelated_failure_does_not_prompt_to_bind(self):
        bridge = MicrophoneBridge(b'', Mock(), chat_factory=Mock)
        await self.fail_recording(bridge, ValueError('audio error'))
        self.assertIsNone(bridge.binding_hint_task)
