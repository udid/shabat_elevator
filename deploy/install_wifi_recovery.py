"""Run as root after FILES is injected; snapshot code/cron before changes."""
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

SERVICE = 'shabat-wifi-recovery.service'
TIMER = 'shabat-wifi-recovery.timer'
LIB = Path('/usr/local/lib/shabat-wifi-recovery')
UNITS = Path('/etc/systemd/system')

if 'FILES' not in globals():
    source_dir = Path(__file__).resolve().parent
    test_source = (source_dir.parent/'tests/test_wifi_recovery.py').read_text(encoding='utf-8')
    if 'from deploy import wifi_recovery as w' not in test_source:
        raise RuntimeError('Unexpected test import; verify tests before installation')
    FILES = {
        'watchdog.py': base64.b64encode((source_dir/'wifi_recovery.py').read_bytes()).decode(),
        'test_watchdog.py': base64.b64encode(test_source.replace('from deploy import wifi_recovery as w', 'import watchdog as w').encode()).decode(),
        SERVICE: base64.b64encode((source_dir/SERVICE).read_bytes()).decode(),
        TIMER: base64.b64encode((source_dir/TIMER).read_bytes()).decode(),
    }


def run(args, *, data=None, timeout=30, check=True):
    p = subprocess.run(args, input=data, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, 'LC_ALL':'C', 'PYTHONDONTWRITEBYTECODE':'1'})
    if check and p.returncode:
        raise RuntimeError(f'{args[0:3]} failed ({p.returncode}): {p.stderr[-2000:]}')
    return p


def atomic(path, content, mode=0o644):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


assert os.geteuid() == 0
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
snapshot = Path('/var/backups/shabat-wifi-recovery') / stamp
snapshot.mkdir(parents=True, mode=0o700)
os.chmod(snapshot, 0o700)
original_cron = run(['crontab','-u','root','-l'], check=False)
if original_cron.returncode not in (0,1):
    raise RuntimeError('Cannot read original root crontab')
old_cron = original_cron.stdout
atomic(snapshot/'root-crontab.txt', old_cron.encode(), 0o600)
cron_lines=old_cron.splitlines(keepends=True)
removed=[x.strip() for x in cron_lines if x.strip() == '1 0 * * * /sbin/reboot']
new_cron=''.join(x for x in cron_lines if x.strip() != '1 0 * * * /sbin/reboot')
if len(removed) > 1:
    raise RuntimeError('Unexpected duplicate reboot entries; no change applied')
if not removed and not (LIB/'watchdog.py').exists():
    raise RuntimeError('Expected nightly reboot line is missing; inspect before installing')
was_enabled=run(['systemctl','is-enabled',TIMER],check=False).returncode == 0
was_active=run(['systemctl','is-active',TIMER],check=False).returncode == 0
mapping={'watchdog.py': LIB/'watchdog.py', SERVICE:UNITS/SERVICE, TIMER:UNITS/TIMER}
prior={}
prior_modes={}
for name,path in mapping.items():
    if path.is_symlink():
        raise RuntimeError(f'Refusing symlink target {path}')
    if path.exists():
        prior[name]=path.read_bytes()
        prior_modes[name]=path.stat().st_mode & 0o777
        atomic(snapshot/name,prior[name],0o600)
atomic(snapshot/'manifest.json',json.dumps({'timerEnabled':was_enabled,'timerActive':was_active,'existingFiles':list(prior),'fileModes':prior_modes,'removedCron':removed},indent=2).encode(),0o600)
stage=snapshot/'validation'
stage.mkdir(mode=0o700)
for name in ('watchdog.py','test_watchdog.py'):
    atomic(stage/name,base64.b64decode(FILES[name]),0o600)
test=run(['/usr/bin/python3','-m','unittest','discover','-s',str(stage),'-p','test_watchdog.py','-v'],timeout=60)
cron_changed=False
try:
    if was_active:
        run(['systemctl','stop',TIMER])
    run(['systemctl','stop',SERVICE],check=False)
    LIB.mkdir(mode=0o755,parents=True,exist_ok=True)
    for name,path in mapping.items():
        atomic(path,base64.b64decode(FILES[name]))
    run(['systemd-analyze','verify',str(UNITS/SERVICE),str(UNITS/TIMER)])
    before=run(['/usr/bin/python3',str(LIB/'watchdog.py'),'--check-only'])
    if json.loads(before.stdout).get('status') != 'healthy':
        raise RuntimeError('Local Wi-Fi is not healthy before enablement')
    run(['systemctl','daemon-reload'])
    run(['systemctl','start',SERVICE],timeout=100)
    status=run(['systemctl','show',SERVICE,'--property=Result,ExecMainStatus']).stdout
    if 'Result=success' not in status or 'ExecMainStatus=0' not in status:
        raise RuntimeError(f'Initial service check failed: {status}')
    live_state=json.loads(Path('/var/lib/shabat-wifi-recovery/state.json').read_text())
    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if (live_state.get('healthy_since') is None or live_state.get('outage_since') is not None
            or live_state.get('boot_id') != boot_id
            or not 0 <= time.monotonic() - live_state.get('last_observed',-1000) < 60):
        raise RuntimeError('Sandboxed service did not confirm healthy local Wi-Fi')
    run(['systemctl','enable','--now',TIMER])
    run(['systemctl','is-enabled',TIMER])
    run(['systemctl','is-active',TIMER])
    current_cron=run(['crontab','-u','root','-l'],check=False).stdout
    if current_cron != old_cron:
        raise RuntimeError('Root crontab changed during install; refusing overwrite')
    if removed:
        cron_changed=True  # Include timeout after a committed write in rollback.
        run(['crontab','-u','root','-'],data=new_cron)
    if run(['crontab','-u','root','-l'],check=False).stdout != new_cron:
        raise RuntimeError('Crontab verification failed')
    summary={'snapshot':str(snapshot),'removedCron':removed,'timerEnabled':True,'timerActive':True,
             'tests':(test.stdout+test.stderr)[-12000:],'initialCheck':before.stdout,'service':status,
             'sha256':{name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in mapping.items()}}
    atomic(snapshot/'result.json',json.dumps(summary,indent=2).encode(),0o600)
    print(json.dumps(summary))
except Exception:
    run(['systemctl','disable','--now',TIMER],check=False)
    run(['systemctl','stop',SERVICE],check=False)
    if cron_changed:
        run(['crontab','-u','root','-'],data=old_cron)
    for name,path in mapping.items():
        if name in prior:
            atomic(path,prior[name],prior_modes[name])
        elif path.exists():
            path.unlink()
    run(['systemctl','daemon-reload'],check=False)
    if was_enabled:
        run(['systemctl','enable',TIMER],check=False)
    if was_active:
        run(['systemctl','start',TIMER],check=False)
    raise
