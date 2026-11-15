"""Process, file, logging, lock and queue primitives shared by the whole program."""
import fcntl
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time

from .config import Config
from .fsutil import atomic_write_text


def host_env() -> dict[str, str]:
    """os.environ for a program of the host (yt-dlp, ffmpeg, zenity, xdg-open), without what bin/python of the bundled
    runtime set for itself: that program, and whatever it starts, must use the host's CA list, which gets updates, not
    the copy frozen into the bundle. A value the user set (any other path) is kept.
    """
    bundled = {     # exactly what tools/build-runtime.sh writes into bin/python
        "SSL_CERT_FILE": os.path.join(sys.prefix, "etc", "cacert.pem"),
        "TCL_LIBRARY": os.path.join(sys.prefix, "lib", "tcl8.6"),
        "TK_LIBRARY": os.path.join(sys.prefix, "lib", "tk8.6"),
        "FONTCONFIG_FILE": os.path.join(sys.prefix, "etc", "fonts.conf"),
    }
    return {k: v for k, v in os.environ.items() if bundled.get(k) != v}


# --- ProcessManager ---
class ShutdownRequested(Exception):
    """Raised when the application is closing (kill_all() was called)."""


class CancelRequested(ShutdownRequested):
    """Raised when the user pressed Stop (cancel() was called). Unlike a shutdown, the program keeps running."""


class ProcessManager:
    """Manages all child processes to ensure safe termination when the GUI closes."""
    _active_procs = []
    _lock = threading.Lock()
    _shutdown = threading.Event()
    _cancel = threading.Event()
    _DRAIN_TIMEOUT = 5   # seconds to wait for the pipes to close after SIGKILL

    @classmethod
    def unregister(cls, proc: subprocess.Popen):
        with cls._lock:
            if proc in cls._active_procs:
                cls._active_procs.remove(proc)

    @classmethod
    def is_shutting_down(cls) -> bool:
        return cls._shutdown.is_set()

    @classmethod
    def wait(cls, seconds: float) -> bool:
        """Interruptible sleep. Returns True if a shutdown or a cancel was requested meanwhile."""
        deadline = time.monotonic() + seconds
        while not (cls._shutdown.is_set() or cls._cancel.is_set()):
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            cls._shutdown.wait(min(left, 0.1))
        return True

    @classmethod
    def raise_if_stopped(cls):
        if cls._shutdown.is_set():
            raise ShutdownRequested()
        if cls._cancel.is_set():
            raise CancelRequested()

    @classmethod
    def cancel(cls):
        """Stops the running child processes and makes run()/wait() raise CancelRequested until clear_cancel()."""
        with cls._lock:
            cls._cancel.set()
            for proc in cls._active_procs:
                cls._kill_group(proc)

    @classmethod
    def clear_cancel(cls):
        cls._cancel.clear()

    @classmethod
    def _kill_group(cls, proc: subprocess.Popen):
        try:
            # start_new_session=True makes the child its own group leader (pgid == pid).
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.kill()
        except OSError:
            pass

    @classmethod
    def run(cls, cmd, timeout: float | None = None, **kwargs) -> subprocess.CompletedProcess:
        check = kwargs.pop('check', False)
        # Create a new process group so kill_all can terminate the entire tree
        kwargs['start_new_session'] = True
        kwargs.setdefault('env', host_env())
        # One stray non-UTF-8 byte in a child's output must not raise out of communicate().
        if kwargs.get('text'):
            kwargs.setdefault('errors', 'replace')
        # Popen + register must be atomic with respect to kill_all(); otherwise a process
        # started during shutdown could escape and keep running after the GUI exits.
        with cls._lock:
            cls.raise_if_stopped()
            proc = subprocess.Popen(cmd, **kwargs)
            cls._active_procs.append(proc)
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                cls._kill_group(proc)
                try:
                    out, err = proc.communicate(timeout=cls._DRAIN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    # Pipes still held open by something that survived SIGKILL: give up
                    # rather than hang the worker thread forever.
                    out = err = None
                raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
            if proc.returncode != 0:
                # Killed by kill_all() / cancel(): not a real download failure.
                cls.raise_if_stopped()
            if check and proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd, output=out, stderr=err)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        finally:
            cls.unregister(proc)

    @classmethod
    def kill_all(cls):
        with cls._lock:
            cls._shutdown.set()
            for proc in cls._active_procs:
                try:
                    cls._kill_group(proc)
                except Exception:
                    pass
            cls._active_procs.clear()

    @classmethod
    def kill_orphans(cls, *exes: str) -> int:
        """Kills the process groups an earlier instance left behind when it died without kill_all()
        (SIGKILL, out-of-memory, crash): a surviving yt-dlp would write the same .part file as the new run.

        Our children are session leaders (start_new_session), so their session id equals their pid. A program
        shows up as argv[0], as argv[1] when a script runs through its interpreter, and as argv[3] when it runs
        through a dynamic loader (`ld.so --library-path DIR program`, see env.py): look at all of argv.
        Call only while holding the single-instance lock. Returns the number of groups killed.
        """
        killed = 0
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            pid = int(entry.name)
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
                with open(f"/proc/{pid}/stat") as f:
                    session = int(f.read().rsplit(")", 1)[1].split()[3])
            except (OSError, ValueError, IndexError):
                continue
            if session == pid and any(a.decode(errors="replace") in exes for a in argv):
                try:
                    os.killpg(pid, signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
        return killed

# --- File helpers ---

def atomic_write_lines(path: str, lines: list[str], mode: int | None = None):
    """Writes one *lines* entry per line to *path* (atomically, see fsutil.atomic_write_text)."""
    atomic_write_text(path, '\n'.join(lines) + ('\n' if lines else ''), mode)

def read_lines(path: str) -> list[str]:
    """Returns the non-empty, stripped lines of *path* ([] if the file does not exist).

    Undecodable bytes (a note saved in Big5 / UTF-16 by another editor) become U+FFFD instead of raising:
    URLs are ASCII, and one bad byte must not make the whole queue unreadable.
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


# --- setup_logger ---
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("XDownloader")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="{asctime} [{levelname:^8}] {message}",
        datefmt="%Y-%m-%d %H:%M:%S",
        style="{"
    )

    # Rotate so the log file cannot grow without bound.
    fh = logging.handlers.RotatingFileHandler(
        Config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8'
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# --- LockManager ---
class LockManager:
    """Exclusive lock to ensure single instance."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._lock_file = None

    def acquire(self) -> bool:
        """False if another instance holds the lock. Raises OSError if the lock file cannot be used at all."""
        if self._lock_file is not None:
            try:
                if os.fstat(self._lock_file.fileno()).st_ino == os.stat(Config.LOCK_FILE).st_ino:
                    return True
            except OSError:
                pass
            self.release()
        for _ in range(5):
            f = None
            try:
                f = open(Config.LOCK_FILE, 'a+')
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:                 # flock(LOCK_NB): held by another process
                if f is not None:
                    f.close()
                self._logger.warning("Another instance is running. Failed to acquire lock.")
                return False
            except OSError as e:
                # No other instance: the file cannot be opened or locked at all (e.g. created by a root run,
                # read-only folder). This used to be reported as "already running" too.
                if f is not None:
                    f.close()
                self._logger.error("Cannot use the lock file %s: %s", Config.LOCK_FILE, e)
                raise
            # The previous owner may have unlinked the path between our open() and flock();
            # we would then hold a lock on an orphaned inode. Verify and retry.
            try:
                same = os.fstat(f.fileno()).st_ino == os.stat(Config.LOCK_FILE).st_ino
            except OSError:
                same = False
            if not same:
                f.close()
                continue
            try:
                f.seek(0)
                f.truncate()
                f.write(f"{os.getpid()}\n")
                f.flush()
            except OSError:
                pass
            self._lock_file = f
            return True
        self._logger.warning("Failed to acquire lock (lock file kept changing).")
        return False

    def release(self):
        if self._lock_file:
            # Unlink while still holding the lock, then close (which releases the flock).
            try:
                os.remove(Config.LOCK_FILE)
            except OSError:
                pass
            self._lock_file.close()
            self._lock_file = None


# --- QueueManager ---
class QueueManager:
    """Manages atomic reads/writes to .processing.list."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def load_unfinished(self) -> list[str]:
        urls = read_lines(Config.PROCESSING_FILE)
        if urls:
            self._logger.info(
                "Detected %d unfinished download task(s); restored to the queue.", len(urls)
            )
        return urls

    def save(self, urls: list[str]):
        atomic_write_lines(Config.PROCESSING_FILE, urls)
        self._logger.debug("Updated .processing.list (%d item(s))", len(urls))

    def clear(self):
        try:
            os.remove(Config.PROCESSING_FILE)
        except OSError:
            pass



def merge_unique(*lists: list[str]) -> list[str]:
    """Concatenates lists, keeping the first occurrence of every line."""
    result, seen = [], set()
    for lst in lists:
        for item in lst:
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                result.append(item)
    return result
