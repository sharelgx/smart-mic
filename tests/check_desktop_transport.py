"""Real two-client transport check in a temporary CODEX_HOME; never creates a thread."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
sys.path.insert(0, str(ROOT/'bridge'))
from codex_push import CodexPush
from websockets.asyncio.client import unix_connect
from desktop_socket_proxy import REAL


async def rpc(proc, identifier, method, params):
    proc.stdin.write((json.dumps({'id':identifier,'method':method,'params':params})+'\n').encode())
    await proc.stdin.drain()
    while line := await asyncio.wait_for(proc.stdout.readline(), 20):
        value = json.loads(line)
        if value.get('id') == identifier:
            if 'error' in value:
                raise RuntimeError(str(value['error']))
            return value['result']
    raise RuntimeError('transport EOF')


async def handshake(proc):
    await rpc(proc, 1, 'initialize', {'clientInfo':{'name':'microbit_transport_check','version':'0.1'}})
    proc.stdin.write(b'{"method":"initialized","params":{}}\n')
    await proc.stdin.drain()


async def main():
    with tempfile.TemporaryDirectory(prefix='mbtransport-') as folder:
        sock = Path(folder)/'rpc.sock'
        (Path(folder)/'isolated-home').mkdir()
        env = {**os.environ, 'CODEX_HOME':str(Path(folder)/'isolated-home')}
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from desktop_socket_proxy import supervise,REAL;from pathlib import Path;'
                'sys.exit(supervise(REAL,sys.argv[3:],Path(sys.argv[2])))')
        desktop = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(ROOT/'tools'), str(sock),
            'app-server','--analytics-default-enabled','--listen','unix://'+str(sock),
            '-c','analytics.enabled=false','-c','sandbox_mode="read-only"',
            '-c','approval_policy="on-request"', env=env,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=None)
        voice = None
        try:
            await handshake(desktop)
            voice = await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'tools/unix_stdio_bridge.py'),str(sock),env=env,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=None)
            await handshake(voice)
            configs = [await rpc(proc,2,'config/read',{'includeLayers':False}) for proc in (desktop,voice)]
            for value in configs:
                config = value['config']
                assert config['sandbox_mode'] == 'read-only', config.get('sandbox_mode')
                assert config['approval_policy'] == 'on-request', config.get('approval_policy')
            assert configs[0]['config'] == configs[1]['config']
            direct = CodexPush('no-thread-created', ROOT, sock)
            direct.ws = await unix_connect(str(sock), uri='ws://localhost/rpc', proxy=None, ping_interval=None)
            direct.reader = asyncio.create_task(direct._read())
            try:
                await direct.request('initialize', {'clientInfo':{'name':'voice_protocol_check','version':'0.1'}})
                await direct._send({'method':'initialized', 'params':{}})
                config = await direct.request('config/read', {'includeLayers':False})
                assert config['config'] == configs[0]['config']
            finally:
                await direct.close()
            print('PASS: voice client JSON-RPC request/response over actual Unix WebSocket')
            print('PASS: two clients, one real backend, identical read-only/on-request config; no thread created')
        finally:
            if voice:
                voice.terminate()
                await voice.wait()
            desktop.stdin.close()
            try:
                await asyncio.wait_for(desktop.wait(),10)
            except asyncio.TimeoutError:
                desktop.terminate()
                await asyncio.wait_for(desktop.wait(),10)
        assert not sock.exists(), 'owned socket not cleaned'
        print('PASS: owned backend stopped and socket removed')


if __name__ == '__main__':
    asyncio.run(main())
