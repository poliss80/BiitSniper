import os
import sys
import time
import subprocess
import logging
import threading
import atexit
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MAIN_SCRIPT = BASE_DIR / "main.py"
VENV_DIR = BASE_DIR / "apextrader"
LEGACY_VENV_DIR = BASE_DIR / ".venv"
REQUIREMENTS_FILE = BASE_DIR / "requirements.txt"
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "autobot.log"
PID_FILE = BASE_DIR / "autobot.pid"
BOOTSTRAP_MARKER = VENV_DIR / ".bootstrapped"
RESTART_BACKOFF_SECONDS = [5, 10, 20, 30, 60]


class AutoBotWatchdog:
    def __init__(self) -> None:
        self.logger = self._setup_logging()

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("autobot")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
            fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
            fh.setFormatter(formatter)
            logger.addHandler(fh)
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(formatter)
            logger.addHandler(sh)
        return logger

    def _run_command(self, command: list[str]) -> None:
        subprocess.run(command, check=True)

    def _parse_env_file(self) -> dict[str, str]:
        env = {}
        if not ENV_FILE.exists():
            return env
        for raw_line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
        return env

    def _ensure_virtualenv(self) -> Path:
        target = VENV_DIR if VENV_DIR.exists() else LEGACY_VENV_DIR
        if not target.exists():
            self.logger.info("Creating virtualenv at %s", VENV_DIR)
            self._run_command([sys.executable, "-m", "venv", str(VENV_DIR)])
            target = VENV_DIR

        python_exe = target / "Scripts" / "python.exe"
        if not python_exe.exists():
            raise RuntimeError(f"Unable to locate virtualenv Python at {python_exe}")

        if not BOOTSTRAP_MARKER.exists() and target == VENV_DIR:
            self.logger.info("Bootstrapping requirements into apextrader venv")
            self._run_command([str(python_exe), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
            if REQUIREMENTS_FILE.exists():
                self._run_command([str(python_exe), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)])
            BOOTSTRAP_MARKER.write_text("bootstrapped\n", encoding="utf-8")

        return python_exe

    def _write_pid(self) -> None:
        try:
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
            atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
        except Exception as exc:
            self.logger.warning("Unable to write PID file: %s", exc)

    def kill_existing_project_processes(self) -> None:
        try:
            import psutil
        except ImportError:
            return

        me = os.getpid()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            if proc.info["pid"] == me:
                continue
            try:
                cmdline = proc.info["cmdline"] or []
                joined = " ".join(str(x) for x in cmdline)
                if str(MAIN_SCRIPT) in joined or str(Path(__file__)) in joined:
                    self.logger.info("Killing duplicate process %s %s", proc.info["pid"], joined)
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _drain_subprocess_output(self, process: subprocess.Popen) -> threading.Thread:
        def _drain() -> None:
            try:
                if process.stdout is None:
                    return
                for line in process.stdout:
                    self.logger.info("[main] %s", line.rstrip())
            except Exception:
                pass

        thread = threading.Thread(target=_drain, daemon=True)
        thread.start()
        return thread

    def get_current_trade_mode(self) -> str:
        return os.environ.get("TRADE_MODE", os.getenv("TRADE_MODE", "paper")).strip().lower() or "paper"

    def run(self) -> None:
        self.logger.info("Starting AutoBot watchdog")
        self._write_pid()
        python_exe = self._ensure_virtualenv()
        last_env_mtime = ENV_FILE.stat().st_mtime if ENV_FILE.exists() else None
        restart_count = 0

        self.kill_existing_project_processes()

        while True:
            env = os.environ.copy()
            env.update(self._parse_env_file())
            # Only override TRADE_MODE from the watchdog's OS env if it was explicitly
            # set there (e.g. $env:TRADE_MODE="live" in the shell).
            # If not set in OS env, leave whatever _parse_env_file() read from .env.
            if "TRADE_MODE" in os.environ:
                env["TRADE_MODE"] = os.environ["TRADE_MODE"].strip().lower()

            mode = env["TRADE_MODE"]
            self.logger.info("Launching main.py in %s mode", mode.upper())

            process = subprocess.Popen(
                [str(python_exe), str(MAIN_SCRIPT)],
                cwd=str(BASE_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            start_time = time.monotonic()
            self._drain_subprocess_output(process)

            while process.poll() is None:
                time.sleep(2)
                current_mtime = ENV_FILE.stat().st_mtime if ENV_FILE.exists() else None
                if current_mtime != last_env_mtime:
                    self.logger.info("Detected .env change, restarting main.py")
                    last_env_mtime = current_mtime
                    process.terminate()
                    break

            runtime = time.monotonic() - start_time
            exit_code = process.poll()
            self.logger.warning("main.py exited with code %s after %.1f seconds", exit_code, runtime)

            if runtime < 30:
                restart_count += 1
            else:
                restart_count = 0

            backoff = RESTART_BACKOFF_SECONDS[min(restart_count, len(RESTART_BACKOFF_SECONDS) - 1)]
            self.logger.info("Restarting in %s seconds", backoff)
            time.sleep(backoff)
