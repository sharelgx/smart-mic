"""Read only microphone-use metadata reported by macOS Core Audio."""
import ctypes
from functools import lru_cache
from pathlib import Path


_u32 = ctypes.c_uint32


def _fourcc(value):
    return int.from_bytes(value.encode('ascii'), 'big')


class _Address(ctypes.Structure):
    _fields_ = [('selector', _u32), ('scope', _u32), ('element', _u32)]


@lru_cache
def _libraries():
    audio = ctypes.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
    common = [_u32, ctypes.POINTER(_Address), _u32, ctypes.c_void_p, ctypes.POINTER(_u32)]
    audio.AudioObjectGetPropertyDataSize.argtypes = common
    audio.AudioObjectGetPropertyData.argtypes = common + [ctypes.c_void_p]
    proc = ctypes.CDLL('/usr/lib/libproc.dylib')
    proc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, _u32]
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, _u32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    return audio, proc, cf


def _read_words(audio, obj, selector, scope='glob'):
    address = _Address(_fourcc(selector), _fourcc(scope), 0)
    size = _u32()
    args = (obj, ctypes.byref(address), 0, None, ctypes.byref(size))
    if audio.AudioObjectGetPropertyDataSize(*args) or size.value % 4:
        raise RuntimeError('无法读取 macOS 收音状态，请重试')
    if size.value == 0:
        return []
    words = (_u32 * (size.value // 4))()
    if audio.AudioObjectGetPropertyData(*args, words):
        raise RuntimeError('macOS 音频设备状态改变，请重试')
    return words[:size.value // 4]


def _device_name(audio, cf, device):
    address = _Address(_fourcc('lnam'), _fourcc('glob'), 0)
    name = ctypes.c_void_p()
    size = _u32(ctypes.sizeof(name))
    if audio.AudioObjectGetPropertyData(device, ctypes.byref(address), 0, None,
                                        ctypes.byref(size), ctypes.byref(name)) or not name:
        raise RuntimeError('无法读取搜狗使用的麦克风名称')
    try:
        result = ctypes.create_string_buffer(1024)
        if not cf.CFStringGetCString(name, result, len(result), 0x08000100):  # UTF-8
            raise RuntimeError('无法读取搜狗使用的麦克风名称')
        return result.value.decode()
    finally:
        cf.CFRelease(name)


def active_input_pids(voice_binary, device_name='BlackHole 2ch'):
    """AudioHardware.h process list, PID and active input selectors (macOS 14.2+)."""
    audio, proc, cf = _libraries()
    bundle = voice_binary.resolve().parents[2]
    found = set()
    for obj in _read_words(audio, 1, 'prs#'):
        if _read_words(audio, obj, 'piri') != [1]:
            continue
        pids = _read_words(audio, obj, 'ppid')
        if len(pids) != 1:
            raise RuntimeError('无法确认收音进程')
        path = ctypes.create_string_buffer(4096)
        if proc.proc_pidpath(pids[0], path, len(path)) <= 0:
            raise RuntimeError('无法确认收音进程路径')
        executable = Path(path.value.decode(errors='surrogateescape')).resolve()
        if executable.is_relative_to(bundle):
            inputs = _read_words(audio, obj, 'pdv#', scope='inpt')
            if {_device_name(audio, cf, device) for device in inputs} != {device_name}:
                raise RuntimeError(f'请将搜狗使用的麦克风设为 {device_name}')
            found.add(pids[0])
    return frozenset(found)
