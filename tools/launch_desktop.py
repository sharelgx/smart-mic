"""Launch the signed desktop app with a per-process CLI override."""
import os
from pathlib import Path
import subprocess
import sys
import json
import time
import select
import errno
import fcntl

ROOT = Path(__file__).resolve().parents[1]
APP = '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'


def record(event, **fields):
    path = ROOT/'work/desktop-launch.jsonl'
    path.parent.mkdir(exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps({'time':time.time(), 'event':event, **fields})+'\n')


def wait_for_exit(pid):
    # Kernel process-exit event, not periodic polling.
    queue = select.kqueue()
    try:
        event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                             flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                             fflags=select.KQ_NOTE_EXIT)
        try:
            queue.control([event], 1, None)
        except ProcessLookupError:
            return
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise

    finally:
        queue.close()


def main():
    lock_path = ROOT/'work/desktop-launch.lock'
    lock_path.parent.mkdir(exist_ok=True)
    lock = lock_path.open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('已有启动器在等待退出，请勿重复打开。', flush=True)
        return 1
    record('invoked', normal='--normal' in sys.argv)
    processes = subprocess.run(['/bin/ps','-axo','pid=,comm='], capture_output=True,
                               text=True, check=True).stdout.splitlines()
    for line in processes:
        parts = line.strip().split(None,1)
        if len(parts) == 2 and parts[1] == APP:
            pid = int(parts[0])
            if '--wait' not in sys.argv:
                record('blocked_running', pid=pid)
                print('桌面应用仍在运行。请按 Cmd+Q 完全退出，再运行启动器；不会强制结束其他任务。')
                return 1
            record('waiting_exit', pid=pid)
            print('启动器已就绪。请回到 Codex 按 Cmd+Q；旧进程退出后会自动重新打开，无需再点击图标。', flush=True)
            wait_for_exit(pid)
            record('old_process_exited', pid=pid)
    env = dict(os.environ)
    if '--normal' in sys.argv:
        env.pop('CODEX_CLI_PATH', None)
    else:
        env['CODEX_CLI_PATH'] = str(ROOT/'tools/codex-desktop-with-socket')
    child = subprocess.Popen([APP], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    record('spawned', pid=child.pid, override='CODEX_CLI_PATH' in env)
    print('已启动桌面应用。请打开原聊天，再验证共享接口；此启动器不会创建聊天。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
