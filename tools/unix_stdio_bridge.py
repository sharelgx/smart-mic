"""Transparent JSONL/WebSocket framing adapter; no RPC interpretation or polling."""
import asyncio
import sys
from websockets.asyncio.client import unix_connect


async def main(path):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64*1024*1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    transport, writer_protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, writer_protocol, None, loop)
    async with unix_connect(path, uri='ws://localhost/rpc', proxy=None,
                            max_size=64*1024*1024, ping_interval=None) as ws:
        async def up():
            while line := await reader.readline():
                await ws.send(line.decode().rstrip('\n'))
        async def down():
            async for message in ws:
                if not isinstance(message, str):
                    raise ValueError('Unexpected binary RPC frame')
                writer.write(message.encode()+b'\n')
                await writer.drain()
        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            await task


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
