#!/usr/bin/env python3
"""ESP32 Wi-Fi microphone with Sogou dictation and optional current-chat delivery."""
import argparse
import asyncio
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from virtual_mic import VirtualMicrophone,select_device
from wifi_protocol import START,AUDIO,END,CANCEL,RESULT,READY,authenticate,receive,RATE
from wifi_settings import load_settings,save_settings
from wifi_network import lan_address
from wifi_provision import provision,update_host
ROOT = Path(__file__).resolve().parents[1]
from codex_push import DEFAULT_SOCKET
from chat_binding import BindingError
from sogou_chat import LiveDictation, SogouChat
import secrets
import ipaddress


def show_binding_hint():
    """Show a short, click-through panel without activating the microphone app."""
    import AppKit as A

    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
    app.finishLaunching()
    screen = A.NSScreen.mainScreen()
    if screen is None:
        return
    frame = screen.visibleFrame()
    width, height = 386, 84
    rect = A.NSMakeRect(A.NSMaxX(frame) - width - 16, A.NSMaxY(frame) - height - 12,
                        width, height)
    panel = A.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, A.NSWindowStyleMaskNonactivatingPanel, A.NSBackingStoreBuffered, False)
    panel.setLevel_(A.NSFloatingWindowLevel)
    panel.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces |
                                 A.NSWindowCollectionBehaviorFullScreenAuxiliary)
    panel.setHidesOnDeactivate_(False)
    panel.setIgnoresMouseEvents_(True)
    panel.setHasShadow_(True)
    title = A.NSTextField.labelWithString_('ESP32 麦克风 · 聊天绑定未完成')
    title.setFont_(A.NSFont.boldSystemFontOfSize_(15))
    title.setFrame_(A.NSMakeRect(16, 48, width - 32, 22))
    body = A.NSTextField.wrappingLabelWithString_('在目标聊天点「… → 复制 → 复制深度链接」（⌘⌥L），再按设备最下方的第三键。')
    body.setFrame_(A.NSMakeRect(16, 9, width - 32, 36))
    panel.contentView().addSubview_(title)
    panel.contentView().addSubview_(body)
    panel.orderFrontRegardless()
    panel.displayIfNeeded()
    A.NSRunLoop.currentRunLoop().runUntilDate_(A.NSDate.dateWithTimeIntervalSinceNow_(7))


def run_binding_hint():
    # AppKit needs its own main thread. Give it no peer data or inherited audio streams.
    try:
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--binding-hint'],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=12, check=True)
    except (OSError, subprocess.SubprocessError):
        print('电脑未能显示聊天绑定提示。', flush=True)


class MicrophoneBridge:
    def __init__(self,key,sink,start_voice=None,chat_factory=None):
        self.key=key; self.sink=sink; self.owner=None; self.writer=None
        self.start_voice=start_voice
        self.chat_factory=chat_factory
        self.handover=asyncio.Lock()
        self.binding_hint_task=None

    async def handle(self,reader,writer):
        current=asyncio.current_task(); active=False; live=None; session=None
        try:
            up,down=await authenticate(reader,writer,self.key)
            async with self.handover:
                if self.owner:
                    old=self.owner
                    self.writer.close(); old.cancel()
                    await asyncio.gather(old,return_exceptions=True)
                self.owner=current; self.writer=writer
            writer.write(down.encode(READY,b'virtual-mic')); await writer.drain()
            print('设备已连接虚拟麦克风。按右侧最下面第三键唤起搜狗并开始收音。',flush=True)
            while True:
                timeout=max(.01,65-(time.monotonic()-started)) if active else None
                kind,payload=await receive(reader,up,timeout)
                if kind==START:
                    if active or json.loads(payload)!={'rate':RATE,'bits':16,'channels':1}: raise ValueError('invalid start')
                    session=self.chat_factory() if self.chat_factory else None
                    if session:
                        live=LiveDictation(self.sink,session)
                    else:
                        if self.start_voice:
                            await self.start_voice()
                        generation=self.sink.begin()
                    active=True; count=0; sha=hashlib.sha256(); started=time.monotonic()
                    print('正在实时向虚拟麦克风送音频……',flush=True)
                elif kind==AUDIO:
                    if not active or not payload or len(payload)%2 or count+len(payload)>RATE*2*60: raise ValueError('invalid audio')
                    sha.update(payload); count+=len(payload)
                    (live or self.sink).feed(payload)
                elif kind==END:
                    end=json.loads(payload)
                    if not active or count==0 or end.get('bytes')!=count or end.get('sha256')!=sha.hexdigest(): raise ValueError('incomplete audio')
                    result=b'checked'
                    if live:
                        try:
                            # Firmware also emits END at its 60-second limit, without a press.
                            if count == RATE*2*60:
                                result=b'cancelled'
                            else:
                                result=await live.finish(lambda: self.owner is current and not reader.at_eof() and not writer.is_closing())
                        finally:
                            await live.close()
                            live=None
                    else:
                        await asyncio.to_thread(self.sink.finish,generation)
                    active=False
                    print(f'本段 {count/(RATE*2):.2f} 秒，结果 {result.decode()}。',flush=True)
                    writer.write(down.encode(RESULT,result)); await writer.drain()
                elif kind==CANCEL:
                    if live:
                        await live.close()
                        live=None
                    self.sink.cancel(); active=False
                    writer.write(down.encode(RESULT,b'cancelled')); await writer.drain()
                else: raise ValueError('unexpected frame')
        except (asyncio.IncompleteReadError,ConnectionError):
            print('连接已断开，等待重连。本段未收到的回执不自动重试。',flush=True)
        except Exception as exc:
            # Protocol or server error payloads can include text supplied by a peer.
            stage=getattr(session,'stage','接收音频')
            print(f'本段中止（{stage}，{type(exc).__name__}）。',flush=True)
            if isinstance(exc, BindingError) and self.owner is current and session:
                if self.binding_hint_task is None or self.binding_hint_task.done():
                    self.binding_hint_task = asyncio.create_task(asyncio.to_thread(run_binding_hint))
        finally:
            try:
                if live:
                    await live.close()
            finally:
                if self.owner is current:
                    self.sink.cancel(); self.owner=None; self.writer=None
                writer.close()
            try: await writer.wait_closed()
            except ConnectionError: pass

async def run(args):
    saved=load_settings(args.settings)
    key=bytes.fromhex(saved['key']) if saved else secrets.token_bytes(32)
    sink=VirtualMicrophone(args.device)
    try:
        from sogou_voice import start_voice
        def current_chat():
            if DEFAULT_SOCKET.is_socket():
                return SogouChat()
            print('共享聊天接口未开启，本次搜狗仅上屏。',flush=True)
            return None
        bridge=MicrophoneBridge(key,sink,start_voice=start_voice,chat_factory=current_chat)
        async with await asyncio.start_server(bridge.handle,args.bind,args.port,limit=8192,backlog=2) as server:
            print(f'Wi-Fi 麦克风 {args.bind}:{args.port} → {args.device}，共享接口就绪后可按键发送。',flush=True)
            if not saved or args.configure:
                if not args.usb_port: raise RuntimeError('首次配置需要 USB')
                await asyncio.to_thread(provision,args.usb_port,args.bind,args.port,key)
                save_settings(args.settings,key,args.bind,args.port)
            elif saved['host']!=args.bind or saved['port']!=args.port:
                if not args.usb_port: raise RuntimeError('Mac 地址变化，请插 USB 更新地址')
                await asyncio.to_thread(update_host,args.usb_port,args.bind,args.port)
                save_settings(args.settings,key,args.bind,args.port)
            await server.serve_forever()
    finally: sink.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device',default='BlackHole 2ch')
    parser.add_argument('--list-devices',action='store_true')
    parser.add_argument('--binding-hint',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--bind')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--usb-port')
    parser.add_argument('--configure',action='store_true')
    parser.add_argument('--settings',type=Path,default=ROOT/'work/private/wifi-mic.json')
    args=parser.parse_args()
    if args.binding_hint:
        show_binding_hint()
        return
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices()); return
    args.bind=args.bind or lan_address()
    address=ipaddress.ip_address(args.bind)
    if not address.is_private or args.bind=='0.0.0.0': parser.error('请使用本机局域网地址')
    if not args.usb_port:
        from serial.tools.list_ports import comports
        candidates=[p.device for p in comports() if p.vid==0x303a and p.pid==0x1001]
        if len(candidates)==1: args.usb_port=candidates[0]
    select_device(args.device)  # never silently output to the real speakers
    with (ROOT/'work/wifi-voice.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        asyncio.run(run(args))

if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: print('音频桥已停止。')
    except Exception as exc:
        print('错误：'+str(exc),file=sys.stderr); sys.exit(1)
