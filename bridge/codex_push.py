"""Event-driven attachment to an EXISTING Codex app-server; never starts one."""
import asyncio
import json
from pathlib import Path
from websockets.asyncio.client import unix_connect
import stat

DEFAULT_SOCKET = Path.home() / 'Library/Caches/microbit-codex-voice/desktop.sock'


class CodexPush:
    def __init__(self, thread, cwd, socket=DEFAULT_SOCKET, executable='codex'):
        self.thread, self.cwd = thread, Path(cwd).resolve()
        self.socket, self.executable = Path(socket), executable
        self.process = None
        self.reader = None
        self.pending = {}
        self.sequence = 0

    async def connect(self, verify=True):
        if not self.socket.exists() or not stat.S_ISSOCK(self.socket.stat().st_mode):
            raise RuntimeError('当前聊天的共享控制接口未开放：' + str(self.socket)
                               + '；不能实时推送，未启动新会话，也不会改用定时检查。')
        self.ws = await unix_connect(str(self.socket), uri='ws://localhost/rpc', proxy=None,
                                     max_size=64*1024*1024, ping_interval=None)
        self.reader = asyncio.create_task(self._read())
        try:
            await self.request('initialize', {'clientInfo': {'name':'microbit_voice', 'version':'0.1.0'}})
            await self._send({'method':'initialized', 'params':{}})
            if verify:
                await self.verify_target()
        except BaseException:
            await self.close()
            raise

    async def _send(self, value):
        await self.ws.send(json.dumps(value, ensure_ascii=False))

    async def _read(self):
        try:
            async for line in self.ws:
                value = json.loads(line)
                if 'method' in value:
                    if 'id' in value:
                        # Never approve or reject on behalf of the desktop user.
                        print('Codex 请求人工审批或工具输入；语音端不代答，请查看桌面聊天。', flush=True)
                    continue
                future = self.pending.get(value.get('id'))
                if future is not None and not future.done():
                    if 'error' in value:
                        future.set_exception(RuntimeError(str(value['error'])))
                    else:
                        future.set_result(value.get('result', {}))
            raise RuntimeError('当前聊天推送连接已关闭；不自动重试消息，以免重复执行')
        except BaseException as exc:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(str(exc)))

    async def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({'id':request_id, 'method':method, 'params':params})
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(request_id, None)

    async def verify_target(self):
        result = await self.request('thread/read', {'threadId':self.thread, 'includeTurns':False})
        thread = result['thread']
        cwd = thread.get('cwd')
        if (thread.get('id') != self.thread or not cwd or not Path(cwd).is_absolute()
                or Path(cwd).resolve() != self.cwd):
            raise RuntimeError('接口返回的聊天或项目目录不匹配，拒绝推送')
        state = thread.get('status', {}).get('type')
        if state not in ('idle', 'active'):
            raise RuntimeError('该后端未加载当前聊天；不通过 resume 或新建会话替代')
        return state

    async def push(self, text, *, before_submit=None):
        if not text.strip() or len(text) > 8000:
            raise ValueError('语音文字为空或过长')
        state = await self.verify_target()
        if state == 'active':
            turns = await self.request('thread/turns/list', {'threadId':self.thread,
                                      'limit':1, 'sortDirection':'desc', 'itemsView':'notLoaded'})
            latest = turns.get('data', [])
            if not latest or latest[0].get('status') != 'inProgress' or not latest[0].get('id'):
                raise RuntimeError('当前轮次已变化，未发送；不自动重试')
            expected = latest[0]['id']
            if before_submit:
                before_submit()
            result = await self.request('turn/steer', {'threadId':self.thread,
                                        'expectedTurnId':expected,
                                        'input':[{'type':'text', 'text':text}]})
            if result.get('turnId') != expected:
                raise RuntimeError('未取得目标轮次的发送确认；状态不明，不自动重发')
            return expected
        # Omit all settings overrides: retain the thread's permissions and model.
        if before_submit:
            before_submit()
        result = await self.request('turn/start', {'threadId':self.thread,
                                    'input':[{'type':'text', 'text':text}]})
        turn = result.get('turn', {})
        if not turn.get('id'):
            raise RuntimeError('未取得发送确认；状态不明，不自动重发')
        return turn['id']

    async def close(self):
        if hasattr(self, 'ws'):
            await self.ws.close()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
