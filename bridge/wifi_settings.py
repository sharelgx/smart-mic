"""Explicitly authorized local Passport pairing config; never includes Wi-Fi password."""
import json
import os
from pathlib import Path
import stat
import tempfile


def load_settings(path):
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('配对配置不能是符号链接')
    if not path.exists():
        return None
    if path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('配对配置权限不安全，需要当前用户拥有且仅用户可读写（600）')
    value=json.loads(path.read_text())
    if value.get('version')!=1 or len(bytes.fromhex(value['key']))!=32:
        raise ValueError('配对配置格式不正确')
    return value


def save_settings(path,key,host,port):
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('配对配置不能是符号链接')
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(path.parent,0o700)
    value={'version':1,'key':key.hex(),'host':host,'port':port}
    descriptor,name=tempfile.mkstemp(prefix='.pair-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w') as stream:
            json.dump(value,stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)
