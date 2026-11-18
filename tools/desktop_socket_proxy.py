#!/usr/bin/env python3
"""Desktop CLI override: same server, same arguments, Unix socket + stdio proxy.

No thread creation/resumption, app bundle patches, persistent daemon, or credentials.
"""
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
REAL = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
SOCKET = Path.home() / 'Library/Caches/microbit-codex-voice/desktop.sock'
SUBCOMMANDS = {'proxy', 'daemon', 'generate-ts', 'generate-json-schema', 'help'}


def server_args(args, path):
    """Only intercept the desktop's ordinary app-server invocation."""
    if 'app-server' not in args or '--analytics-default-enabled' not in args:
        return None
    index = args.index('app-server')
    if index+1 < len(args) and args[index+1] in SUBCOMMANDS:
        return None
    if any(x in args for x in ('--help','-h','--version','--print-startup-config')):
        return None
    if '--stdio' in args or any(x == '--listen' or x.startswith('--listen=') for x in args):
        raise ValueError('Refusing to replace an explicitly selected transport')
    return [*args, '--listen', 'unix://'+str(path)]


def supervise(real, args, path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock = open(path.with_suffix('.lock'), 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('A desktop socket owner is already running; refusing to merge backends')
    if path.exists():
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink(missing_ok=True)
            else:
                raise RuntimeError('Socket already active; refusing to attach to an unrelated backend')
    child = None
    proxy = None
    cancelled = threading.Event()
    def stop(signum, frame):
        cancelled.set()
        for proc in (proxy, child):
            if proc is not None and proc.poll() is None:
                proc.terminate()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        # All config/permission/plugin flags and the environment are inherited unchanged.
        child = subprocess.Popen([real, *args], stdin=subprocess.DEVNULL,
                                 stdout=sys.stderr, stderr=sys.stderr)
        deadline = time.monotonic()+20
        # Bounded startup readiness only; voice delivery itself never polls.
        while not path.exists():
            if cancelled.wait(.05) or child.poll() is not None:
                raise RuntimeError('Desktop backend exited before its socket became ready')
            if time.monotonic() >= deadline:
                raise RuntimeError('Desktop backend socket startup timed out')
        os.chmod(path, 0o600)
        proxy = subprocess.Popen([sys.executable, str(ROOT/'tools/unix_stdio_bridge.py'), str(path)],
                                 stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        def watch_child():
            child.wait()
            if proxy.poll() is None:
                proxy.terminate()
        threading.Thread(target=watch_child, daemon=True).start()
        return proxy.wait()
    finally:
        for proc in (proxy, child):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        path.unlink(missing_ok=True)
        lock.close()


def main():
    args = sys.argv[1:]
    changed = server_args(args, SOCKET)
    if changed is None:
        os.execv(REAL, [REAL, *args])
    return supervise(REAL, changed, SOCKET)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('Desktop socket proxy: '+str(exc), file=sys.stderr)
        sys.exit(1)
