"""Auto-detects installed browsers and extracts the login cookies of a Site."""
import contextlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .sites import Site
from .system import ProcessManager, ShutdownRequested, atomic_write_lines

_SNAPSHOT_TRIES = 3   # attempts to get a consistent copy of a live browser database
_SNAPSHOT_PREFIX = "xdl-cookies-"
# Seconds. A copy of a database lives for a second or two, and the copy of Firefox's login that cookie_args() makes
# for yt-dlp as long as one run of it: one instance runs one at a time, so an older copy was left by a killed run. Only
# a second instance of the program from another project folder can meet a run of over 10 minutes; yt-dlp has read its
# copy at the start by then, and saving its cookie jar back at the end fails, so that download is simply tried again.
_SNAPSHOT_MAX_AGE = 600


def _xdg_config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def _snapshot_folder() -> str:
    """Where copies of browser cookie databases go. A copy holds every cookie of the browser (in plain text for Firefox),
    so prefer $XDG_RUNTIME_DIR: in memory, only this user can enter it, emptied at logout. Otherwise the temp folder,
    which may be on disk, or the project folder's .tmp/ when /tmp is noexec (see the launch script)."""
    run = os.environ.get("XDG_RUNTIME_DIR")
    return run if run and os.path.isdir(run) else tempfile.gettempdir()


def _remove_stale_snapshots(folder: str):
    """Removes the copies that runs killed while holding one (SIGKILL, power loss) left in *folder*. Never raises."""
    now = time.time()
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.name.startswith(_SNAPSHOT_PREFIX) or not entry.is_dir(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            if st.st_uid == os.getuid() and now - st.st_mtime > _SNAPSHOT_MAX_AGE:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            pass


# SQL for the current time in seconds since 1970, and for the expiry of a Firefox cookie in the same unit: Firefox 142
# (database schema 16) stores milliseconds, earlier versions seconds (yt-dlp reads it the same way). 0 or NULL: none.
_NOW_SQL = "CAST(strftime('%s', 'now') AS INTEGER)"
_FIREFOX_EXPIRY_SQL = "(expiry / CASE WHEN (SELECT user_version FROM pragma_user_version) >= 16 THEN 1000 ELSE 1 END)"


def _host_sql(column: str, domains: tuple[str, ...]) -> str:
    """SQL condition matching cookie hosts of *domains* (and their subdomains) exactly.

    A plain LIKE '%x.com%' would also match unrelated sites such as netflix.com,
    dropbox.com or box.com and leak their session cookies into cookies.txt.
    """
    exact = ", ".join(f"'{d}', '.{d}'" for d in domains)
    subs = " OR ".join(f"{column} LIKE '%.{d}'" for d in domains)
    return f"({column} IN ({exact}) OR {subs})"


class BrowserCookieManager:
    """Auto-detects system browsers and extracts the login cookies of one *site*."""

    _CHROMIUM_DIRS = {
        "chrome":   "google-chrome",
        "chromium": "chromium",
        "brave":    os.path.join("BraveSoftware", "Brave-Browser"),
        "opera":    "opera",
        "edge":     "microsoft-edge",
        "vivaldi":  "vivaldi",
    }
    _CHROMIUM_FLATPAK_IDS = {
        "chrome":   "com.google.Chrome",
        "chromium": "org.chromium.Chromium",
        "brave":    "com.brave.Browser",
        "opera":    "com.opera.Opera",
        "edge":     "com.microsoft.Edge",
        "vivaldi":  "com.vivaldi.Vivaldi",
    }

    @classmethod
    def _browser_roots(cls, browser: str) -> list[str]:
        home = os.path.expanduser("~")
        cfg = _xdg_config_home()
        if browser == "firefox":
            return [
                os.path.join(cfg, "mozilla", "firefox"),          # Firefox 147+ (XDG layout)
                os.path.join(home, ".mozilla", "firefox"),        # classic layout
                os.path.join(home, "snap", "firefox", "common", ".mozilla", "firefox"),
                os.path.join(home, ".var", "app", "org.mozilla.firefox", ".mozilla", "firefox"),
                os.path.join(home, ".var", "app", "org.mozilla.firefox", "config", "mozilla", "firefox"),
            ]
        sub = cls._CHROMIUM_DIRS.get(browser)
        if not sub:
            return []
        roots = [os.path.join(cfg, sub)]
        if browser == "chromium":
            roots.append(os.path.join(home, "snap", "chromium", "common", "chromium"))
        if browser == "brave":
            roots.append(os.path.join(home, "snap", "brave", "current", ".config", sub))
        flatpak_id = cls._CHROMIUM_FLATPAK_IDS.get(browser)
        if flatpak_id:
            roots.append(os.path.join(home, ".var", "app", flatpak_id, "config", sub))
        return roots

    def __init__(self, logger: logging.Logger, site: Site):
        self._logger = logger
        self.site = site
        self._detected_browser: str | None = None
        self._detected_profile: str | None = None
        self._cookie_args: list[str] | None = None

    @property
    def cookies_file(self) -> str:
        return os.path.join(Config.BASE_PATH, self.site.cookies_name)

    # -- helpers --
    @staticmethod
    def _newest_first(paths) -> list[Path]:
        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        return sorted(set(paths), key=mtime, reverse=True)

    def _cookie_dbs(self, browser: str) -> list[Path]:
        dbs = []
        for root in self._browser_roots(browser):
            if not os.path.isdir(root):
                continue
            r = Path(root)
            if browser == "firefox":
                patterns = ("*/cookies.sqlite",)
            else:
                patterns = ("Cookies", "Network/Cookies", "*/Cookies", "*/Network/Cookies")
            for pattern in patterns:
                dbs.extend(p for p in r.glob(pattern) if p.is_file())
        return self._newest_first(dbs)

    @staticmethod
    def _profile_dir(browser: str, db_path: Path) -> str:
        parent = db_path.parent
        if browser != "firefox" and parent.name == "Network":
            parent = parent.parent
        return str(parent)

    @staticmethod
    def _query_copy(db_path: Path, sql: str) -> list[tuple]:
        """Runs *sql* on a private copy of the (possibly locked) browser database.

        The db and its -wal cannot be copied atomically; if the browser checkpoints in
        between, the pair is inconsistent ("malformed"), so take the snapshot again.
        """
        folder = _snapshot_folder()
        _remove_stale_snapshots(folder)
        for attempt in range(1, _SNAPSHOT_TRIES + 1):
            with tempfile.TemporaryDirectory(prefix=_SNAPSHOT_PREFIX, dir=folder) as tmpdir:
                tmp_db = os.path.join(tmpdir, "cookies.db")
                shutil.copyfile(str(db_path), tmp_db)
                # Recently written cookies may still live in the write-ahead log.
                wal = str(db_path) + "-wal"
                if os.path.exists(wal):
                    shutil.copyfile(wal, tmp_db + "-wal")
                conn = sqlite3.connect(tmp_db)
                try:
                    return conn.execute(sql).fetchall()
                except sqlite3.DatabaseError as e:
                    if "malformed" not in str(e) or attempt == _SNAPSHOT_TRIES:
                        raise
                finally:
                    conn.close()
            time.sleep(0.2)
        return []  # unreachable: the last attempt returns or raises

    def _db_has_login(self, browser: str, db_path: Path) -> bool:
        """True if *db_path* holds the site's login cookie and it has not expired: a browser nobody has opened since its
        login ran out still keeps the cookie, and must not win over a valid cookies file or another browser."""
        if browser == "firefox":
            table, column = "moz_cookies", "host"
            unexpired = f"(expiry IS NULL OR expiry = 0 OR {_FIREFOX_EXPIRY_SQL} > {_NOW_SQL})"
        else:
            # Cookie names are stored unencrypted in Chromium databases. expires_utc counts microseconds since
            # 1601-01-01 (11644473600 s before 1970); 0 is a session cookie.
            table, column = "cookies", "host_key"
            unexpired = f"(expires_utc = 0 OR expires_utc > ({_NOW_SQL} + 11644473600) * 1000000)"
        sql = (f"SELECT COUNT(*) FROM {table} "
               f"WHERE {_host_sql(column, self.site.cookie_domains)} "
               f"AND name = '{self.site.login_cookie}' AND {unexpired}")
        try:
            return self._query_copy(db_path, sql)[0][0] > 0
        except (sqlite3.Error, OSError, IndexError) as e:
            self._logger.debug("Cannot inspect %s: %s", db_path, e)
            return False

    def _installed_browsers(self) -> list[str]:
        return [b for b in Config.SUPPORTED_BROWSERS
                if any(os.path.isdir(r) for r in self._browser_roots(b))]

    def detect_browser(self) -> str | None:
        """Returns the first browser (and remembers the profile) holding a login cookie."""
        if self._detected_browser:
            return self._detected_browser

        for browser in self._installed_browsers():
            for db_path in self._cookie_dbs(browser):
                if self._db_has_login(browser, db_path):
                    self._detected_browser = browser
                    self._detected_profile = self._profile_dir(browser, db_path)
                    self._logger.info(
                        "🔍 Detected browser logged into %s: %s (%s)",
                        self.site.name, browser, self._detected_profile
                    )
                    return browser

        self._logger.warning("⚠️ No browser logged into %s was detected.", self.site.name)
        return None

    @staticmethod
    def _browser_spec(browser: str, profile: str | None) -> str:
        # yt-dlp syntax BROWSER[:PROFILE]; an explicit profile path makes yt-dlp read the
        # profile that actually holds the login instead of the most recently used one.
        return f"{browser}:{profile}" if profile else browser

    def _read_firefox_cookies(self, db_path: Path) -> list[tuple]:
        return self._query_copy(
            db_path,
            "SELECT host, "
            "CASE WHEN host LIKE '.%' THEN 'TRUE' ELSE 'FALSE' END, "
            "path, "
            "CASE WHEN isSecure THEN 'TRUE' ELSE 'FALSE' END, "
            f"COALESCE({_FIREFOX_EXPIRY_SQL}, 0), name, value "       # a cookies file counts seconds
            f"FROM moz_cookies WHERE {_host_sql('host', self.site.cookie_domains)}"
        )

    def _export_firefox_cookies(self, db_path: Path, path: str) -> bool:
        """Writes the site's cookies of the Firefox database *db_path* to *path* (Netscape format, 0600).

        False if they hold no login cookie, or cannot be read or written.
        """
        try:
            rows = self._read_firefox_cookies(db_path)
        except (sqlite3.Error, OSError) as e:
            self._logger.debug("Failed to read Firefox cookies: %s", e)
            return False
        if self.site.login_cookie not in {row[5] for row in rows}:
            return False

        lines = [
            "# Netscape HTTP Cookie File",
            f"# Auto-exported from Firefox - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        for row in rows:
            fields = [str(c) for c in row]
            # Tabs / newlines inside a field would corrupt the Netscape format.
            if any(ch in fld for fld in fields for ch in "\t\r\n"):
                continue
            lines.append('\t'.join(fields))
        try:
            # 0600: the file contains the account session token.
            atomic_write_lines(path, lines, mode=0o600)
        except OSError as e:
            self._logger.warning("⚠️ Cannot write %s: %s", path, e)
            return False
        return True

    def get_cookie_args(self) -> list[str]:
        """Returns the yt-dlp cookie arguments resolved by ensure_cookies()."""
        if self._cookie_args is None:
            self.ensure_cookies()
        return list(self._cookie_args or [])

    @contextlib.contextmanager
    def cookie_args(self):
        """Yields the yt-dlp cookie arguments for one run of yt-dlp: those of get_cookie_args(), except for Firefox.

        yt-dlp reads Firefox's cookies.sqlite but not its write-ahead log, where a login of the last minutes still is.
        So for Firefox they name a copy of the site's cookies made right now from both, in a private folder that is
        deleted when the run ends: the copy holds the session token. (The project's cookies file is the user's and is
        never written.) Without a copy, yt-dlp reads Firefox itself.
        """
        args = self.get_cookie_args()
        if self._detected_browser != "firefox" or not self.is_using_browser("firefox"):
            yield args
            return
        try:
            private = tempfile.TemporaryDirectory(prefix=_SNAPSHOT_PREFIX, dir=_snapshot_folder(),
                                                  ignore_cleanup_errors=True)
        except OSError as e:
            self._logger.warning("⚠️ Cannot write %s: %s", _snapshot_folder(), e)
            yield args
            return
        with private as folder:
            path = os.path.join(folder, "cookies.txt")
            exported = self._export_firefox_cookies(Path(self._detected_profile) / "cookies.sqlite", path)
            yield ["--cookies", path] if exported else args

    def is_using_browser(self, browser: str) -> bool:
        args = self._cookie_args or []
        return (len(args) == 2 and args[0] == "--cookies-from-browser"
                and args[1].split(":", 1)[0] == browser)

    def ensure_cookies(self) -> bool:
        """Resolves the cookie source once; every download then uses the same, verified source."""
        browser = self.detect_browser()

        if browser:
            spec = self._browser_spec(browser, self._detected_profile)
            # Firefox cookies are plain sqlite: no probe needed, cookie_args() copies them for every run of yt-dlp.
            # Chromium's are encrypted: only yt-dlp can read them.
            if browser == "firefox" or self._test_browser_cookies(spec):
                self._logger.info(
                    "✅ Browser cookies available (%s), will be used automatically", browser
                )
                self._cookie_args = ["--cookies-from-browser", spec]
                return True
            self._logger.warning(
                "⚠️ yt-dlp failed to extract cookies from %s, trying fallback...", browser
            )

        if self._check_cookies_file():
            self._cookie_args = ["--cookies", self.cookies_file]
            return True

        # Last resort: an installed browser whose login could not be verified
        # (e.g. unknown database layout). Use it only if yt-dlp can read it.
        for other in self._installed_browsers():
            if other == browser:
                continue
            if self._test_browser_cookies(other):
                self._logger.info("Using unverified cookies from %s.", other)
                self._cookie_args = ["--cookies-from-browser", other]
                return False

        self._logger.warning(
            "⚠️ Cannot acquire cookies, will attempt download as guest (success rate is extremely low)."
        )
        self._logger.warning(
            "   It is recommended to log into %s in your browser and try again.", self.site.name
        )
        self._cookie_args = []
        return False

    def _test_browser_cookies(self, spec: str) -> bool:
        try:
            result = ProcessManager.run(
                [Config.YT_DLP_EXE, "--ignore-config", "--cookies-from-browser", spec,
                 "--simulate", "--skip-download",
                 "--", self.site.probe_url],
                timeout=Config.COOKIE_TEST_TIMEOUT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            output = (result.stdout or "") + (result.stderr or "")
            m = re.search(r"Extracted (\d+) cookies from", output)
            return bool(m and int(m.group(1)) > 0)
        except ShutdownRequested:
            raise
        except Exception as e:
            self._logger.debug("Cookie extraction test failed for %s: %s", spec, e)
            return False

    def _check_cookies_file(self) -> bool:
        name, path = self.site.cookies_name, self.cookies_file
        if not os.path.exists(path):
            self._logger.warning("⚠️ %s not found, will attempt download as guest.", name)
            return False

        # The file holds a session token: make sure other users cannot read it.
        try:
            if os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass

        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as e:
            self._logger.warning("⚠️ Cannot read %s: %s", name, e)
            return False

        missing = [c for c in self.site.required_cookies if f"\t{c}\t" not in content]
        if missing:
            self._logger.warning(
                "⚠️ %s is missing key login cookies: %s", name, ", ".join(missing)
            )
            self._logger.warning(
                "   %s restricts media access for guests. Most videos will fail to download.",
                self.site.name
            )
            self._logger.warning(
                "   Please log into the site in your browser first; the downloader will automatically read the browser cookies."
            )
            return False

        self._logger.info("✅ %s authentication is valid (%s)",
                          name, " + ".join(self.site.required_cookies))
        return True
