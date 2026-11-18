"""Interactive macOS install, USB pairing, and per-user launch agents."""
import hashlib
import os
from pathlib import Path
import plistlib
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
from serial.tools.list_ports import comports
import sounddevice
from sogou_voice import VOICE_BINARY
from virtual_mic import select_device
from wifi_network import lan_address
from wifi_provision import provision, update_host
from wifi_settings import load_settings, save_settings

PORT = 8766
SETTINGS = ROOT / 'work/private/wifi-mic.json'
APP = Path('/Applications/ChatGPT.app')
DOMAIN = f'gui/{os.getuid()}'
AGENTS = Path.home() / 'Library/LaunchAgents'
JOBS = {
    'local.smart-mic.audio': ('bridge/wifi_microphone.py', []),
    'local.smart-mic.codex': ('tools/launch_desktop.py', ['--wait']),
}


def verify_bundle():
    folder = ROOT / 'release'
    sums = dict(line.split()[::-1] for line in (folder / 'SHA256SUMS').read_text().splitlines())
    for name in ('partition-table.bin', 'smart-mic.bin'):
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != sums[name]:
            raise RuntimeError(f'固件校验失败：{name}')
    return folder


def flash(port):
    bundle = verify_bundle()
    esp = [sys.executable, '-m', 'esptool', '--chip', 'esp32c3', '--port', port]
    table = (bundle / 'partition-table.bin').read_bytes()
    with tempfile.TemporaryDirectory() as temp:
        actual = Path(temp) / 'partition.bin'
        subprocess.run(esp + ['read-flash', '0x8000', str(len(table)), str(actual)], check=True)
        if actual.read_bytes() != table:
            raise RuntimeError('分区表不匹配，已停止烧录。')
    subprocess.run(esp + ['write-flash', '0x10000', str(bundle / 'smart-mic.bin')], check=True)


def install_jobs():
    AGENTS.mkdir(exist_ok=True)
    for label, (script, arguments) in JOBS.items():
        path = AGENTS / f'{label}.plist'
        target = f'{DOMAIN}/{label}'
        if subprocess.run(['/bin/launchctl', 'print', target],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            subprocess.run(['/bin/launchctl', 'bootout', target], check=True)
        job = dict(Label=label, ProgramArguments=[sys.executable, str(ROOT / script), *arguments],
                   WorkingDirectory=str(ROOT), RunAtLoad=True, LimitLoadToSessionType='Aqua')
        if script.startswith('bridge/'):
            job.update(KeepAlive=True, ThrottleInterval=15)
        else:
            job.update(AbandonProcessGroup=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(plistlib.dumps(job))
        temporary.replace(path)
        subprocess.run(['/bin/launchctl', 'bootstrap', DOMAIN, str(path)], check=True)


def main():
    if sys.platform != 'darwin':
        raise RuntimeError('本部署程序需要 macOS。')
    if not APP.is_dir() or not VOICE_BINARY.is_file():
        raise RuntimeError('请先安装 Codex 桌面应用及搜狗语音助手。')
    select_device('BlackHole 2ch')
    if sounddevice.query_devices(kind='input')['name'] != 'BlackHole 2ch':
        raise RuntimeError('请在 macOS 系统设置 → 声音 → 输入选择 BlackHole 2ch。')
    for legacy in ('local.esp32.sogou-microphone', 'local.esp32.codex-shared-desktop'):
        if (AGENTS / f'{legacy}.plist').exists():
            raise RuntimeError('检测到旧版登录启动项。请先停用旧版，避免两个音频桥同时运行。')
    host = lan_address()
    saved = load_settings(SETTINGS)
    if not saved or (saved['host'], saved['port']) != (host, PORT):
        ports = [item.device for item in comports() if (item.vid, item.pid) == (0x303a, 0x1001)]
        if len(ports) != 1:
            raise RuntimeError('请用 USB 数据线连接一台 AI Passport。')
        port = ports[0]
        if saved:
            key = bytes.fromhex(saved['key'])
            update_host(port, host, PORT)
        else:
            choice = input('确认连接的是 FoloToy AI Passport ESP32-C3 8MB。输入 flash 覆盖应用固件，输入 pair 配对已刷 Smart Mic 的设备：').strip()
            if choice == 'flash':
                flash(port)
            elif choice != 'pair':
                raise RuntimeError('已取消设备设置。')
            key = secrets.token_bytes(32)
            provision(port, host, PORT, key)
        save_settings(SETTINGS, key, host, PORT)
    install_jobs()
    print('已安装登录启动项。若 Codex 已打开，按 ⌘Q 后等待自动重开。')
    print('打开目标本地聊天，按 ⌘⌥L，再按设备第三键开始听写。')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f'部署未完成：{error}')
