"""Bounded real-time PCM16/16k -> float32 stereo/48k virtual microphone sink."""
from collections import deque
from threading import Condition
import time
import numpy as np

class AudioBuffer:
    def __init__(self,rate=48000,prebuffer_ms=60,limit_ms=400):
        self.rate=rate
        self.prebuffer=rate*prebuffer_ms//1000
        self.limit=rate*limit_ms//1000
        self.condition=Condition()
        self.chunks=deque(); self.queued=0; self.offset=0
        self.enabled=False; self.started=False; self.flushing=False; self.failed=False
        self.last=0.0; self.input_samples=0; self.played_frames=0; self.underruns=0
        self.generation=0

    def begin(self):
        with self.condition:
            self.generation+=1
            self.chunks.clear(); self.queued=0; self.offset=0
            self.enabled=True; self.started=False; self.flushing=False; self.failed=False
            self.last=0.0; self.input_samples=0; self.played_frames=0; self.underruns=0
            return self.generation

    def feed(self,pcm):
        samples=np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768
        if not len(samples) or len(pcm)%2: raise ValueError('invalid PCM16 chunk')
        with self.condition:
            if not self.enabled or self.failed: raise RuntimeError('audio session inactive')
            previous=np.empty_like(samples); previous[0]=self.last; previous[1:]=samples[:-1]
            phases=np.array([1/3,2/3,1],dtype=np.float32)
            mono=(previous[:,None]+(samples-previous)[:,None]*phases).reshape(-1)
            self.last=float(samples[-1]); self.input_samples+=len(samples)
            stereo=np.repeat(mono[:,None],2,axis=1)
            if self.queued+len(stereo)>self.limit:
                self._clear(); self.failed=True
                raise BufferError('音频缓冲溢出，本段已停止；请在搜狗取消本次输入')
            self.chunks.append(stereo); self.queued+=len(stereo)

    def _clear(self):
        self.chunks.clear(); self.queued=0; self.offset=0
        self.enabled=False; self.started=False
        self.condition.notify_all()

    def cancel(self):
        with self.condition:
            self.generation+=1; self._clear()

    def render(self,out):
        out.fill(0)
        with self.condition:
            if not self.enabled: return
            if not self.started:
                if self.queued<self.prebuffer and not self.flushing: return
                self.started=True
            written=0
            while self.chunks and written<len(out):
                chunk=self.chunks[0]
                n=min(len(out)-written,len(chunk)-self.offset)
                out[written:written+n]=chunk[self.offset:self.offset+n]
                written+=n; self.offset+=n; self.queued-=n; self.played_frames+=n
                if self.offset==len(chunk): self.chunks.popleft(); self.offset=0
            if written<len(out) and not self.flushing: self.underruns+=1
            if self.queued==0: self.condition.notify_all()

    def finish(self,generation=None,timeout=2):
        with self.condition:
            generation=self.generation if generation is None else generation
            if generation!=self.generation: raise RuntimeError('old audio session')
            self.flushing=True
            end=time.monotonic()+timeout
            while self.queued and not self.failed:
                if generation!=self.generation: raise RuntimeError('old audio session')
                remaining=end-time.monotonic()
                if remaining<=0:
                    self._clear(); raise TimeoutError('虚拟声卡未及时取走音频')
                self.condition.wait(remaining)
            if generation!=self.generation: raise RuntimeError('old audio session')
            stats={'input_samples':self.input_samples,'output_frames':self.played_frames,'underruns':self.underruns}
            self._clear()
            return stats


def select_device(name='BlackHole 2ch'):
    import sounddevice as sd
    found=[(i,d) for i,d in enumerate(sd.query_devices()) if d['name']==name]
    if len(found)!=1: raise RuntimeError('未发现唯一的 '+name+'；请先完成虚拟声卡安装')
    index,device=found[0]
    if device['max_input_channels']<2 or device['max_output_channels']<2:
        raise RuntimeError('所选设备必须同时提供麦克风输入和双声道输出')
    return index

class VirtualMicrophone:
    def __init__(self,name='BlackHole 2ch',monitor=None):
        import sounddevice as sd
        self.buffer=AudioBuffer()
        self.status_count=0
        def callback(incoming,out,frames,when,status):
            if status: self.status_count+=1
            self.buffer.render(out)
            if monitor is not None: monitor(incoming)
        self.device=select_device(name)
        sd.check_output_settings(device=self.device,samplerate=48000,channels=2,dtype='float32')
        self.stream=sd.Stream(device=(self.device,self.device),samplerate=48000,channels=(2,2),
            dtype='float32',latency='low',blocksize=0,callback=callback)
        self.stream.start()

    def begin(self): return self.buffer.begin()
    def feed(self,pcm): self.buffer.feed(pcm)
    def cancel(self): self.buffer.cancel()
    def finish(self,generation=None): return self.buffer.finish(generation)
    def close(self):
        self.buffer.cancel(); self.stream.abort(); self.stream.close()
