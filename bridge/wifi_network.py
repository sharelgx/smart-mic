"""Pick a physical macOS LAN interface, never a VPN/tunnel/default UDP route."""
import ipaddress
import re
import subprocess


def lan_address():
    ports=subprocess.run(['/usr/sbin/networksetup','-listallhardwareports'],capture_output=True,text=True,check=True).stdout
    candidates=[]
    for block in ports.split('\n\n'):
        name=re.search(r'Hardware Port: (.+)',block)
        device=re.search(r'Device: (en\d+)\b',block)
        if not name or not device:
            continue
        # Prefer Wi-Fi because this device joins the selected wireless LAN.
        priority=0 if name.group(1)=='Wi-Fi' else 1
        candidates.append((priority,device.group(1)))
    for _,device in sorted(candidates):
        state=subprocess.run(['/sbin/ifconfig',device],capture_output=True,text=True,check=True).stdout
        if 'status: active' not in state:
            continue
        for value in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+)\b',state):
            address=ipaddress.IPv4Address(value)
            if any(address in ipaddress.IPv4Network(cidr) for cidr in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
                return str(address)
    raise RuntimeError('未发现活动的局域网网卡。请让 Mac 连接设备使用的路由器，或显式指定 --bind。')
