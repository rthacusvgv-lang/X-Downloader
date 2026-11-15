"""cookies.py: finding the browser profile (or the cookies file) that holds the login."""
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from common import (
    Base, BrowserCookieManager, Config, DownloaderCore, REAL_YTDLP, SANDBOX, SANDBOX_CONFIG, SANDBOX_HOME, cookies_path,
    make_chromium_cookies, make_firefox_profile, sites,
)


class TestCookies(Base):
    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp(prefix="home_", dir=SANDBOX)
        os.environ["HOME"] = self.home
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.home, ".config")

    def tearDown(self):
        os.environ["HOME"] = SANDBOX_HOME
        os.environ["XDG_CONFIG_HOME"] = SANDBOX_CONFIG

    def test_no_false_positive_from_lookalike_hosts(self):
        # Old query (LIKE '%x.com%') matched netflix.com / box.com -> wrong browser + leaked cookies
        make_firefox_profile(os.path.join(self.home, ".mozilla", "firefox"), "p.default",
                             [("auth_token", "BOX", ".box.com"), ("sess", "N", ".netflix.com")])
        m = BrowserCookieManager(self.log, sites.X)
        self.assertIsNone(m.detect_browser())
        self.assertFalse(m.export_firefox_cookies())

    def test_firefox_xdg_detect_and_export(self):
        root = os.path.join(self.home, ".config", "mozilla", "firefox")   # Firefox 147+ layout
        make_firefox_profile(root, "old.default", [("sess", "N", ".netflix.com")])
        prof = make_firefox_profile(root, "main.default-release", [
            ("auth_token", "AAA", ".x.com"), ("ct0", "BBB", ".x.com"),
            ("guest_id", "G", "twitter.com"), ("sess", "NETFLIX", ".netflix.com"),
            ("auth_token", "BOX", ".box.com"), ("k", "SUB", "api.x.com"),
        ])
        m = BrowserCookieManager(self.log, sites.X)
        self.assertEqual(m.detect_browser(), "firefox")
        self.assertEqual(m._detected_profile, prof)
        self.assertTrue(m.export_firefox_cookies())
        with open(cookies_path(), encoding="utf-8") as f:
            content = f.read()
        self.assertTrue(content.startswith("# Netscape HTTP Cookie File"))
        self.assertIn("\tauth_token\tAAA", content)
        self.assertIn("\tct0\tBBB", content)
        self.assertIn("twitter.com\tFALSE", content)
        self.assertIn("api.x.com", content)
        self.assertNotIn("NETFLIX", content)
        self.assertNotIn("BOX", content)
        self.assertEqual(stat.S_IMODE(os.stat(cookies_path()).st_mode), 0o600)
        # the exported file is accepted by the validator and loadable as a cookie jar
        self.assertTrue(m._check_cookies_file())
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(cookies_path())
        jar.load(ignore_expires=True)
        self.assertEqual({c.name for c in jar}, {"auth_token", "ct0", "guest_id", "k"})

    def test_firefox_wal_is_read(self):
        root = os.path.join(self.home, ".mozilla", "firefox")
        prof = os.path.join(root, "w.default")
        os.makedirs(prof)
        db = sqlite3.connect(os.path.join(prof, "cookies.sqlite"))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, name TEXT, value TEXT, host TEXT, "
                   "path TEXT, expiry INTEGER, isSecure INTEGER)")
        db.commit()
        db.execute("INSERT INTO moz_cookies (name,value,host,path,expiry,isSecure) "
                   "VALUES ('auth_token','W','.x.com','/',0,1)")
        db.commit()  # stays in -wal while the connection is open (like a running Firefox)
        try:
            self.assertTrue(os.path.getsize(os.path.join(prof, "cookies.sqlite-wal")) > 0)
            m = BrowserCookieManager(self.log, sites.X)
            self.assertEqual(m.detect_browser(), "firefox")
        finally:
            db.close()

    def test_inconsistent_snapshot_is_retried(self):
        # The browser may checkpoint between our copy of the db and of its -wal: the copy is then
        # "malformed" and used to be reported as "no login cookie" (silent false negative).
        root = os.path.join(self.home, ".mozilla", "firefox")
        prof = make_firefox_profile(root, "m.default",
                                    [("auth_token", "A", ".x.com")] +
                                    [(f"n{i}", "v", f"h{i}.example.com") for i in range(3000)])
        real_copy, calls = shutil.copyfile, []

        def torn_copy(src, dst, **kw):
            real_copy(src, dst, **kw)
            calls.append(src)
            if len(calls) == 1:                 # The first snapshot is intentionally truncated (torn).
                with open(dst, "r+b") as f:
                    f.truncate(8192)
        with mock.patch.object(shutil, "copyfile", torn_copy):
            m = BrowserCookieManager(self.log, sites.X)
            self.assertEqual(m.detect_browser(), "firefox")
        self.assertEqual(len(calls), 2)
        self.assertEqual(m._detected_profile, prof)

    def test_persistently_malformed_snapshot_gives_up(self):
        root = os.path.join(self.home, ".mozilla", "firefox")
        make_firefox_profile(root, "m.default", [(f"n{i}", "v", f"h{i}.example.com") for i in range(3000)])
        real_copy = shutil.copyfile

        def torn_copy(src, dst, **kw):
            real_copy(src, dst, **kw)
            with open(dst, "r+b") as f:
                f.truncate(8192)
        with mock.patch.object(shutil, "copyfile", torn_copy):
            self.assertIsNone(BrowserCookieManager(self.log, sites.X).detect_browser())

    def test_null_expiry_is_exported_as_session_cookie(self):
        root = os.path.join(self.home, ".mozilla", "firefox")
        prof = make_firefox_profile(root, "n.default", [("auth_token", "A", ".x.com"), ("ct0", "B", ".x.com")])
        db = sqlite3.connect(os.path.join(prof, "cookies.sqlite"))
        db.execute("UPDATE moz_cookies SET expiry = NULL WHERE name = 'ct0'")
        db.commit()
        db.close()
        self.assertTrue(BrowserCookieManager(self.log, sites.X).export_firefox_cookies())
        with open(cookies_path()) as f:
            content = f.read()
        self.assertNotIn("None", content)
        self.assertIn("\t0\tct0\tB", content)

    def test_chromium_profile_detection(self):
        cfg = os.path.join(self.home, ".config")
        # Chrome installed but not logged in, Brave logged in on "Profile 2"
        make_chromium_cookies(os.path.join(cfg, "google-chrome", "Default", "Network", "Cookies"),
                              [("sess", ".netflix.com")])
        make_chromium_cookies(os.path.join(cfg, "BraveSoftware", "Brave-Browser", "Profile 2", "Network", "Cookies"),
                              [("auth_token", ".x.com"), ("ct0", ".x.com")])
        m = BrowserCookieManager(self.log, sites.X)
        self.assertEqual(m.detect_browser(), "brave")
        self.assertEqual(m._detected_profile,
                         os.path.join(cfg, "BraveSoftware", "Brave-Browser", "Profile 2"))
        self.assertEqual(m._browser_spec("brave", m._detected_profile),
                         "brave:" + m._detected_profile)

        # Snap Chromium layout (Ubuntu default)
        shutil.rmtree(os.path.join(cfg, "BraveSoftware"))
        snap_prof = os.path.join(self.home, "snap", "chromium", "common", "chromium", "Default")
        make_chromium_cookies(os.path.join(snap_prof, "Network", "Cookies"),
                              [("auth_token", ".x.com"), ("ct0", ".x.com")])
        m2 = BrowserCookieManager(self.log, sites.X)
        self.assertEqual(m2.detect_browser(), "chromium")
        self.assertEqual(m2._detected_profile, snap_prof)

    @unittest.skipUnless(os.path.exists(REAL_YTDLP), "real yt-dlp not present")
    def test_ensure_cookies_with_real_ytdlp(self):
        shutil.copy(REAL_YTDLP, Config.YT_DLP_EXE)
        prof = make_firefox_profile(os.path.join(self.home, ".config", "mozilla", "firefox"), "x.default",
                                    [("auth_token", "AAA", ".x.com"), ("ct0", "BBB", ".x.com")])
        m = BrowserCookieManager(self.log, sites.X)
        self.assertTrue(m.ensure_cookies())
        self.assertEqual(m.get_cookie_args(), ["--cookies-from-browser", "firefox:" + prof])
        self.assertTrue(m.is_using_browser("firefox"))
        # DownloaderCore must use exactly the resolved args
        cmd = DownloaderCore(self.log, {sites.X.name: m})._build_cmd("https://x.com/a/status/1", sites.X)
        self.assertIn("firefox:" + prof, cmd)

    def test_ensure_cookies_prefers_valid_cookies_txt_when_browser_unusable(self):
        # Old bug: get_cookie_args() returned --cookies-from-browser even after ensure_cookies()
        # had found it unusable and fallen back to cookies.txt.
        with open(Config.YT_DLP_EXE, "w") as f:
            f.write("#!/bin/sh\necho 'ERROR: failed to decrypt' >&2\nexit 1\n")
        os.chmod(Config.YT_DLP_EXE, 0o755)
        make_chromium_cookies(os.path.join(self.home, ".config", "chromium", "Default", "Cookies"),
                              [("auth_token", ".x.com")])
        with open(cookies_path(), "w") as f:
            f.write("# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tA\n"
                    ".x.com\tTRUE\t/\tTRUE\t0\tct0\tB\n")
        os.chmod(cookies_path(), 0o664)
        m = BrowserCookieManager(self.log, sites.X)
        self.assertTrue(m.ensure_cookies())
        self.assertEqual(m.get_cookie_args(), ["--cookies", cookies_path()])
        self.assertEqual(stat.S_IMODE(os.stat(cookies_path()).st_mode), 0o600)

    def test_guest_mode(self):
        self.install_fake_tools()
        m = BrowserCookieManager(self.log, sites.X)
        self.assertFalse(m.ensure_cookies())
        self.assertEqual(m.get_cookie_args(), [])

    def test_firefox_skips_redundant_probe(self):
        self.install_fake_tools()
        prof = make_firefox_profile(os.path.join(self.home, ".mozilla", "firefox"), "f.default",
                                    [("auth_token", "AAA", ".x.com"), ("ct0", "BBB", ".x.com")])
        m = BrowserCookieManager(self.log, sites.X)
        with mock.patch.object(m, "_test_browser_cookies") as m_probe:
            self.assertTrue(m.ensure_cookies())
            self.assertFalse(m_probe.called)
        self.assertEqual(m.get_cookie_args(), ["--cookies-from-browser", "firefox:" + prof])


class TestCookieSnapshotsLeaveNothingBehind(Base):
    """To read a browser's cookie database the program copies all of it (every site's cookies, in plain text for
    Firefox) into the temp folder, which is the project folder's .tmp/ when /tmp is noexec. A run killed while it held
    the copy (SIGKILL, power loss) left the whole cookie jar there for good."""

    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.base, "cookies.sqlite")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (x)")
        conn.close()
        self.run_dir = os.path.join(self.base, "run")
        os.mkdir(self.run_dir, 0o700)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.run_dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def leftover(self, folder: str, name: str, age: float) -> str:
        path = os.path.join(folder, name)
        os.mkdir(path)
        with open(os.path.join(path, "cookies.db"), "w") as f:
            f.write("every cookie of the browser")
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def query(self):
        return BrowserCookieManager._query_copy(Path(self.db), "SELECT COUNT(*) FROM t")

    def test_the_copy_is_made_in_memory_for_this_user_only(self):
        # $XDG_RUNTIME_DIR: a tmpfs only its user can enter, emptied at logout; never the disk or the project folder.
        with mock.patch("xdownloader.cookies.sqlite3.connect", wraps=sqlite3.connect) as connect:
            self.assertEqual(self.query(), [(0,)])
        self.assertTrue(connect.call_args[0][0].startswith(self.run_dir + os.sep), connect.call_args)
        self.assertEqual(os.listdir(self.run_dir), [])

    def test_a_copy_left_by_a_killed_run_is_removed(self):
        stale = self.leftover(self.run_dir, "xdl-cookies-k1ll3d00", age=3600)
        running = self.leftover(self.run_dir, "xdl-cookies-n0wrun00", age=0)     # another copy of the app, right now
        others = [self.leftover(self.run_dir, name, age=3600) for name in ("xdl_test_sandbox", "other-program")]
        self.query()
        self.assertFalse(os.path.exists(stale))
        for path in [running] + others:
            self.assertTrue(os.path.exists(path), path)

    def test_without_a_runtime_folder_the_temp_folder_is_used_and_cleaned(self):
        tmp = os.path.join(self.base, "tmp")
        os.mkdir(tmp)
        stale = self.leftover(tmp, "xdl-cookies-k1ll3d00", age=3600)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}), mock.patch.object(tempfile, "tempdir", tmp):
            self.assertEqual(self.query(), [(0,)])
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
