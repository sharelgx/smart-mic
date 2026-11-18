"""Interactive macOS install, USB pairing, and per-user launch agents."""
import hashlib
import json
import asyncio
from contextlib import redirect_stdout
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
        subprocess.run(esp + ['read-flash', '0x8000', str(len(table)), str(actual)], check=True, stdout=sys.stderr)
        if actual.read_bytes() != table:
            raise RuntimeError('分区表不匹配，已停止烧录。')
    subprocess.run(esp + ['write-flash', '0x10000', str(bundle / 'smart-mic.bin')], check=True, stdout=sys.stderr)


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


def check_mac():
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
    verify_bundle()
    lan_address()


def usb_port():
    ports = [item.device for item in comports() if (item.vid, item.pid) == (0x303a, 0x1001)]
    if len(ports) != 1:
        raise RuntimeError('请用 USB 数据线只连接一台 AI Passport，再重新检测。')
    return ports[0]


def device_status():
    saved = load_settings(SETTINGS)
    host = lan_address()
    if saved and (saved['host'], saved['port']) == (host, PORT):
        return 'paired'
    usb_port()
    return 'update' if saved else 'new'


def pair(data):
    host = lan_address()
    saved = load_settings(SETTINGS)
    if saved:
        if (saved['host'], saved['port']) != (host, PORT):
            update_host(usb_port(), host, PORT)
        key = bytes.fromhex(saved['key'])
    else:
        ssid, password = data.get('ssid'), data.get('password')
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
            raise ValueError('请输入 1 至 32 字节的 Wi-Fi 名称。')
        if not isinstance(password, str) or len(password.encode()) > 63:
            raise ValueError('Wi-Fi 密码长度无效。')
        port = usb_port()
        if data.get('flash') is True:
            flash(port)
            port = usb_port()
        key = secrets.token_bytes(32)
        provision(port, host, PORT, key, ssid=ssid, password=password)
    save_settings(SETTINGS, key, host, PORT)


def verify_services():
    from codex_push import CodexPush, DEFAULT_SOCKET

    if not DEFAULT_SOCKET.is_socket():
        raise RuntimeError('共享接口正在等待 Codex 完全退出。请切回 Codex 按 ⌘Q，等应用自动重新打开后重试。')

    async def check_socket():
        client = CodexPush('', ROOT)
        try:
            await client.connect(verify=False)
        finally:
            await client.close()

    try:
        asyncio.run(check_socket())
    except Exception as error:
        raise RuntimeError('共享接口暂时无法连接。请等待 Codex 启动完成，再重新检测。') from error
    connections = subprocess.run(
        ['/usr/sbin/lsof', '-nP', f'-iTCP:{PORT}', '-sTCP:ESTABLISHED'],
        capture_output=True, text=True, check=False)
    status = subprocess.run(['/bin/launchctl', 'print', f'{DOMAIN}/local.smart-mic.audio'],
                            capture_output=True, text=True, check=False)
    pid = next((line.split('=')[-1].strip() for line in status.stdout.splitlines()
                if line.strip().startswith('pid =')), None)
    if not pid or not any(row.split()[1:2] == [pid] for row in connections.stdout.splitlines()):
        raise RuntimeError('音频桥还没检测到设备连接。请确认设备开机，Mac 和设备处于同一局域网，再重试。')


if __name__ == '__main__':
    action = sys.argv[1]
    data = json.load(sys.stdin)
    try:
        with redirect_stdout(sys.stderr):
            if action == 'mac':
                check_mac()
                result = None
            elif action == 'device':
                result = device_status()
            elif action == 'pair':
                check_mac()
                pair(data)
                result = None
            elif action == 'install':
                check_mac()
                if device_status() != 'paired':
                    raise RuntimeError('请先完成无线配对。')
                install_jobs()
                result = None
            elif action == 'verify':
                verify_services()
                result = None
            else:
                raise ValueError('未知步骤。')
        print(json.dumps({'ok': True, 'result': result}))
    except subprocess.CalledProcessError:
        print(json.dumps({'ok': False, 'error': '操作失败。请检查 USB 连接或登录启动权限后重试。'}))
    except (RuntimeError, ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
