"""Launch the installed Sogou voice assistant through its own CLI entry point.

The --from=toolbox route requests start, rather than the hotkey toggle route.
No keyboard events, composer access, text capture, or chat submission.
"""
import asyncio
import os
import socket
from pathlib import Path

VOICE_BINARY = Path.home() / 'Library/Application Support/Sogou/SpeechAssistant/sogou_voice_assistant.app/Contents/MacOS/sogou_voice_assistant'

PROFILE = Path.home() / 'Library/Application Support/sogou_voice_assistant_pc'


def notify_running(source):
    """Use the installed Electron 39 second-instance protocol for fixed voice actions."""
    link = PROFILE / 'SingletonSocket'
    if not link.is_socket():
        return False
    endpoint = link.resolve(strict=True)
    if (endpoint.stat().st_uid != os.getuid()
            or endpoint.parent.stat().st_uid != os.getuid()
            or endpoint.parent.stat().st_mode & 0o077
            or os.readlink(PROFILE / 'SingletonCookie') != os.readlink(endpoint.parent / 'SingletonCookie')):
        raise RuntimeError('搜狗本机接口身份核对失败')
    args = [str(VOICE_BINARY), '--from=' + source]
    fields = ['START', str(PROFILE), str(len(args)), *args]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(endpoint))
        client.sendall(chr(0).join(fields).encode())
        client.shutdown(socket.SHUT_WR)
        with client.makefile('rb') as reply:
            if reply.read(3) != b'ACK':
                raise RuntimeError('搜狗未确认本次按键')
    return True


async def _launch(source):
    if not VOICE_BINARY.is_file():
        raise RuntimeError('未找到搜狗语音助手，请先在搜狗中安装并打开语音输入')
    if source not in ('toolbox', 'hotkey'):
        raise ValueError('未知的搜狗按键')
    if await asyncio.to_thread(notify_running, source):
        return
    if source == 'hotkey':
        raise RuntimeError('本次搜狗进程已退出')
    process = await asyncio.create_subprocess_exec(
        str(VOICE_BINARY), '--from=' + source,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    # Existing assistant consumes the second-instance arguments and exits.
    # A cold-started primary remains alive; do not terminate it.
    try:
        code = await asyncio.wait_for(process.wait(), timeout=0.5)
        if code:
            raise RuntimeError(f'搜狗语音启动失败，退出码 {code}')
    except asyncio.TimeoutError:
        pass


async def start_voice():
    await _launch('toolbox')


def _recorders():
    from sogou_audio_state import active_input_pids
    return active_input_pids(VOICE_BINARY)


async def wait_voice_ready(timeout=4.0):
    deadline = asyncio.get_running_loop().time() + timeout
    previous = frozenset()
    while asyncio.get_running_loop().time() < deadline:
        current = _recorders()
        if current and current == previous:
            return current
        previous = current
        await asyncio.sleep(.10)
    raise RuntimeError('搜狗未开始收音，请检查麦克风设置')


async def start_session_voice():
    if _recorders():
        raise RuntimeError('搜狗已在收音，请先结束已有听写')
    await start_voice()
    return await wait_voice_ready()


async def finish_voice(pids):
    """Ask the same active Sogou session to finish once, then verify mic release."""
    if not pids or _recorders() != pids:
        raise RuntimeError('搜狗收音状态已变化，本次未提交')
    # Sogou currently exposes a toggle, so dispatch only after checking the
    # process identity. Never retry the toggle after an uncertain response.
    await _launch('hotkey')
    deadline = asyncio.get_running_loop().time() + 4.0
    quiet = 0
    while asyncio.get_running_loop().time() < deadline:
        active = _recorders()
        if not active:
            quiet += 1
            if quiet == 3:
                return
        elif active != pids:
            raise RuntimeError('搜狗收音进程已更换，本次未提交')
        else:
            quiet = 0
        await asyncio.sleep(.10)
    raise RuntimeError('未确认搜狗结束收音，本次未提交')
