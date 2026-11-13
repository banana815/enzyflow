"""Release this project's ESP32 port through Thonny's own Disconnect menu.

Never kills/suspends the frontend, edits its config, or discards editor files.
Only runs when exactly one ESP backend owns the requested port.
"""
import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys


def backend_for_port(port):
    found = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc/'cmdline').read_bytes().decode().split('\0')
            for index, value in enumerate(argv):
                if value.endswith('/thonny/plugins/esp/esp_back.py'):
                    config = ast.literal_eval(argv[index+1])
                    if config.get('port') == port:
                        parent = next(line.split()[1] for line in (proc/'status').read_text().splitlines()
                                      if line.startswith('PPid:'))
                        found.append(int(parent))
        except (OSError, ValueError, SyntaxError, IndexError, StopIteration):
            continue
    return found


def invoke_menu():
    import tkinter as tk
    window = tk.Tk()
    window.withdraw()
    try:
        apps = [name for name in window.tk.splitlist(window.tk.call('winfo', 'interps'))
                if str(name).lower().startswith('thonny')]
        if len(apps) != 1:
            raise RuntimeError('需要恰好一个 Thonny 窗口；请在 Thonny 中手动选择“断开”。')
        app = apps[0]
        def query(*args):
            return window.tk.call('send', app, *args)
        count = int(query('.!menu', 'index', 'end'))
        run_menu = None
        for index in range(count+1):
            try:
                if query('.!menu', 'entrycget', index, '-label') in ('运行', 'Run'):
                    run_menu = query('.!menu', 'entrycget', index, '-menu')
                    break
            except tk.TclError:
                pass
        if not run_menu:
            raise RuntimeError('找不到 Thonny 运行菜单；请手动选择“断开”。')
        for index in range(int(query(run_menu, 'index', 'end'))+1):
            try:
                label = query(run_menu, 'entrycget', index, '-label')
            except tk.TclError:
                continue
            if label in ('断开', 'Disconnect'):
                query(run_menu, 'invoke', index)
                print('Thonny 已释放串口，编辑器保持打开。')
                return
        raise RuntimeError('找不到 Thonny 断开菜单。')
    finally:
        window.destroy()


def handoff(port='/dev/ttyACM0'):
    parents = backend_for_port(port)
    if not parents:
        return True
    if len(parents) != 1:
        print('存在多个 Thonny ESP 后端，请手动断开后再连接网页。')
        return False
    env = os.environ.copy()
    try:
        for item in Path('/proc/%d/environ' % parents[0]).read_bytes().split(b'\0'):
            key, separator, value = item.partition(b'=')
            if separator and key.decode() in ('DISPLAY', 'XAUTHORITY', 'LD_LIBRARY_PATH', 'XDG_RUNTIME_DIR'):
                env[key.decode()] = value.decode()
        completed = subprocess.run([sys.executable, __file__, '--invoke'], env=env,
                                   capture_output=True, text=True, timeout=8)
        print(completed.stdout.strip() or completed.stderr.strip())
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        print('Thonny 自动交接未完成，请手动选择“断开”：%s' % exc)
        return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--invoke', action='store_true')
    parser.add_argument('--port', default='/dev/ttyACM0')
    args = parser.parse_args()
    if args.invoke:
        invoke_menu()
    else:
        raise SystemExit(0 if handoff(args.port) else 1)
