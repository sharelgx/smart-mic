"""Configure a Smart Mic over its USB serial port."""
import getpass
import json
import time


def provision(port, host, listen_port, key, *, ssid=None, password=None):
    import serial
    if ssid is None:
        ssid = input('2.4GHz Wi-Fi 名称：').strip()
    if password is None:
        password = getpass.getpass('Wi-Fi 密码（不显示，仅保存到设备）：')
    if not 1 <= len(ssid.encode()) <= 32 or len(password.encode()) > 63:
        raise ValueError('Wi-Fi 名称或密码长度不合法')
    config = {'ssid':ssid, 'password':password, 'host':host,
              'port':listen_port, 'key':key.hex()}
    wire = b'@PVM1 CONFIG ' + json.dumps(config, ensure_ascii=False).encode() + b'\n'
    config.clear()
    password = None
    with serial.Serial(port, 115200, timeout=.25, write_timeout=3, exclusive=True) as board:
        board.write(wire)
        wire = b''
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = board.readline()
            if b'@PVM1 CONFIGURED' in line:
                print('已通过 USB 保存设备连接配置；Mac 仅保存配对密钥，Wi-Fi 密码不在 Mac 落盘。', flush=True)
                return
        raise RuntimeError('设备未确认配置，请确认已烧录 Wi-Fi 麦克风固件且设备已开机')


def update_host(port, host, listen_port):
    import serial
    wire=b'@PVM1 HOST '+json.dumps({'host':host,'port':listen_port}).encode()+b'\n'
    with serial.Serial(port,115200,timeout=.25,write_timeout=3,exclusive=True) as board:
        board.write(wire)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if b'@PVM1 HOST_UPDATED' in board.readline():
                print('已更新 Mac 局域网地址，保留设备 Wi-Fi 密码和配对密钥。',flush=True)
                return
    raise RuntimeError('设备未确认地址更新；请核对固件版本和 USB 连接')
