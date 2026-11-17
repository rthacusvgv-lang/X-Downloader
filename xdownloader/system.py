"""Process, file, logging, lock and queue primitives shared by the whole program."""
import contextlib
import fcntl
import hashlib
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import tempfile
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
    _TERM_GRACE = 3      # seconds a stopped child may take to end on SIGTERM before it gets SIGKILL (see _end_groups)

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
            cls._end_groups([proc.pid for proc in cls._active_procs])

    @classmethod
    def clear_cancel(cls):
        cls._cancel.clear()

    @staticmethod
    def _signal_group(pgid: int, sig: int) -> bool:
        """Sends *sig* to the process group *pgid*; False if the group is gone."""
        try:
            os.killpg(pgid, sig)
            return True
        except OSError:
            return False

    @staticmethod
    def _ended(pid: int) -> bool:
        """True once *pid* has ended; a zombie that its parent has not collected yet counts as ended."""
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] in ("Z", "X", "x")
        except (OSError, IndexError):
            return True

    @classmethod
    def _end_groups(cls, pgids: list[int]) -> int:
        """Ends the process groups *pgids* (start_new_session=True makes each of our children lead its own group, so
        pgid == pid) and returns how many there were. SIGTERM first, SIGKILL for what still runs after _TERM_GRACE
        seconds: yt-dlp is a self-unpacking program that removes its unpacked copy (about 85 MiB in the temp folder)
        when it gets SIGTERM, but cannot on SIGKILL."""
        pgids = [pgid for pgid in pgids if cls._signal_group(pgid, signal.SIGTERM)]
        deadline = time.monotonic() + cls._TERM_GRACE
        while time.monotonic() < deadline and not all(cls._ended(pgid) for pgid in pgids):
            time.sleep(0.02)
        for pgid in pgids:
            cls._signal_group(pgid, signal.SIGKILL)         # whatever is left of the group
        return len(pgids)

    @staticmethod
    def tmpdir_prefix() -> str:
        """Prefix of the temporary folders of our children; the key of the project folder lets a start clean up its
        own leftovers only (see remove_leftover_tmpdirs), never those of a copy of the program running elsewhere."""
        return f"xdl-run-{hashlib.sha1(Config.BASE_PATH.encode('utf-8', 'surrogateescape')).hexdigest()[:10]}-"

    @classmethod
    @contextlib.contextmanager
    def _own_tmpdir(cls, env: dict[str, str]):
        """Gives the child a temporary folder of its own (TMPDIR in *env*), removed when the child has ended.

        yt-dlp unpacks itself (about 85 MiB) into the temporary folder, and stopped while it is still unpacking, it
        cannot remove that (measured: SIGTERM in its first ~0.4 s leaves 12 to 69 MiB). Without such a folder (none can
        be made) the child gets the usual one.
        """
        try:
            folder = tempfile.mkdtemp(prefix=cls.tmpdir_prefix(), dir=env.get("TMPDIR") or tempfile.gettempdir())
        except OSError:
            yield
            return
        env["TMPDIR"] = folder
        try:
            yield
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    @classmethod
    def remove_leftover_tmpdirs(cls):
        """Removes the temporary folders that children of an earlier instance kept when it died without kill_all()
        (SIGKILL, out of memory). Call only while holding the single-instance lock, after kill_orphans(). Never raises."""
        prefix = cls.tmpdir_prefix()
        try:
            entries = list(os.scandir(tempfile.gettempdir()))
        except OSError:
            return
        for entry in entries:
            try:
                if (entry.name.startswith(prefix) and entry.is_dir(follow_symlinks=False)
                        and entry.stat(follow_symlinks=False).st_uid == os.getuid()):
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                pass

    @classmethod
    def run(cls, cmd, timeout: float | None = None, **kwargs) -> subprocess.CompletedProcess:
        check = kwargs.pop('check', False)
        # Create a new process group so kill_all can terminate the entire tree
        kwargs['start_new_session'] = True
        env = kwargs['env'] = dict(kwargs['env']) if 'env' in kwargs else host_env()
        # One stray non-UTF-8 byte in a child's output must not raise out of communicate().
        if kwargs.get('text'):
            kwargs.setdefault('errors', 'replace')
        with cls._own_tmpdir(env):
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
                    cls._end_groups([proc.pid])
                    try:
                        out, err = proc.communicate(timeout=cls._DRAIN_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        # Pipes still held open by something that survived SIGKILL: give up
                        # rather than hang the worker thread forever.
                        out = err = None
                    raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
                # Ended by kill_all() / cancel(): not a result of the child, whatever it returned (a child may end
                # with 0 on SIGTERM).
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
            try:
                cls._end_groups([proc.pid for proc in cls._active_procs])
            except Exception:
                pass            # closing the program must not fail here
            cls._active_procs.clear()

    @classmethod
    def kill_orphans(cls, *exes: str) -> int:
        """Ends the process groups an earlier instance left behind when it died without kill_all()
        (SIGKILL, out-of-memory, crash): a surviving yt-dlp would write the same .part file as the new run.

        Our children are session leaders (start_new_session), so their session id equals their pid. A program
        shows up as argv[0], as argv[1] when a script runs through its interpreter, and as argv[3] when it runs
        through a dynamic loader (`ld.so --library-path DIR program`, see env.py): look at all of argv.
        Call only while holding the single-instance lock. Returns the number of groups ended.
        """
        groups = []
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
                groups.append(pid)
        return cls._end_groups(groups)

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
