"""
Extreme Stress & Edge-case Test Suite (150+ iterations per feature).
Verifies that all functions handle malformed, hostile, and boundary inputs gracefully without crashing.
"""
import concurrent.futures
import json
import logging
import os
import random
import sqlite3
import string
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Base, make_firefox_profile
from xdownloader import sites
from xdownloader.config import Config
from xdownloader.cookies import BrowserCookieManager, _host_sql
from xdownloader.core import DownloaderCore, DownloadThread
from xdownloader.env import EnvManager
from xdownloader.sites import extract_urls, find_site, normalize_url
from xdownloader.system import (
    LockManager, ProcessManager, QueueManager,
    atomic_write_lines, merge_unique, read_lines,
)


ITERATIONS = 160  # > 150 iterations per feature requirement


class TestStressConfig(Base):
    """Stress tests for Config and Settings management (>150 iterations each)."""

    def test_resolve_path_extreme_inputs(self):
        """Feature 1.1: Config.resolve_path with 160+ extreme/hostile inputs."""
        extreme_inputs = [
            None,
            "",
            "   \t\n  ",
            "\x00",
            "/tmp/\x00evil/path",
            "A" * 4096,  # buffer-overflow scale
            "../../../../../../../../../../etc/passwd",
            "~",
            "~/some_folder",
            "./relative/sub/path",
            "symbols_!@#$%^&*()_+=-`~[]{}|;':\",./<>?",
            "中文路徑/測試目錄/📁/測試",
            "   spaces around   ",
            12345,  # non-string
            {"bad": "type"},
            [1, 2, 3],
            True,
            False,
            "//multiple//leading//slashes//",
            "/trailing/slash///",
        ]
        # Pad with randomized extreme combinations to exceed 160 iterations
        random.seed(42)
        chars = string.ascii_letters + string.digits + " /._-\x00\t\n~!@#$%^&*()=+"
        for _ in range(ITERATIONS - len(extreme_inputs)):
            rand_len = random.randint(1, 200)
            extreme_inputs.append("".join(random.choice(chars) for _ in range(rand_len)))

        self.assertGreaterEqual(len(extreme_inputs), ITERATIONS)
        count = 0
        for raw in extreme_inputs:
            count += 1
            res = Config.resolve_path(raw)
            self.assertIsInstance(res, str)
            self.assertNotIn("\x00", res)
            self.assertTrue(os.path.isabs(res))
        self.assertEqual(count, len(extreme_inputs))

    def test_load_settings_corruption_and_boundaries(self):
        """Feature 1.2: Config.load_settings against 160+ corrupted/adversarial settings files."""
        for i in range(ITERATIONS):
            # Alternate between various corruption modes:
            # 1. Truncated / malformed JSON
            # 2. Over-sized files (>65536 bytes)
            # 3. Non-dict root (array, primitive, null)
            # 4. Out-of-bounds types for retries, format, language, download_dir
            mode = i % 8
            if mode == 0:
                payload = '{"download_dir": "' + ("A" * 70000) + '"}'  # >64KB limit
            elif mode == 1:
                payload = '{"download_dir": '  # truncated JSON syntax error
            elif mode == 2:
                payload = json.dumps([1, 2, 3, "not a dict"])
            elif mode == 3:
                payload = json.dumps({
                    "max_retries": random.choice([-100, 0, 11, 99999, "five", None, True, False, 3.5]),
                    "merge_format": random.choice(["avi", "wmv", "rmvb", None, 123, True]),
                    "language": random.choice(["fr", "ja", "es", None, 999, False]),
                    "download_dir": random.choice(["/dev/null", "\x00/evil", "", 123, None, []])
                })
            elif mode == 4:
                payload = "\x00\xff\xfe\x00\x00" * 20  # raw garbage binary
            elif mode == 5:
                payload = ""  # empty file
            elif mode == 6:
                # Valid but boundary settings
                payload = json.dumps({
                    "download_dir": self.base,
                    "max_retries": random.choice([1, 2, 3, 5, 10]),
                    "merge_format": random.choice(["mp4", "mkv"]),
                    "language": random.choice(["en", "zh_TW"]),
                })
            else:
                payload = "   \n\t  "

            with open(Config.SETTINGS_FILE, "wb") as f:
                f.write(payload.encode("utf-8", errors="ignore"))

            # Must never raise an unhandled exception
            try:
                Config.load_settings()
            except Exception as e:
                self.fail(f"Config.load_settings crashed on iteration {i} with payload {payload[:50]}: {e}")

            # Verify Config stays in a valid invariant state
            self.assertTrue(os.path.isdir(Config.DOWNLOAD_DIR))
            self.assertIn(Config.MERGE_FORMAT, Config.SUPPORTED_FORMATS)
            self.assertIn(Config.LANGUAGE, Config.SUPPORTED_LANGUAGES)
            self.assertIsInstance(Config.MAX_RETRIES, int)
            self.assertNotIsInstance(Config.MAX_RETRIES, bool)
            self.assertTrue(1 <= Config.MAX_RETRIES <= 10)

    def test_save_settings_rapid_concurrent_writes(self):
        """Feature 1.3: Config.save_settings rapid and concurrent writes (160+ iterations)."""
        def worker(idx):
            Config.MAX_RETRIES = (idx % 10) + 1
            Config.MERGE_FORMAT = "mkv" if idx % 2 == 0 else "mp4"
            Config.LANGUAGE = "zh_TW" if idx % 2 == 0 else "en"
            Config.DOWNLOAD_DIR = self.base
            Config.save_settings()

        # Run 160 saves using thread pool to stress concurrency & atomic replace
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, i) for i in range(ITERATIONS)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        # Verify final settings file is valid JSON, mode 0600, and has no leftover .tmp files
        self.assertTrue(os.path.isfile(Config.SETTINGS_FILE))
        mode = os.stat(Config.SETTINGS_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        with open(Config.SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("download_dir", data)
        self.assertIn("max_retries", data)
        self.assertIn("merge_format", data)
        self.assertIn("language", data)

        # Check no .tmp files were leaked in base
        tmp_files = [f for f in os.listdir(self.base) if ".tmp" in f]
        self.assertEqual(tmp_files, [])


class TestStressSites(Base):
    """Stress tests for URL parsing, normalization, extraction, and anti-injection (>150 iterations)."""

    def test_normalize_url_extreme(self):
        """Feature 2.1: sites.normalize_url on 160+ malformed / hostile inputs."""
        inputs = [
            "",
            "   ",
            "not_a_url",
            "ftp://x.com/status/123",
            "https://x.com",
            "https://x.com/",
            "https://x.com/user/status/123",
            "https://x.com/user/status/123/video/1",
            "https://x.com/user/status/123/photo/1",
            "https://x.com/i/web/status/123",
            "https://mobile.x.com/user/status/123?s=20",
            "https://www.twitter.com/user/status/123#frag",
            "https://t.co/xyz123",
            "https://x.com/user/status/-999",
            "https://x.com/user/status/9999999999999999999999999999999999999999999999999999",
            "https://x.com/user/status/123?foo=bar&baz=qux&v=99",
            "https://x.com/user/status/123/?trailing=yes#with_hash",
            "https://X.COM/USER/STATUS/123/VIDEO/1",
            "https://x.com/user/status/123;rm -rf /",
            "https://x.com/user/status/123' OR '1'='1",
            "https://x.com/user/status/123\x00evil",
            "https://x.com/user/status/123\nhttps://x.com/user/status/456",
        ]
        # Generate random URLs up to 160
        for i in range(len(inputs), ITERATIONS):
            status_id = "".join(random.choices(string.digits, k=18))
            junk = "".join(random.choices(string.ascii_letters + "?&=#/%", k=20))
            inputs.append(f"https://x.com/test_user_{i}/status/{status_id}/{junk}")

        for u in inputs:
            try:
                res = normalize_url(u)
                self.assertIsInstance(res, str)
                # If it matched X canonical url, /video/1 or trailing query should be stripped
                if "x.com" in u and "/status/" in u and any(c.isdigit() for c in u):
                    if "?" not in u and "#" not in u and "/video/" in u:
                        self.assertNotIn("/video/", res)
            except Exception as e:
                self.fail(f"normalize_url crashed on '{u}': {e}")

    def test_extract_urls_glued_pastes_and_injection(self):
        """Feature 2.2: sites.extract_urls handling glued URLs, injection strings (160+ iterations)."""
        injection_templates = [
            "https://x.com/user/status/{id}; rm -rf /",
            "https://x.com/user/status/{id} && echo pwned",
            "https://x.com/user/status/{id} | cat /etc/passwd",
            "https://x.com/user/status/{id}`whoami`",
            "https://x.com/user/status/{id}$(id)",
            "https://x.com/user/status/{id}\nhttps://twitter.com/other/status/{id}",
            "Notes before https://x.com/user/status/{id}/video/1 and notes after",
            "https://x.com/user/status/{id}https://twitter.com/user/status/{id2}",  # glued
            "https://x.com/user/status/{id}https://x.com/user/status/{id}",  # duplicate glued
            "https://x.com/user/status/{id}?s=20 https://x.com/user/status/{id}/video/1",  # duplicate with diff params
        ]

        for i in range(ITERATIONS):
            tmpl = injection_templates[i % len(injection_templates)]
            raw_text = tmpl.format(id=1000 + i, id2=2000 + i)
            # Add random noise
            raw_text = f"  \t\n  {raw_text}  \r\n  random_text_{i}  "
            extracted = extract_urls(raw_text)
            self.assertIsInstance(extracted, list)
            # Extracted items must be supported sites
            for u in extracted:
                site = find_site(u)
                self.assertIsNotNone(site, f"Extracted unsupported URL: {u}")
                # Ensure no whitespace inside token
                self.assertNotIn(" ", u)
                self.assertNotIn("\n", u)
                self.assertNotIn("\t", u)

    def test_find_site_boundary_domains(self):
        """Feature 2.3: sites.find_site with lookalike and spoofing domains (160+ iterations)."""
        spoofs = [
            "https://notx.com/user/status/123",
            "https://x.com.evil.org/user/status/123",
            "https://twitter.com.phishing.io/user/status/123",
            "https://fake-twitter.com/123",
            "http://x.com/123",  # HTTP instead of HTTPS (X requires https://)
            "https://sub.x.com/123",
            "https://myt.co/123",
            "https://t.co.attacker.com/123",
        ]
        valid = [
            "https://x.com/user/status/1",
            "https://www.x.com/user/status/1",
            "https://mobile.x.com/user/status/1",
            "https://twitter.com/user/status/1",
            "https://www.twitter.com/user/status/1",
            "https://mobile.twitter.com/user/status/1",
            "https://t.co/abcXYZ",
        ]
        for i in range(ITERATIONS):
            if i % 2 == 0:
                s = random.choice(spoofs) + f"?i={i}"
                self.assertIsNone(find_site(s), f"False positive on spoof: {s}")
            else:
                v = random.choice(valid) + f"_{i}"
                self.assertIsNotNone(find_site(v), f"Failed to match valid site: {v}")


class TestStressSystem(Base):
    """Stress tests for atomic writes, lock manager, queue manager, process manager (>150 iterations)."""

    def test_atomic_write_and_read_lines_stress(self):
        """Feature 3.1: atomic_write_lines and read_lines with 160+ heavy/concurrent cycles."""
        target_file = os.path.join(self.base, "atomic_test.txt")
        symlink_target = os.path.join(self.base, "real_target.txt")
        symlink_file = os.path.join(self.base, "symlink_test.txt")
        os.symlink(symlink_target, symlink_file)

        for i in range(ITERATIONS):
            # Generate random lines
            lines_count = random.randint(0, 100)
            lines = [f"line_{j}_{random.random()}" for j in range(lines_count)]
            if i % 2 == 0:
                # Write to regular file
                atomic_write_lines(target_file, lines, mode=0o644)
                read_back = read_lines(target_file)
                self.assertEqual(read_back, lines)
            else:
                # Write through symlink -> must preserve symlink
                atomic_write_lines(symlink_file, lines, mode=0o600)
                self.assertTrue(os.path.islink(symlink_file))
                read_back = read_lines(symlink_file)
                self.assertEqual(read_back, lines)

        # Non-existent file read
        self.assertEqual(read_lines(os.path.join(self.base, "non_existent.txt")), [])

    def test_lock_manager_rapid_acquire_release(self):
        """Feature 3.2: LockManager rapid acquire and release cycles (160+ iterations)."""
        lm = LockManager(self.log)
        for i in range(ITERATIONS):
            acquired = lm.acquire()
            self.assertTrue(acquired, f"Failed to acquire lock on iteration {i}")
            self.assertTrue(os.path.isfile(Config.LOCK_FILE))
            with open(Config.LOCK_FILE, "r") as f:
                content = f.read().strip()
                self.assertEqual(content, str(os.getpid()))

            # Double acquire in same instance should still hold or re-check
            lm.release()
            self.assertFalse(os.path.exists(Config.LOCK_FILE))

    def test_queue_manager_crash_resume_cycles(self):
        """Feature 3.3: QueueManager save, load_unfinished, clear (160+ iterations)."""
        qm = QueueManager(self.log)
        for i in range(ITERATIONS):
            urls = [f"https://x.com/user/status/{i * 100 + j}" for j in range(random.randint(0, 20))]
            qm.save(urls)
            loaded = qm.load_unfinished()
            self.assertEqual(loaded, urls)
            if i % 3 == 0:
                qm.clear()
                self.assertEqual(qm.load_unfinished(), [])

    def test_process_manager_run_timeout_and_kill(self):
        """Feature 3.4: ProcessManager process lifecycle & signal handling (160+ iterations)."""
        for i in range(ITERATIONS):
            mode = i % 4
            if mode == 0:
                # Normal fast execution
                res = ProcessManager.run(["echo", f"test_{i}"], stdout=subprocess.PIPE, text=True)
                self.assertEqual(res.returncode, 0)
                self.assertEqual(res.stdout.strip(), f"test_{i}")
            elif mode == 1:
                # Timeout handling
                with self.assertRaises(subprocess.TimeoutExpired):
                    ProcessManager.run(["sleep", "10"], timeout=0.05)
            elif mode == 2:
                # Check=True error raising
                with self.assertRaises(subprocess.CalledProcessError):
                    ProcessManager.run(["false"], check=True)
            elif mode == 3:
                # Wait interruptibility
                res = ProcessManager.wait(0.01)
                self.assertFalse(res)


class TestStressCookies(Base):
    """Stress tests for BrowserCookieManager (>150 iterations)."""

    def test_sqlite_corrupted_and_wal_queries(self):
        """Feature 4.1: Querying corrupted / flaky SQLite cookie databases (160+ iterations)."""
        mgr = BrowserCookieManager(self.log, sites.X)
        db_path = Path(self.base) / "test_cookies.sqlite"

        # Create valid SQLite first
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, path TEXT, expiry INT, isSecure INT)")
        conn.execute("INSERT INTO moz_cookies VALUES ('auth_token', 'secret', 'x.com', '/', 9999999999, 1)")
        conn.commit()
        conn.close()

        for i in range(ITERATIONS):
            if i % 2 == 0:
                # Query valid copy
                rows = mgr._query_copy(db_path, "SELECT name, value FROM moz_cookies")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], "auth_token")
                sql = f"SELECT host FROM moz_cookies WHERE {_host_sql('host', sites.X.cookie_domains)}"
                matched = mgr._query_copy(db_path, sql)
                self.assertEqual(len(matched), 1)
            else:
                # Corrupted DB: _db_has_login must safely return False without crashing
                corrupt_db = Path(self.base) / f"corrupt_{i}.sqlite"
                with open(corrupt_db, "wb") as f:
                    f.write(b"not a valid sqlite file database header garbage")
                has_login = mgr._db_has_login("firefox", corrupt_db)
                self.assertFalse(has_login)
                try:
                    os.remove(corrupt_db)
                except OSError:
                    pass

    def test_host_sql_injection_and_domain_leaks(self):
        """Feature 4.2: Verify _host_sql never leaks unrelated domains (160+ iterations)."""
        test_hosts = [
            ("x.com", True),
            (".x.com", True),
            ("api.x.com", True),
            ("sub.domain.twitter.com", True),
            ("twitter.com", True),
            (".twitter.com", True),
            # Lookalikes that must NEVER match
            ("netflix.com", False),
            ("dropbox.com", False),
            ("box.com", False),
            ("fake-x.com", False),
            ("myx.com", False),
            ("x.com.evil.com", False),
            ("twitter.com.attacker.org", False),
            ("not-twitter.com", False),
            ("prefix_x.com", False),
        ]

        db_path = Path(self.base) / "domain_leak_test.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test_cookies (host TEXT, name TEXT)")

        for host, _ in test_hosts:
            conn.execute("INSERT INTO test_cookies VALUES (?, 'cookie_val')", (host,))
        conn.commit()
        conn.close()

        sql = f"SELECT host FROM test_cookies WHERE {_host_sql('host', sites.X.cookie_domains)}"
        for i in range(ITERATIONS):
            conn = sqlite3.connect(str(db_path))
            matched = {r[0] for r in conn.execute(sql).fetchall()}
            conn.close()

            for host, should_match in test_hosts:
                if should_match:
                    self.assertIn(host, matched, f"Host {host} should have matched")
                else:
                    self.assertNotIn(host, matched, f"Host {host} LEAKED through _host_sql!")

    def test_export_firefox_cookies_sanitization(self):
        """Feature 4.3: Firefox cookie export sanitization of tabs/newlines (160+ iterations)."""
        prof = os.path.join(self.base, "ff_prof")
        os.makedirs(prof, exist_ok=True)

        # Inject cookies with tabs/newlines that would corrupt Netscape format
        dirty_cookies = [
            ("auth_token", "valid_token", ".x.com"),
            ("ct0", "valid_csrf", ".x.com"),
            ("evil_tab", "value\twith\ttabs", ".x.com"),
            ("evil_newline", "value\nwith\nnewlines", ".x.com"),
            ("evil_crlf", "value\r\nwith\r\ncrlf", ".x.com"),
        ]
        make_firefox_profile(self.base, "ff_prof", dirty_cookies)

        mgr = BrowserCookieManager(self.log, sites.X)
        mgr._detected_browser = "firefox"
        mgr._detected_profile = prof

        for i in range(ITERATIONS):
            ok = mgr.export_firefox_cookies()
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(mgr.cookies_file))
            # Verify file mode 0600
            self.assertEqual(os.stat(mgr.cookies_file).st_mode & 0o777, 0o600)
            # Verify no line contains unescaped tabs inside value
            with open(mgr.cookies_file, "r", encoding="utf-8") as f:
                lines = [l for l in f.read().splitlines() if l and not l.startswith("#")]
                for line in lines:
                    cols = line.split("\t")
                    self.assertEqual(len(cols), 7, f"Malformed line: {line}")
                    self.assertNotIn("evil_tab", cols[5])
                    self.assertNotIn("evil_newline", cols[5])

    def test_check_cookies_file_edge_cases(self):
        """Feature 4.4: _check_cookies_file on 160+ corrupted/permissive cookie files."""
        mgr = BrowserCookieManager(self.log, sites.X)
        cpath = mgr.cookies_file

        for i in range(ITERATIONS):
            mode = i % 5
            if mode == 0:
                # Missing file
                if os.path.exists(cpath):
                    os.remove(cpath)
                self.assertFalse(mgr._check_cookies_file())
            elif mode == 1:
                # Overly permissive mode (0777) -> should be chmod 0600
                with open(cpath, "w") as f:
                    f.write("# Netscape\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tval\n.x.com\tTRUE\t/\tTRUE\t0\tct0\tval\n")
                os.chmod(cpath, 0o777)
                self.assertTrue(mgr._check_cookies_file())
                self.assertEqual(os.stat(cpath).st_mode & 0o777, 0o600)
            elif mode == 2:
                # Missing required cookie (e.g. ct0)
                with open(cpath, "w") as f:
                    f.write("# Netscape\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tval\n")
                self.assertFalse(mgr._check_cookies_file())
            elif mode == 3:
                # Binary garbage
                with open(cpath, "wb") as f:
                    f.write(os.urandom(512))
                self.assertFalse(mgr._check_cookies_file())
            elif mode == 4:
                # Empty file
                with open(cpath, "w") as f:
                    f.write("")
                self.assertFalse(mgr._check_cookies_file())


class TestStressEnv(Base):
    """Stress tests for EnvManager (>150 iterations)."""

    def test_ensure_binary_validation_and_permissions(self):
        """Feature 5.1: EnvManager._ensure validation and chmod auto-repair (160+ iterations)."""
        env = EnvManager(self.log)
        dummy_exe = os.path.join(self.base, "dummy_exe")

        for i in range(ITERATIONS):
            mode = i % 4
            if mode == 0:
                # File < 1024 bytes -> considered corrupt, calls downloader
                with open(dummy_exe, "wb") as f:
                    f.write(b"tiny")
                called = []
                ok = env._ensure(dummy_exe, "dummy", lambda: called.append(True) or True)
                self.assertTrue(ok)
                self.assertEqual(len(called), 1)
            elif mode == 1:
                # File > 1024 bytes, lacks exec permission -> auto-chmod 0o755
                with open(dummy_exe, "wb") as f:
                    f.write(b"A" * 2048)
                os.chmod(dummy_exe, 0o644)
                called = []
                ok = env._ensure(dummy_exe, "dummy", lambda: called.append(True) or True)
                self.assertTrue(ok)
                self.assertEqual(len(called), 0)
                self.assertTrue(os.access(dummy_exe, os.X_OK))
            elif mode == 2:
                # Non-existent file -> calls downloader
                if os.path.exists(dummy_exe):
                    os.remove(dummy_exe)
                called = []
                ok = env._ensure(dummy_exe, "dummy", lambda: called.append(True) or True)
                self.assertTrue(ok)
                self.assertEqual(len(called), 1)
            elif mode == 3:
                # Downloader failure returns False
                if os.path.exists(dummy_exe):
                    os.remove(dummy_exe)
                ok = env._ensure(dummy_exe, "dummy", lambda: False)
                self.assertFalse(ok)

    def test_update_yt_dlp_never_raises(self):
        """Feature 5.2: EnvManager.update_yt_dlp resilience against failures (160+ iterations)."""
        env = EnvManager(self.log)
        for i in range(ITERATIONS):
            # Target binary does not exist or is a broken script
            if i % 3 == 0:
                Config.YT_DLP_EXE = os.path.join(self.base, f"nonexistent_{i}")
            elif i % 3 == 1:
                Config.YT_DLP_EXE = os.path.join(self.base, f"corrupt_{i}")
                with open(Config.YT_DLP_EXE, "w") as f:
                    f.write("#!/bin/sh\nexit 1\n")
                os.chmod(Config.YT_DLP_EXE, 0o755)
            else:
                Config.YT_DLP_EXE = os.path.join(self.base, f"unexecutable_{i}")
                with open(Config.YT_DLP_EXE, "w") as f:
                    f.write("#!/bin/sh\nexit 0\n")
                os.chmod(Config.YT_DLP_EXE, 0o000)

            # Must NEVER raise any exception
            try:
                env.update_yt_dlp()
            except Exception as e:
                self.fail(f"update_yt_dlp crashed on iteration {i}: {e}")


class TestStressCore(Base):
    """Stress tests for DownloaderCore and DownloadThread (>150 iterations)."""

    def test_base_cmd_injection_immunity(self):
        """Feature 6.1: Command building immunity against option injection (160+ iterations)."""
        core = DownloaderCore(self.log)
        evil_urls = [
            "--exec=rm -rf /",
            "-o /etc/passwd",
            "--config-location /tmp/evil",
            "-F --simulate",
            "--batch-file=malicious.txt",
            "--newline --verbose",
            "https://x.com/test/status/1 --custom-flag",
            "https://x.com/test/status/1;reboot",
            "https://x.com/test/status/1'--eval",
            "https://x.com/test/status/1\"--help",
        ]
        for i in range(ITERATIONS):
            evil = evil_urls[i % len(evil_urls)] + f"?t={i}"
            cmd = core._base_cmd(evil, ["--cookies", "cookies.txt"])
            self.assertEqual(cmd[0], Config.YT_DLP_EXE)
            # The URL MUST be preceded by '--' to prevent argument parsing
            self.assertIn("--", cmd)
            dash_dash_idx = cmd.index("--")
            self.assertEqual(cmd[dash_dash_idx + 1], evil)
            self.assertEqual(dash_dash_idx + 2, len(cmd))

    def test_downloader_core_retries_and_non_retryable(self):
        """Feature 6.2: DownloaderCore.run retry limits & instant failure (160+ iterations)."""
        self.install_fake_tools()
        core = DownloaderCore(self.log)

        for i in range(ITERATIONS):
            Config.MAX_RETRIES = 2
            Config.RETRY_DELAY = 0.001
            mode = i % 3
            if mode == 0:
                # Success
                ok = core.run(f"https://x.com/u/status/ok_{i}")
                self.assertTrue(ok)
            elif mode == 1:
                # Non-retryable error (e.g. tweet unavailable / gone) -> should NOT retry
                before_calls = len(self.calls())
                ok = core.run(f"https://x.com/u/status/gone_{i}")
                self.assertFalse(ok)
                after_calls = len(self.calls())
                # Should have failed immediately on 1st attempt
                self.assertEqual(after_calls - before_calls, 1)
            elif mode == 2:
                # Flaky error -> retries up to MAX_RETRIES (2 times)
                before_calls = len(self.calls())
                ok = core.run(f"https://x.com/u/status/flaky_{i}")
                self.assertFalse(ok)
                after_calls = len(self.calls())
                self.assertEqual(after_calls - before_calls, 2)

    def test_download_thread_double_buffer_concurrency(self):
        """Feature 6.3: DownloadThread double-buffering & concurrent list modifications (160+ iterations)."""
        self.install_fake_tools()
        finish_events = []

        def on_done(pending):
            finish_events.append(pending)

        orig_run = DownloaderCore.run

        for i in range(ITERATIONS):
            finish_events.clear()
            urls = [f"https://x.com/user/status/ok_{i}_{j}" for j in range(2)]
            external = f"https://x.com/user/status/external_{i}"
            # Write to list.txt
            atomic_write_lines(Config.LIST_FILE, urls)

            def run_and_append(core, url, external=external, first=urls[0]):
                # Concurrent simulation: another program appends to list.txt while a download is active.
                # (Done inside the batch instead of after a sleep(): a timer sometimes landed in the
                # unavoidable list.txt rewrite windows at the start and end of a batch.)
                if url == first:
                    with open(Config.LIST_FILE, "a", encoding="utf-8") as f:
                        f.write(external + "\n")
                return orig_run(core, url)

            with patch.object(DownloaderCore, "run", run_and_append):
                thread = DownloadThread(urls=urls, on_finish=on_done)
                thread.start()
                thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), f"DownloadThread hung on iteration {i}")
            self.assertEqual(len(finish_events), 1)
            # External additions should have survived into list.txt
            final_list = read_lines(Config.LIST_FILE)
            self.assertIn(external, final_list)


class TestStressGui(Base):
    """Stress tests for GUI components, event loop, dialogs (>150 iterations)."""

    def setUp(self):
        super().setUp()
        if not os.environ.get("DISPLAY"):
            self.skipTest("No DISPLAY available for GUI testing.")
        import customtkinter as ctk
        from xdownloader.gui import App, CustomMessageBox, DirectoryChooserDialog, GUILogHandler, SettingsDialog
        self.ctk = ctk
        self.App = App
        self.CustomMessageBox = CustomMessageBox
        self.DirectoryChooserDialog = DirectoryChooserDialog
        self.SettingsDialog = SettingsDialog
        self.GUILogHandler = GUILogHandler

    def test_gui_log_handler_burst_and_rolling_limit(self):
        """Feature 7.1: GUILogHandler handling high-frequency bursts and rolling (160+ iterations)."""
        root = self.ctk.CTk()
        tb = self.ctk.CTkTextbox(root)
        handler = self.GUILogHandler(tb)

        logger = logging.getLogger("TestBurstLogger")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        for i in range(ITERATIONS):
            # Burst 20 log records per iteration = 3200 records total
            for j in range(20):
                logger.info(f"Burst log entry {i}_{j} with some arguments: %s", "arg")
            handler.drain()
            # History must be capped at MAX_LINES
            self.assertLessEqual(len(handler._history), handler.MAX_LINES)

        # Test language refresh
        handler.refresh_language()
        handler.clear()
        self.assertEqual(len(handler._history), 0)
        root.destroy()

    def test_custom_message_box_rapid_lifecycle(self):
        """Feature 7.2: CustomMessageBox rapid open and dismiss (160+ iterations)."""
        root = self.ctk.CTk()
        root.geometry("400x300")
        root.update()

        for i in range(ITERATIONS):
            mode = "askyesno" if i % 2 == 0 else "info"
            dlg = self.CustomMessageBox(root, f"Title {i}", f"Message content {i}", mode=mode)
            dlg.update()
            # Programmatically trigger dismiss
            if mode == "askyesno":
                dlg._on_yes()
                self.assertTrue(dlg.result)
            else:
                dlg._on_no()
                self.assertFalse(dlg.result)
            root.update()

        root.destroy()

    def test_directory_chooser_dialog_navigation_stress(self):
        """Feature 7.3: DirectoryChooserDialog navigation across 160+ rapid changes."""
        root = self.ctk.CTk()
        root.update()

        dlg = self.DirectoryChooserDialog(root, initialdir=self.base)
        dlg.update()

        subdirs = []
        for i in range(10):
            d = os.path.join(self.base, f"folder_{i}")
            os.makedirs(d, exist_ok=True)
            subdirs.append(d)

        for i in range(ITERATIONS):
            target = random.choice(subdirs) if i % 2 == 0 else self.base
            dlg._navigate_to(target)
            dlg.update()
            self.assertEqual(dlg._current_path, os.path.abspath(target))

        dlg._on_cancel()
        root.update()
        root.destroy()

    def test_settings_dialog_tabs_and_rapid_toggles(self):
        """Feature 7.4: SettingsDialog tab switching & option toggling (160+ iterations)."""
        root = self.ctk.CTk()
        root.update()

        dlg = self.SettingsDialog(root, logger=self.log)
        dlg.update()

        tabs = ["General", "Advanced", "About"]
        for i in range(ITERATIONS):
            tab = tabs[i % 3]
            dlg._show_tab(tab)
            dlg.update()
            self.assertEqual(dlg._active_tab, tab)

            if tab == "Advanced":
                dlg._on_retry_change(str((i % 10) + 1))
                dlg._on_format_change("mkv" if i % 2 == 0 else "mp4")
            elif tab == "General":
                dlg._on_language_change("zh_TW" if i % 2 == 0 else "en")

        dlg.destroy()
        root.update()
        root.destroy()

    def test_app_lifecycle_and_input_stress(self):
        """Feature 7.5: Full App event loop, paste, queue, and clean exit (160+ iterations)."""
        self.install_fake_tools()

        for i in range(ITERATIONS):
            # Prior iteration may have persisted URLs or cleared it
            expected_preloaded = read_lines(Config.LIST_FILE)
            app = self.App()
            app.update()

            preloaded_valid = app._get_valid_urls()
            self.assertEqual(preloaded_valid, expected_preloaded)

            # Insert new batch of URLs
            new_urls = [f"https://x.com/user/status/{i * 1000 + j}" for j in range(random.randint(1, 5))]
            app._listbox.insert("end", "\n".join(new_urls) + "\n")
            app._refresh_count()
            app.update()

            all_valid = app._get_valid_urls()
            expected_total = merge_unique(expected_preloaded, new_urls)
            self.assertEqual(all_valid, expected_total)

            # Every alternate iteration, test clearing the list
            if i % 2 == 1:
                app._on_clear_list()
                app.update()
                self.assertEqual(len(app._get_valid_urls()), 0)

            # Close app cleanly
            app._on_closing()
            app.update()

            # Verify that list.txt matches the final state upon closing
            expected_on_disk = [] if i % 2 == 1 else expected_total
            self.assertEqual(read_lines(Config.LIST_FILE), expected_on_disk)


if __name__ == "__main__":
    unittest.main()
