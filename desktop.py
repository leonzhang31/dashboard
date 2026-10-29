"""Local Mac launcher. Run with --stop to stop the background dashboard."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.desktop'
STATE = RUNTIME / 'server.json'


def running_server():
    try:
        state = json.loads(STATE.read_text())
        with urlopen(f"http://127.0.0.1:{int(state['port'])}/_desktop/health", timeout=1) as response:
            health = json.load(response)
        return state if health.get('token') == state['token'] else None
    except (OSError, ValueError, KeyError):
        return None


def serve():
    from app import app
    from werkzeug.serving import make_server

    token = secrets.token_hex(24)

    @app.get('/_desktop/health')
    def desktop_health():
        return {'token': token}

    server = make_server('127.0.0.1', 0, app, threaded=True)
    STATE.write_text(json.dumps({'pid': os.getpid(), 'port': server.server_port, 'token': token}))
    os.chmod(STATE, 0o600)
    server.serve_forever()


def launch(stop=False):
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    with (RUNTIME / 'launch.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = running_server()
        if stop:
            if state:
                os.kill(int(state['pid']), signal.SIGTERM)
                STATE.unlink(missing_ok=True)
            return
        if state is None:
            with (RUNTIME / 'server.log').open('a') as log:
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), '--serve'],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,
                )
            for _ in range(100):
                state = running_server()
                if state:
                    break
                if process.poll() is not None:
                    raise RuntimeError(f'Dashboard could not start. See {RUNTIME / "server.log"}')
                time.sleep(.1)
            if state is None:
                process.terminate()
                raise RuntimeError('Dashboard startup timed out. Try opening it again.')
        subprocess.run(['open', '-a', 'Google Chrome', f"http://127.0.0.1:{state['port']}/"], check=True)


if __name__ == '__main__':
    if '--serve' in sys.argv:
        serve()
    else:
        launch(stop='--stop' in sys.argv)
