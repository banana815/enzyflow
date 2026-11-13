"""Use Thonny's Python: board_tool.py backup DEST | exec CODEFILE | deploy DIR."""
import os, sys, signal, time, subprocess, json, hashlib, fcntl
from pathlib import Path
import serial
from thonny.common import ImmediateCommand, serialize_message


def main():
    # One maintenance operation at a time, before touching the Thonny backend.
    lock = open('/tmp/enzyflow-board-tool.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pids = subprocess.check_output(['pgrep','-f','[e]sp_back.py'], text=True).split()
    if len(pids) != 1:
        raise RuntimeError('Expected exactly one Thonny ESP backend')
    pid = int(pids[0])
    fd = os.open('/proc/%d/fd/0' % pid, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, (serialize_message(ImmediateCommand('interrupt'))+'\n').encode())
    finally:
        os.close(fd)
    time.sleep(0.7)
    os.kill(pid, signal.SIGSTOP)
    s = None
    try:
        s = serial.Serial('/dev/ttyACM0',115200,timeout=0.1)
        s.write(b'\x03\x03\x01'); time.sleep(.3); s.read_all()
        def run(code, timeout=25):
            data=code.encode()
            for n in range(0,len(data),128):
                s.write(data[n:n+128]); time.sleep(.005)
            s.write(b'\x04')
            out=b''; end=time.monotonic()+timeout
            while time.monotonic()<end:
                out+=s.read(4096)
                if out.endswith(b'\x04>'):
                    break
            else:
                s.write(b'\x03'); time.sleep(.3); s.read_all()
                raise TimeoutError(out[-500:])
            if not out.startswith(b'OK'):
                raise RuntimeError(repr(out))
            stdout, stderr, _=out[2:].split(b'\x04',2)
            if stderr:
                raise RuntimeError(stderr.decode(errors='replace'))
            return stdout.decode()
        action, target=sys.argv[1:3]
        if action=='backup':
            root=Path(target);root.mkdir(parents=True,exist_ok=False)
            listing=run("import os,json\ndef walk(p):\n for n in os.listdir(p):\n  q=p.rstrip('/')+'/'+n\n  if os.stat(q)[0]&0x4000:\n   walk(q)\n  else:\n   print(json.dumps([q,os.stat(q)[6]]))\nwalk('/')")
            manifest={}
            for line in listing.splitlines():
                path,size=json.loads(line)
                dest=root/path.lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True)
                data=bytearray()
                for offset in range(0,size,1024):
                    result=run("import binascii\nwith open(%r,'rb') as f:\n f.seek(%d)\n print(binascii.hexlify(f.read(1024)).decode())"%(path,offset))
                    data.extend(bytes.fromhex(result.strip()))
                if len(data)!=size:raise RuntimeError('Backup size mismatch')
                dest.write_bytes(data);manifest[path]=hashlib.sha256(data).hexdigest()
            (root/'manifest.json').write_text(json.dumps(manifest,indent=2))
            print('BACKUP_OK',listing)
        elif action=='exec':
            print(run(Path(target).read_text()))
        elif action=='deploy':
            root=Path(target)
            files=sorted(root.rglob('*.py'),key=lambda p:(p.name=='main.py',str(p)))
            for src in files:
                if '__pycache__' in src.parts:continue
                path='/'+src.relative_to(root).as_posix();data=src.read_bytes()
                directory=path.rsplit('/',1)[0] or '/'
                run("import os\np=''\nfor part in %r.split('/'):\n if part:\n  p+='/'+part\n  if part not in os.listdir(p.rsplit('/',1)[0] or '/'):\n   os.mkdir(p)\nf=open(%r,'wb')\nf.close()"%(directory,path+'.tmp'))
                for offset in range(0,len(data),768):
                    run("with open(%r,'ab') as f:\n f.write(%r)"%(path+'.tmp',data[offset:offset+768]))
                expected=hashlib.sha256(data).hexdigest()
                run("import hashlib,binascii,os\nwith open(%r,'rb') as f:\n digest=binascii.hexlify(hashlib.sha256(f.read()).digest()).decode()\nassert digest==%r\nos.rename(%r,%r)\nos.sync()"%(path+'.tmp',expected,path+'.tmp',path))
                print('VERIFIED',path,expected[:12],flush=True)
        else:raise ValueError(action)
    finally:
        try:
            if s:
                try:
                    s.write(b'\x02')  # return to friendly REPL before handing back
                finally:
                    s.close()
        finally:
            try:
                os.kill(pid,signal.SIGCONT)
            except ProcessLookupError:
                pass

if __name__=='__main__': main()
