#!/usr/bin/env python3
"""xu — single-command control for the Xu brain + web UI.

Cross-platform Python port of the original `xu` bash script: same commands,
messages, exit codes and env-var names.  This file is the ONE implementation —
the `xu` bash shim and `xu.cmd` Windows shim just exec it.

    xu.py start   build web (if needed) + launch brain daemon in background
    xu.py stop    kill the running brain daemon
    xu.py restart stop + start
    xu.py status  show running state + ports
    xu.py logs    tail the brain log
    xu.py test    run brain pytest + frontend svelte-check + web checks

Stdlib only.  Python >= 3.12.
"""

import ctypes
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
VENV = REPO / "brain" / ".venv"

# Windows process/liveness constants (the POSIX equivalents are signals).
_QUERY_LIMITED_INFORMATION = 0x1000   # OpenProcess access right
_STILL_ACTIVE = 259                   # GetExitCodeProcess sentinel
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

USAGE = """\
xu — personal AI harness

  xu start    build web + launch brain daemon
  xu stop     kill the brain daemon
  xu restart  stop + start
  xu status   show running state
  xu logs     tail brain logs
  xu test     run brain + frontend tests
  xu update [remote]   pull latest from remote (default origin), rebuild, restart

Environment:
  XU_BRAIN_PORT   websocket port  (default 9876)
  XU_WEB_PORT     http port        (default 1421)
  XU_DATA_HOME    data directory   (default ~/.xu)
  XU_KEY_<ID>    provider API key  (dev escape hatch)"""


# ---------------------------------------------------------------------------
# Platform branches — each isolated in a named function so tests can force
# either side.  is_windows() is the only gate; everything else flows from it.
# ---------------------------------------------------------------------------

def is_windows() -> bool:
    return os.name == "nt"


def venv_python() -> Path:
    """Interpreter inside the brain venv: bin/python vs Scripts/python.exe."""
    if is_windows():
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def find_tool(name: str) -> str | None:
    """Resolve an external tool to an absolute path (or None).

    shutil.which is PATHEXT-aware on Windows, so `npm`/`npx`/`bun` resolve
    to their .cmd shims there, and to plain binaries on POSIX.
    """
    return shutil.which(name)


def detach_kwargs() -> dict:
    """Popen kwargs so the child survives us and does not hold our console."""
    if is_windows():
        return {"creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate(pid: int, force: bool = False) -> None:
    """Graceful stop (SIGTERM / taskkill) or hard kill (SIGKILL / taskkill /F).

    Windows uses taskkill /T: the brain spawns children, so the whole process
    tree must go.  Never shell=True — argv lists only.
    """
    if is_windows():
        argv = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            argv.append("/F")
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # bash used `kill … 2>/dev/null || true`: a pid we may not signal
        # (recycled, or another user's) must not raise out of `xu stop`.
        pass


def _kernel32():
    """kernel32 with correct signatures — HANDLE is pointer-sized, so the
    default c_int restype would truncate it on 64-bit Windows."""
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.restype = ctypes.c_void_p
    dll.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    dll.GetExitCodeProcess.restype = ctypes.c_int
    dll.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    dll.CloseHandle.restype = ctypes.c_int
    dll.CloseHandle.argtypes = (ctypes.c_void_p,)
    return dll


def _win_alive(pid: int) -> bool:
    """Windows liveness probe.  NOT os.kill(pid, 0) — on Windows that call
    *terminates* the target process.  Query via OpenProcess + exit code."""
    k32 = _kernel32()
    handle = k32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.pointer(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def process_alive(pid: int | None) -> bool:
    """The `kill -0` equivalent behind the bash is_running()."""
    if not pid or pid <= 0:
        return False
    if is_windows():
        return _win_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def poll_ready(host: str, port: int | None, proc: subprocess.Popen | None = None,
               timeout: float = 5.0, interval: float = 0.1) -> bool:
    """Wait until the brain listens on TCP host:port.

    Replaces the bash `grep -q listening $LOG_FILE` loop (same 50x0.1s budget).
    Gives up early if the child already exited — the bash version could not.
    """
    deadline = time.monotonic() + timeout
    while True:
        if port is not None:
            try:
                with socket.create_connection((host, port), timeout=interval):
                    return True
            except (OSError, ValueError):
                pass
        if proc is not None and proc.poll() is not None:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Paths + shared helpers (env read at call time, like the bash vars).
# ---------------------------------------------------------------------------

def data_home() -> Path:
    return Path(os.environ.get("XU_DATA_HOME") or Path.home() / ".xu")


def pid_file() -> Path:
    return data_home() / "brain.pid"


def log_file() -> Path:
    return data_home() / "brain.log"


def brain_port() -> str:
    return os.environ.get("XU_BRAIN_PORT") or "9876"


def web_port() -> str:
    return os.environ.get("XU_WEB_PORT") or "1421"


def read_pid() -> int | None:
    try:
        return int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


def is_running() -> bool:
    return process_alive(read_pid())


def die(message: str):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def run_checked(argv: list[str], cwd: Path | None = None):
    """Run a command (argv list, never shell); exit with its status on
    failure — the `set -e` behaviour of the bash original."""
    cp = subprocess.run(argv, cwd=cwd)
    if cp.returncode != 0:
        raise SystemExit(cp.returncode)
    return cp


def tail_lines(path: Path, n: int = 20) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def ensure_venv() -> None:
    # Create the brain venv if missing. `uv sync` when available (fast, honours
    # uv.lock), else stdlib venv + `pip install -e .` off pyproject — so a fresh
    # clone works without uv installed. Both `start` and `test` need this, and
    # `test` needs the dev extra (pytest, pytest-asyncio, ruff), so install it:
    # plain `uv sync` would also *prune* those if they were already there.
    py = venv_python()
    if py.is_file() and os.access(py, os.X_OK):
        return
    print("xu: creating brain venv…")
    uv = find_tool("uv")
    if uv:
        run_checked([uv, "sync", "--extra", "dev"], cwd=REPO / "brain")
    else:
        py3 = os.environ.get("PYTHON3", "python3")
        if shutil.which(py3) is None:
            die("xu: no python3 on PATH — need Python >=3.12")
        cp = subprocess.run([py3, "-c", "import sys; sys.exit(sys.version_info < (3, 12))"])
        if cp.returncode != 0:
            ver = subprocess.run([py3, "-V"], capture_output=True, text=True)
            have = (ver.stdout + ver.stderr).strip()
            die(f"xu: need Python >=3.12 (have {have}) — install uv or a newer python3")
        run_checked([py3, "-m", "venv", str(VENV)])
        run_checked([str(venv_python()), "-m", "pip", "install", "-q", "-e", ".[dev]"],
                    cwd=REPO / "brain")
    if not (py.is_file() and os.access(py, os.X_OK)):
        die("xu: venv creation failed — see the output above")


def build_web_frontend() -> None:
    print("xu: building web frontend…")
    web = REPO / "web"
    bun = find_tool(os.environ.get("BUN", "bun"))
    if bun:
        first = subprocess.run([bun, "install", "--frozen-lockfile"], cwd=web,
                               stderr=subprocess.DEVNULL)
        if first.returncode != 0:
            run_checked([bun, "install"], cwd=web)
        run_checked([bun, "run", "build"], cwd=web)
        return
    npm = find_tool("npm")
    if npm:
        # package-lock.json is tracked and no npm script uses a shell-ism.
        run_checked([npm, "install", "--no-fund", "--no-audit"], cwd=web)
        run_checked([npm, "run", "build"], cwd=web)
        return
    die("xu: need bun or npm to build the web UI")


def brain_env() -> dict:
    """Child env for the brain process."""
    return os.environ.copy()


def cmd_start() -> None:
    if is_running():
        print(f"xu: already running (pid {read_pid()}) — try 'xu restart'")
        return

    ensure_venv()

    # Build the web frontend if dist/ is missing.
    web_dist = REPO / "web" / "dist"
    if not (web_dist / "index.html").is_file():
        build_web_frontend()

    print(f"xu: starting brain on :{brain_port()} (web :{web_port()})…")
    log = log_file()
    with log.open("wb") as lf:  # bash `> "$LOG_FILE"`: truncated on every start
        proc = subprocess.Popen(
            [str(venv_python()), "-m", "xu_brain",
             "--port", brain_port(),
             "--data-home", str(data_home()),
             "--web-dir", str(web_dist)],
            stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
            cwd=REPO, env=brain_env(), **detach_kwargs(),
        )
    pid_file().write_text(f"{proc.pid}\n")

    # Wait for readiness (TCP open beats grepping the log; abort if the
    # child already died).
    port = int(brain_port()) if brain_port().isdigit() else None
    if poll_ready("127.0.0.1", port, proc):
        print(f"xu: running (pid {proc.pid})")
        print(f"xu: web UI  → http://127.0.0.1:{web_port()}")
        print(f"xu: ws      → ws://127.0.0.1:{brain_port()}")
        print(f"xu: data    → {data_home()}")
        print(f"xu: logs    → {log_file()}")
        return
    print(f"xu: failed to start — check {log_file()}")
    for line in tail_lines(log_file()):
        print(line)
    raise SystemExit(1)


def cmd_stop() -> None:
    if not is_running():
        print("xu: not running")
        pid_file().unlink(missing_ok=True)  # drop a stale pid file
        return
    pid = read_pid()
    print(f"xu: stopping (pid {pid})…")
    terminate(pid)
    for _ in range(30):  # ~3s graceful window (30 x 0.1s, same as bash)
        if not process_alive(pid):
            break
        time.sleep(0.1)
    if process_alive(pid):
        terminate(pid, force=True)
    pid_file().unlink(missing_ok=True)
    print("xu: stopped")


def cmd_status() -> None:
    if is_running():
        print(f"xu: running (pid {read_pid()})")
        print(f"  ws   → ws://127.0.0.1:{brain_port()}")
        print(f"  web  → http://127.0.0.1:{web_port()}")
        print(f"  data → {data_home()}")
    else:
        print("xu: not running")


def cmd_logs() -> None:
    log = log_file()
    if not log.is_file():
        print("xu: no logs yet")
        return
    try:
        with log.open(encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            while True:  # follow like tail -f
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.1)
    except KeyboardInterrupt:
        pass  # Ctrl-C exits cleanly, like tail -f


def cmd_test() -> None:
    ensure_venv()
    brain = REPO / "brain"
    web = REPO / "web"
    print("=== brain pytest ===")
    run_checked([str(venv_python()), "-m", "pytest", "tests/", "-v"], cwd=brain)
    print("")
    print("=== svelte-check ===")
    npx = find_tool("npx")
    if npx is None:
        die("xu: npx not found on PATH")
    run_checked([npx, "svelte-check", "--tsconfig", "./tsconfig.json"], cwd=web)
    print("")
    print("=== web checks ===")
    npm = find_tool("npm")
    if npm is None:
        die("xu: npm not found on PATH")
    run_checked([npm, "test"], cwd=web)


def cmd_update(remote: str = "origin") -> None:
    """Pull the latest code from a git remote and rebuild what changed.

    Refuses on a dirty tree (local changes would collide with the pull) and
    only fast-forwards — a diverged history needs a human. Restarts the brain
    only if it was running before the update.
    """
    if shutil.which("git") is None:
        die("xu: git not found on PATH")
    cp = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                        capture_output=True, text=True)
    if cp.returncode != 0:
        die("xu: not a git checkout — 'update' needs a clone of the repo")
    if cp.stdout.strip():
        die("xu: working tree has uncommitted changes — commit or stash first")
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()
    if not branch or branch == "HEAD":
        die("xu: detached HEAD — check out a branch before updating")

    run_checked(["git", "fetch", remote], cwd=REPO)
    behind = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..{remote}/{branch}"],
        cwd=REPO, capture_output=True, text=True)
    if behind.returncode == 0 and behind.stdout.strip() == "0":
        print(f"xu: already up to date ({remote}/{branch})")
        return

    was_running = is_running()
    if was_running:
        cmd_stop()
    run_checked(["git", "pull", "--ff-only", remote, branch], cwd=REPO)
    print("xu: refreshed code — syncing deps + rebuilding web…")
    ensure_venv()
    uv = find_tool("uv")
    if uv:
        run_checked([uv, "sync", "--extra", "dev"], cwd=REPO / "brain")
    else:
        run_checked([str(venv_python()), "-m", "pip", "install", "-q", "-e", ".[dev]"],
                    cwd=REPO / "brain")
    build_web_frontend()
    if was_running:
        cmd_start()
    else:
        print("xu: updated — start with 'xu start'")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    data_home().mkdir(parents=True, exist_ok=True)

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": lambda: (cmd_stop(), cmd_start()),
        "status": cmd_status,
        "logs": cmd_logs,
        "test": cmd_test,
        "update": lambda: cmd_update(args[1] if len(args) > 1 else "origin"),
    }
    fn = commands.get(args[0]) if args else None
    if fn is None:
        print(USAGE)
        return 1
    fn()
    return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
