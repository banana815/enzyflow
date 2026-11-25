"""Start one local control server and open the browser; never issues motion."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    'motor': ('webcontrol.server', 8765, '三轴控制台'),
    'spectrometer': ('webcontrol.spectrometer_server', 8766, '光谱仪控制台'),
}


def health(url):
    try:
        with urlopen(url+'api/health', timeout=0.6) as response:
            value = json.load(response)
        return value if value.get('app') == 'enzyflow-control' else None
    except Exception:
        return None


def validate_service(current, service, demo):
    if current and (current.get('service') != service or current.get('demo') != demo):
        raise RuntimeError('该端口已运行旧版控制台、其他设备服务或另一模拟模式；请先退出旧服务，或使用 --port 指定其他端口。')
    if current and service == 'motor' and current.get('version') != 11:
        raise RuntimeError('电机网页服务仍在运行旧版进程。请停止旧进程后重新运行 ./start-control.sh。')


def open_browser(url):
    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
    try:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        print('无法自动打开浏览器，请手动访问 '+url, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description='打开 Enzyflow 本机控制台')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--service', choices=SERVICES, default='motor')
    parser.add_argument('--port', type=int)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--handoff-thonny', action='store_true',
                        help='可选维护操作：尝试让已打开的 Thonny 释放串口；日常运行不需要')
    args = parser.parse_args()
    module, default_port, label = SERVICES[args.service]
    if args.port is None:
        args.port = default_port
    if args.handoff_thonny and args.service != 'motor':
        parser.error('--handoff-thonny 仅用于电机服务')
    if args.handoff_thonny and not sys.platform.startswith('linux'):
        parser.error('请先在 Thonny 中手动断开串口，再启动电机控制台。')
    if not 1024 <= args.port <= 65535:
        parser.error('端口必须为 1024–65535')
    url = 'http://127.0.0.1:%d/' % args.port
    runtime = ROOT/'webcontrol/runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    with open('/tmp/enzyflow-webcontrol-%d.lock' % args.port, 'a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        current = health(url)
        validate_service(current, args.service, args.demo)
        if not current:
            if args.handoff_thonny and not args.demo:
                from thonny_disconnect import handoff
                handoff()
            command = [sys.executable, '-m', module, '--port', str(args.port), '--no-browser']
            if args.demo:
                command.append('--demo')
            with (runtime/('server-%d.log' % args.port)).open('a') as log:
                process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
            (runtime/('server-%d.pid' % args.port)).write_text(str(process.pid))
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('控制服务启动失败，请查看 webcontrol/runtime/server-%d.log' % args.port)
                current = health(url)
                validate_service(current, args.service, args.demo)
                if current:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError('控制服务未就绪，请查看启动日志。')
    print('Enzyflow '+label+'：'+url, flush=True)
    if not args.no_browser:
        open_browser(url)


if __name__ == '__main__':
    main()
