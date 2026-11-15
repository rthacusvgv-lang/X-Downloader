"""
Shared test infrastructure for X-Downloader.

Importing this module:
  * points HOME / XDG_CONFIG_HOME at a throw-away sandbox, so the tests can never read the
    real browser profiles of the user, and
  * registers an atexit hook that deletes the sandbox.

Every test additionally runs in its own temp "base" directory (see Base.setUp), so the real
project files (list.txt, cookies.txt, logs, yt-dlp, ffmpeg, ...) are never touched.
"""
import atexit
import logging
import logging.handlers  # noqa: F401  (used via logging.handlers.*)
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

SANDBOX = tempfile.mkdtemp(prefix="xdl_test_")
SANDBOX_HOME = os.path.join(SANDBOX, "home")
SANDBOX_CONFIG = os.path.join(SANDBOX_HOME, ".config")
os.makedirs(SANDBOX_CONFIG)
os.environ["HOME"] = SANDBOX_HOME
os.environ["XDG_CONFIG_HOME"] = SANDBOX_CONFIG
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)

from xdownloader import sites  # noqa: E402,F401  (re-exported for the test modules)
from xdownloader.config import Config  # noqa: E402
from xdownloader.cookies import BrowserCookieManager  # noqa: E402,F401
from xdownloader.core import DownloaderCore, DownloadThread  # noqa: E402,F401
from xdownloader.env import EnvManager  # noqa: E402,F401
from xdownloader.system import (  # noqa: E402,F401
    LockManager, ProcessManager, QueueManager, ShutdownRequested,
    atomic_write_lines, merge_unique, read_lines, setup_logger,
)

# Without any handler Python prints WARNING+ records to stderr; those are expected here.
logging.getLogger("XDownloader").addHandler(logging.NullHandler())

REAL_YTDLP = os.path.join(PROJECT, "yt-dlp")

FAKE_YTDLP = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Fake yt-dlp: behaviour is selected by a word in the URL.
    echo "$@" >> "$(dirname "$0")/calls.log"
    url="${@: -1}"
    case "$url" in
      *ok*)       echo "[download] done"; exit 0 ;;
      *gone*)     echo "ERROR: [twitter] This tweet is unavailable" >&2; exit 1 ;;
      *flaky*)    echo "ERROR: HTTP Error 500" >&2; exit 1 ;;
      *slow*)     sleep 30; exit 0 ;;
      *)          exit 0 ;;
    esac
    """) + "# padding " + "x" * 2048 + "\n"   # > 1 KiB so EnvManager treats it as a real binary

# `dns` hosts fail like a dead network, `gone` hosts like a deleted tweet, `slow` hosts never finish in time.
BY_HOST = """\
    echo "$@" >> "$(dirname "$0")/calls.log"
    case "${@: -1}" in
      *dns*)  echo "ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>" >&2; exit 1 ;;
      *gone*) echo "ERROR: [twitter] This tweet is unavailable" >&2; exit 1 ;;
      *slow*) sleep 30 ;;
    esac"""


def write_fake_ytdlp(body: str):
    """Replaces the fake yt-dlp with a script running *body* (padded past the 1 KiB 'real binary' check)."""
    with open(Config.YT_DLP_EXE, "w") as f:
        f.write("#!/usr/bin/env bash\n" + textwrap.dedent(body) + "\n# padding " + "x" * 2048 + "\n")
    os.chmod(Config.YT_DLP_EXE, 0o755)


def point_config_to(base):
    """Redirects every path in Config to *base* and disables real network downloads."""
    Config.BASE_PATH = base
    Config.DOWNLOAD_DIR = base
    Config.MISSING_DOWNLOAD_DIR = None
    Config.YT_DLP_EXE = os.path.join(base, "yt-dlp")
    Config.FFMPEG_EXE = os.path.join(base, "ffmpeg")
    Config.LIST_FILE = os.path.join(base, "list.txt")
    Config.FAILED_FILE = os.path.join(base, "failed_list.txt")
    Config.LOCK_FILE = os.path.join(base, "downloader.lock")
    Config.LOG_FILE = os.path.join(base, "downloader.log")
    Config.PROCESSING_FILE = os.path.join(base, ".processing.list")
    Config.SETTINGS_FILE = os.path.join(base, "settings.json")
    Config.FFMPEG_ARCHIVE = os.path.join(base, "_ffmpeg_temp.tar.xz")
    Config.YT_DLP_URL = "http://127.0.0.1:9/yt-dlp"        # unreachable -> fast failure
    Config.FFMPEG_URL = "http://127.0.0.1:9/ffmpeg.tar.xz"
    Config.HTTP_TIMEOUT = 3
    Config.MAX_RETRIES = 3
    Config.MERGE_FORMAT = "mp4"
    Config.LANGUAGE = "en"


def cookies_path():
    """Where the (redirected) cookies fallback file of the X site lives."""
    return os.path.join(Config.BASE_PATH, sites.X.cookies_name)


def git_ignores(path: str) -> bool | None:
    """Whether .gitignore covers *path*, judged by its patterns alone (also for a file that is already tracked).

    None when this is not a git checkout (e.g. a release tarball) or git is missing.
    """
    try:
        inside = subprocess.run(["git", "-C", PROJECT, "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True)
    except OSError:
        return None
    if inside.returncode != 0:
        return None
    return subprocess.run(["git", "-C", PROJECT, "check-ignore", "-q", "--no-index", "--", path]).returncode == 0


def reset_shutdown():
    """ProcessManager remembers kill_all() for the whole process; clear it between tests."""
    ProcessManager._shutdown.clear()


def process_alive(pid):
    """True while *pid* is running.

    A zombie (Z, waiting to be reaped) is dead. So is state X: for an instant between "zombie" and "gone",
    while init reaps the process, /proc reports X. Counting only Z as dead made a test fail about once in
    250 kills although the process was gone.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X", "x")
    except (FileNotFoundError, ProcessLookupError):
        return False


def make_logger():
    lg = logging.getLogger("XDownloaderTest")
    lg.handlers.clear()
    lg.addHandler(logging.NullHandler())
    lg.setLevel(logging.DEBUG)
    return lg


def install_fake_tools(base=None):
    """Writes a fake yt-dlp and a fake ffmpeg into Config's (already redirected) paths."""
    with open(Config.YT_DLP_EXE, "w") as f:
        f.write(FAKE_YTDLP)
    os.chmod(Config.YT_DLP_EXE, 0o755)
    with open(Config.FFMPEG_EXE, "wb") as f:
        f.write(b"\0" * 4096)
    os.chmod(Config.FFMPEG_EXE, 0o755)


def read_calls(base):
    """Lines the fake yt-dlp recorded (one per invocation)."""
    try:
        with open(os.path.join(base, "calls.log")) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


class Base(unittest.TestCase):
    def setUp(self):
        reset_shutdown()
        self.base = tempfile.mkdtemp(prefix="base_", dir=SANDBOX)
        point_config_to(self.base)
        self.log = make_logger()

    def install_fake_tools(self):
        install_fake_tools()

    def calls(self):
        return read_calls(self.base)


def make_firefox_profile(root, name, cookies):
    """Creates <root>/<name>/cookies.sqlite with (name, value, host) rows; returns the profile dir."""
    prof = os.path.join(root, name)
    os.makedirs(prof, exist_ok=True)
    db = sqlite3.connect(os.path.join(prof, "cookies.sqlite"))
    db.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL DEFAULT '', "
               "name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER, "
               "creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0, "
               "sameSite INTEGER DEFAULT 0, schemeMap INTEGER DEFAULT 0)")
    for n, v, h in cookies:
        db.execute("INSERT INTO moz_cookies (name,value,host,path,expiry,isSecure,isHttpOnly) "
                   "VALUES (?,?,?,'/',9999999999,1,1)", (n, v, h))
    db.commit()
    db.close()
    return prof


def make_chromium_cookies(path, cookies):
    """Creates a Chromium-style Cookies database with (name, host_key) rows."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, path TEXT)")
    for n, h in cookies:
        db.execute("INSERT INTO cookies VALUES (?,?,x'00','/')", (h, n))
    db.commit()
    db.close()
