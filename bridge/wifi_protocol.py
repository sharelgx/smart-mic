"""Authenticated, encrypted Passport audio frames. No files or credentials stored."""
import asyncio
import hashlib
import hmac
import json
import secrets
import struct
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b'PVM1'
START, AUDIO, END, CANCEL, RESULT, READY = range(1, 7)
MAX_FRAME = 4096
RATE = 16000
MAX_SECONDS = 60


def session_keys(key, challenge, nonce):
    return tuple(hmac.digest(key, label + challenge + nonce, 'sha256')
                 for label in (b'passport-up', b'passport-down'))


class Frames:
    def __init__(self, key):
        self.cipher = AESGCM(key)
        self.sequence = 0

    def encode(self, kind, payload=b''):
        plain = bytes([kind]) + payload
        size = len(plain) + 16
        if not 17 <= size <= MAX_FRAME:
            raise ValueError('frame size')
        header = struct.pack('!QI', self.sequence, size)
        nonce = b'\0' * 4 + header[:8]
        result = header + self.cipher.encrypt(nonce, plain, header)
        self.sequence += 1
        return result

    def decode(self, packet):
        if len(packet) < 12:
            raise ValueError('short frame')
        seq, size = struct.unpack('!QI', packet[:12])
        if seq != self.sequence or not 17 <= size <= MAX_FRAME or len(packet) != 12 + size:
            raise ValueError('out of order or invalid length')
        plain = self.cipher.decrypt(b'\0' * 4 + packet[:8], packet[12:], packet[:12])
        self.sequence += 1
        return plain[0], plain[1:]


async def receive(reader, frames, timeout=15):
    header = await asyncio.wait_for(reader.readexactly(12), timeout)
    size = struct.unpack('!I', header[8:])[0]
    if not 17 <= size <= MAX_FRAME:
        raise ValueError('frame size')
    body = await asyncio.wait_for(reader.readexactly(size), timeout)
    return frames.decode(header + body)


async def authenticate(reader, writer, key):
    challenge = secrets.token_bytes(16)
    writer.write(MAGIC + challenge)
    await writer.drain()
    reply = await asyncio.wait_for(reader.readexactly(48), 5)
    nonce, signature = reply[:16], reply[16:]
    expected = hmac.digest(key, MAGIC + challenge + nonce, 'sha256')
    if not hmac.compare_digest(signature, expected):
        raise ValueError('unpaired device')
    up, down = session_keys(key, challenge, nonce)
    return Frames(up), Frames(down)


class Recording:
    """Fail closed on missing, cancelled, duplicate, oversized or malformed audio."""
    def __init__(self):
        self.pcm = bytearray()
        self.started = False
        self.finished = False

    def feed(self, kind, payload):
        if self.finished:
            raise ValueError('recording already ended')
        if kind == START:
            if self.started or json.loads(payload) != {'rate': RATE, 'bits': 16, 'channels': 1}:
                raise ValueError('invalid audio format/start')
            self.started = True
        elif kind == AUDIO:
            if not self.started or not payload or len(payload) % 2:
                raise ValueError('invalid audio chunk')
            if len(self.pcm) + len(payload) > RATE * 2 * MAX_SECONDS:
                raise ValueError('recording exceeds limit')
            self.pcm.extend(payload)
        elif kind == CANCEL:
            self.pcm.clear()
            self.finished = True
        elif kind == END:
            if not self.started or not self.pcm:
                raise ValueError('empty recording')
            end = json.loads(payload)
            if end.get('bytes') != len(self.pcm) or end.get('sha256') != hashlib.sha256(self.pcm).hexdigest():
                raise ValueError('incomplete audio')
            self.finished = True
            return bytes(self.pcm)
        else:
            raise ValueError('unexpected audio frame')
        return None
