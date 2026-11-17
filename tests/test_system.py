"""system.py: files, child processes, the lock, the resume queue, the log and the host environment."""
import logging
import logging.handlers
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from common import (
    Base, Config, LockManager, PROJECT, ProcessManager, QueueManager, REAL_YTDLP, ShutdownRequested,
    atomic_write_lines, process_alive, read_lines, setup_logger, write_fake_ytdlp,
)
from xdownloader.system import host_env


HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class TestFiles(Base):
    def test_atomic_write_and_mode(self):
        p = os.path.join(self.base, "f.txt")
        atomic_write_lines(p, ["a", "b"], mode=0o600)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "a\nb\n")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        atomic_write_lines(p, [])
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "")
        self.assertFalse(os.path.exists(p + ".tmp"))
        self.assertEqual(read_lines(os.path.join(self.base, "missing")), [])

    def test_atomic_write_follows_symlink(self):
        real = os.path.join(self.base, "cloud.txt")
        link = os.path.join(self.base, "list.link")
        atomic_write_lines(real, ["old"])
        os.symlink(real, link)
        atomic_write_lines(link, ["new"])
        self.assertTrue(os.path.islink(link))             # the link itself is kept
        self.assertEqual(read_lines(real), ["new"])

    def test_atomic_write_ignores_stale_tmp(self):
        p = os.path.join(self.base, "f.txt")
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT, 0o600)   # leftover of a crash
        os.write(fd, b"garbage-garbage-garbage")
        os.close(fd)
        os.chmod(p + ".tmp", 0o600)
        atomic_write_lines(p, ["a"])
        self.assertEqual(read_lines(p), ["a"])
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o666 & ~umask)   # not the stale 0600

    def test_atomic_write_concurrent_writers(self):
        p = os.path.join(self.base, "f.txt")
        payloads = [[f"w{i}-{j}" for j in range(500)] for i in range(8)]
        errors = []

        def write(lines):
            try:
                for _ in range(10):
                    atomic_write_lines(p, lines)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=write, args=(pl,)) for pl in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn(read_lines(p), payloads)            # exactly one writer's complete content

    def test_queue_manager(self):
        q = QueueManager(self.log)
        self.assertEqual(q.load_unfinished(), [])
        q.save(["u1", "u2"])
        self.assertEqual(q.load_unfinished(), ["u1", "u2"])
        q.clear()
        self.assertEqual(q.load_unfinished(), [])
        q.clear()  # Verify clear() is idempotent when the queue file is already absent.

    def test_settings_load_and_save(self):
        Config.DOWNLOAD_DIR = self.base
        test_dir = os.path.join(self.base, "custom_dl")
        os.makedirs(test_dir, exist_ok=True)
        Config.DOWNLOAD_DIR = test_dir
        Config.MAX_RETRIES = 5
        Config.MERGE_FORMAT = "mkv"
        Config.LANGUAGE = "zh_TW"
        Config.save_settings()
        self.assertTrue(os.path.exists(Config.SETTINGS_FILE))
        self.assertEqual(stat.S_IMODE(os.stat(Config.SETTINGS_FILE).st_mode), 0o600)

        # Reset in memory and reload
        Config.DOWNLOAD_DIR = self.base
        Config.MAX_RETRIES = 3
        Config.MERGE_FORMAT = "mp4"
        Config.LANGUAGE = "en"
        Config.load_settings()
        self.assertEqual(Config.DOWNLOAD_DIR, test_dir)
        self.assertEqual(Config.MAX_RETRIES, 5)
        self.assertEqual(Config.MERGE_FORMAT, "mkv")
        self.assertEqual(Config.LANGUAGE, "zh_TW")

        # Symlinked settings.json is preserved and target is updated with 0o600
        real_settings = os.path.join(self.base, "real_settings.json")
        os.replace(Config.SETTINGS_FILE, real_settings)
        os.symlink(real_settings, Config.SETTINGS_FILE)
        Config.MAX_RETRIES = 2
        Config.save_settings()
        self.assertTrue(os.path.islink(Config.SETTINGS_FILE))
        self.assertEqual(stat.S_IMODE(os.stat(real_settings).st_mode), 0o600)

        # Oversized settings.json (> 64 KiB) is rejected without loading into memory
        with open(real_settings, "w", encoding="utf-8") as f:
            f.write('{"max_retries": 9, "pad": "' + ("x" * 70000) + '"}')
        Config.MAX_RETRIES = 2
        Config.load_settings()
        self.assertEqual(Config.MAX_RETRIES, 2)

    def test_custom_download_dir_edge_cases(self):
        # 1. Unicode and spaces in path
        special_dir = os.path.join(self.base, "自訂 下載 目錄 (test)")
        os.makedirs(special_dir, exist_ok=True)
        Config.DOWNLOAD_DIR = special_dir
        Config.save_settings()
        Config.DOWNLOAD_DIR = self.base
        Config.load_settings()
        self.assertEqual(Config.DOWNLOAD_DIR, special_dir)

        # 2. Non-existent path does not overwrite valid Config.DOWNLOAD_DIR
        non_existent = os.path.join(self.base, "does_not_exist_xyz")
        with open(Config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write(f'{{"download_dir": "{non_existent}"}}')
        Config.DOWNLOAD_DIR = special_dir
        Config.load_settings()
        self.assertEqual(Config.DOWNLOAD_DIR, special_dir)

        # 3. Whitespace-only path does not overwrite
        with open(Config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write('{"download_dir": "   \\t  \\n "}')
        Config.DOWNLOAD_DIR = special_dir
        Config.load_settings()
        self.assertEqual(Config.DOWNLOAD_DIR, special_dir)

        # 4. Corrupted json does not crash load_settings
        with open(Config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write("{invalid json content!@#$")
        Config.DOWNLOAD_DIR = special_dir
        Config.load_settings()
        self.assertEqual(Config.DOWNLOAD_DIR, special_dir)

        # 5. Unsupported or non-string language does not overwrite valid Config.LANGUAGE
        Config.LANGUAGE = "zh_TW"
        with open(Config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write('{"language": "fr_FR"}')
        Config.load_settings()
        self.assertEqual(Config.LANGUAGE, "zh_TW")
        with open(Config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write('{"language": 123}')
        Config.load_settings()
        self.assertEqual(Config.LANGUAGE, "zh_TW")

    def test_lock_manager(self):
        a, b = LockManager(self.log), LockManager(self.log)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.assertIsNone(b._lock_file)            # failed attempt must not leak the fd
        self.assertEqual(read_lines(Config.LOCK_FILE), [str(os.getpid())])
        a.release()
        self.assertFalse(os.path.exists(Config.LOCK_FILE))
        self.assertTrue(b.acquire())
        b.release()

    def test_lock_across_processes(self):
        a = LockManager(self.log)
        self.assertTrue(a.acquire())
        code = (f"import sys; sys.path.insert(0, {PROJECT!r}); import logging; from xdownloader.config import Config; from xdownloader.system import LockManager;"
                f"Config.LOCK_FILE={Config.LOCK_FILE!r};"
                "sys.exit(0 if LockManager(logging.getLogger('t')).acquire() else 3)")
        r = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(r.returncode, 3)
        a.release()
        r = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(r.returncode, 0)

    def test_logger_rotating(self):
        lg = logging.getLogger("XDownloader")
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg = setup_logger()
        self.assertTrue(any(isinstance(h, logging.handlers.RotatingFileHandler) for h in lg.handlers))
        self.assertIs(setup_logger(), lg)
        self.assertEqual(len(lg.handlers), 1)


class TestProcessManager(Base):
    def test_run_and_check(self):
        r = ProcessManager.run(["sh", "-c", "echo hi"], stdout=subprocess.PIPE, text=True)
        self.assertEqual(r.stdout.strip(), "hi")
        with self.assertRaises(subprocess.CalledProcessError):
            ProcessManager.run(["sh", "-c", "exit 4"], check=True)
        self.assertEqual(ProcessManager._active_procs, [])

    def test_timeout_kills_tree(self):
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            ProcessManager.run(["sh", "-c", "sleep 30 & sleep 30"], timeout=0.5,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(ProcessManager._active_procs, [])

    def test_timeout_with_dead_leader_and_pipe_holding_grandchild(self):
        # The leader exits at once but its background child keeps stdout open: communicate()
        # times out, and the group must still be killed (poll() reaping the leader used to make the
        # kill return early, so the follow-up communicate() blocked until the child ended).
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            ProcessManager.run(["sh", "-c", "sleep 30 & exit 0"], timeout=1,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(ProcessManager._active_procs, [])

    def test_kill_all_with_dead_leader_and_pipe_holding_grandchild(self):
        result = {}

        def worker():
            try:
                ProcessManager.run(["sh", "-c", "sleep 30 & exit 0"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                result["r"] = "returned"
            except ShutdownRequested:
                result["r"] = "shutdown"
            except Exception as e:
                result["r"] = repr(e)

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.5)                       # leader is gone, grandchild keeps the pipes open
        t0 = time.time()
        ProcessManager.kill_all()
        t.join(5)
        self.assertFalse(t.is_alive(), "worker hung: the grandchild was never killed")
        self.assertLess(time.time() - t0, 5)

    def test_kill_all_and_no_respawn(self):
        marker = os.path.join(self.base, "child.pid")
        result = {}

        def worker():
            try:
                ProcessManager.run(["sh", "-c", f"sleep 30 & echo $! > {marker}; wait"])
                result["r"] = "returned"
            except ShutdownRequested:
                result["r"] = "shutdown"

        t = threading.Thread(target=worker)
        t.start()
        for _ in range(50):
            if os.path.exists(marker) and read_lines(marker):
                break
            time.sleep(0.1)
        grandchild = int(read_lines(marker)[0])
        ProcessManager.kill_all()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["r"], "shutdown")
        # the whole process group (including the background grandchild) is gone
        for _ in range(20):
            if not process_alive(grandchild):
                break
            time.sleep(0.1)
        self.assertFalse(process_alive(grandchild))
        # after kill_all no new process may be started
        with self.assertRaises(ShutdownRequested):
            ProcessManager.run(["true"])
        self.assertTrue(ProcessManager.wait(10))   # returns immediately when shutting down


@unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
class TestUnusableLockFile(Base):
    """A lock file that cannot even be opened (e.g. left behind by a root run) was reported as
    "another instance is already running", sending users after a process that did not exist."""

    def test_is_reported_as_an_error_not_as_a_running_instance(self):
        open(Config.LOCK_FILE, "w").close()
        os.chmod(Config.LOCK_FILE, 0)
        self.addCleanup(os.chmod, Config.LOCK_FILE, 0o644)
        with self.assertLogs(self.log, "ERROR") as logs, self.assertRaises(PermissionError):
            LockManager(self.log).acquire()
        self.assertIn("Cannot use the lock file", logs.output[0])

    def test_a_running_instance_is_still_reported_as_such(self):
        holder = LockManager(self.log)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        self.assertFalse(LockManager(self.log).acquire())

    @unittest.skipUnless(HAS_DISPLAY, "needs a display")
    def test_the_app_explains_it(self):
        from xdownloader import gui
        from xdownloader.i18n import t
        error = PermissionError(13, "Permission denied", Config.LOCK_FILE)
        with mock.patch.object(gui.LockManager, "acquire", side_effect=error), \
                mock.patch.object(gui, "CustomMessageBox") as box, self.assertRaises(SystemExit) as exit_:
            gui.App()
        self.assertEqual(exit_.exception.code, 1)
        self.assertEqual(box.call_args.args[2], t("msg_lock_failed", error=error))


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class TestUnusableLogFile(Base):
    """A log file that cannot be opened (left behind by one run with sudo, together with the lock file) raised
    PermissionError out of App(), before the lock was even tried: no window, no dialog, only a traceback in
    gui_startup.log. Like an unusable lock file it is now explained in a dialog."""

    def test_the_app_explains_it(self):
        from xdownloader import gui
        from xdownloader.i18n import t
        error = PermissionError(13, "Permission denied", Config.LOG_FILE)
        with mock.patch.object(gui, "setup_logger", side_effect=error), \
                mock.patch.object(gui, "CustomMessageBox") as box, self.assertRaises(SystemExit) as exit_:
            gui.App()
        self.assertEqual(exit_.exception.code, 1)
        self.assertEqual(box.call_args.args[2], t("msg_log_failed", error=error))

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_a_log_file_that_really_cannot_be_written(self):
        import logging
        from xdownloader import gui
        logger = logging.getLogger("XDownloader")
        saved = list(logger.handlers)
        for handler in saved:                       # setup_logger() leaves a logger that already has handlers alone
            logger.removeHandler(handler)

        def restore():
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            for handler in saved:
                logger.addHandler(handler)
        self.addCleanup(restore)
        open(Config.LOG_FILE, "w").close()
        os.chmod(Config.LOG_FILE, 0o444)
        self.addCleanup(os.chmod, Config.LOG_FILE, 0o644)
        with mock.patch.object(gui, "CustomMessageBox") as box, self.assertRaises(SystemExit):
            gui.App()
        self.assertIn(Config.LOG_FILE, box.call_args.args[2])


class TestHostProgramsGetTheHostsEnvironment(Base):
    """bin/python of the bundled runtime exports SSL_CERT_FILE (the bundle's CA list, frozen when it was built),
    TCL_LIBRARY, TK_LIBRARY and, on hosts without fontconfig, FONTCONFIG_FILE. Every program the app started inherited
    them: yt-dlp, ffmpeg, zenity, xdg-open and what those start in turn (a file manager, a terminal opened from it),
    which then trusted the bundle's CA list instead of the host's, which gets updates."""

    BUNDLE_VARS = {
        "SSL_CERT_FILE": os.path.join(sys.prefix, "etc", "cacert.pem"),
        "TCL_LIBRARY": os.path.join(sys.prefix, "lib", "tcl8.6"),
        "TK_LIBRARY": os.path.join(sys.prefix, "lib", "tk8.6"),
        "FONTCONFIG_FILE": os.path.join(sys.prefix, "etc", "fonts.conf"),
    }

    def setUp(self):
        super().setUp()
        self.bindir = os.path.join(self.base, "bin")
        os.makedirs(self.bindir)
        self.dump = os.path.join(self.base, "env.txt")
        patcher = mock.patch.dict(os.environ, {**self.BUNDLE_VARS,
                                               "PATH": self.bindir + os.pathsep + os.environ["PATH"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_host_program(self, name: str, prints: str = ""):
        """*name* on PATH writes its environment to self.dump (complete when it appears) and prints *prints*."""
        path = os.path.join(self.bindir, name)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nenv > "{self.dump}.tmp" && mv "{self.dump}.tmp" "{self.dump}"\necho "{prints}"\n')
        os.chmod(path, 0o755)

    def inherited(self, env_text: str) -> list[str]:
        lines = set(env_text.splitlines())
        return [k for k, v in self.BUNDLE_VARS.items() if f"{k}={v}" in lines]

    def test_yt_dlp_and_ffmpeg_do_not_inherit_them(self):
        out = ProcessManager.run(["env"], stdout=subprocess.PIPE, text=True).stdout     # how every tool is started
        self.assertEqual(self.inherited(out), [])

    def test_a_value_the_user_set_is_kept(self):
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/etc/ssl/company-ca.pem"}):
            out = ProcessManager.run(["env"], stdout=subprocess.PIPE, text=True).stdout
        self.assertIn("SSL_CERT_FILE=/etc/ssl/company-ca.pem", out.splitlines())

    def test_the_file_manager_does_not_inherit_them(self):
        from xdownloader import gui
        self.fake_host_program("xdg-open")
        gui.SettingsDialog._on_open_folder(mock.Mock())             # the "Open" button of the download folder
        deadline = time.time() + 10
        while not os.path.exists(self.dump) and time.time() < deadline:
            time.sleep(0.05)
        with open(self.dump) as f:
            self.assertEqual(self.inherited(f.read()), [])

    @unittest.skipUnless(HAS_DISPLAY, "needs a display")
    def test_the_folder_chooser_does_not_inherit_them(self):
        import tkinter as tk
        from xdownloader import gui
        root = tk.Tk()
        self.addCleanup(root.destroy)
        self.fake_host_program("zenity", prints="/chosen/dir")
        self.assertEqual(gui._ask_directory(root, self.base, "x"), "/chosen/dir")
        with open(self.dump) as f:
            self.assertEqual(self.inherited(f.read()), [])


class TestOrphanSweep(Base):
    """After SIGKILL / out-of-memory the GUI is gone but its yt-dlp keeps downloading; a restarted GUI would
    start a second yt-dlp on the same .part file."""

    def setUp(self):
        super().setUp()
        write_fake_ytdlp("sleep 30")
        self.procs = []
        self.addCleanup(self.reap)

    def reap(self):
        for p in self.procs:
            if p.poll() is None:
                os.killpg(p.pid, 9)                                   # bash and its sleep
                p.wait()

    def spawn(self, command=None, **kw):
        p = subprocess.Popen(command or [Config.YT_DLP_EXE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.procs.append(p)
        time.sleep(0.2)                                               # let bash exec the script
        return p

    def test_leftover_download_is_killed(self):
        orphan = self.spawn(start_new_session=True)                   # how ProcessManager starts its children
        self.assertEqual(ProcessManager.kill_orphans(Config.YT_DLP_EXE), 1)
        self.assertLess(orphan.wait(5), 0)                            # ended by a signal

    def test_a_download_started_through_the_dynamic_loader_is_found_too(self):
        # Alpine and NixOS: yt-dlp is a script, and its program yt-dlp.bin runs as `ld.so --library-path DIR program`
        # (measured with /proc/<pid>/cmdline), so the program is not among the first two words of the command line.
        # "; true" keeps bash from replacing itself by sleep, which would drop the words that matter here.
        program = Config.YT_DLP_EXE + ".bin"
        orphan = self.spawn(["bash", "-c", "sleep 30; true", "ld.so", "--library-path", self.base, program],
                            start_new_session=True)
        self.assertEqual(ProcessManager.kill_orphans(Config.YT_DLP_EXE, program), 1)
        self.assertLess(orphan.wait(5), 0)

    def test_other_processes_are_left_alone(self):
        manual = self.spawn(preexec_fn=os.setpgrp)                    # a job of the user's shell: not a session leader
        self.assertEqual(ProcessManager.kill_orphans(Config.YT_DLP_EXE), 0)
        self.assertTrue(process_alive(manual.pid))


class TestStoppedChildrenGetToCleanUp(Base):
    """The Stop button, closing the window, a timeout and the orphan sweep ended children with SIGKILL at once. yt-dlp
    is a self-unpacking program: on SIGKILL its unpacked copy (about 85 MiB, measured) stays in the temp folder, one per
    Stop or close; two were found in the real /tmp. On SIGTERM it removes the copy and ends within 0.03 s (measured)."""

    def setUp(self):
        super().setUp()
        ProcessManager.clear_cancel()
        self.addCleanup(ProcessManager.clear_cancel)
        self.marker = os.path.join(self.base, "cleaned up")
        self.ready = os.path.join(self.base, "ready")

    def child(self, on_term: str) -> str:
        """A shell script that runs *on_term* on SIGTERM, as a self-unpacking program cleans up, and otherwise waits."""
        return f'trap \'{on_term}\' TERM; echo > "{self.ready}"; sleep 30 & wait'

    def start(self, cmd, ready=None, **kw):
        """Runs *cmd* through ProcessManager on a thread, as a download does; returns once ready() says so."""
        outcome = {}

        def work():
            try:
                ProcessManager.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
                outcome["r"] = "returned"
            except ShutdownRequested as e:
                outcome["r"] = type(e).__name__
        thread = threading.Thread(target=work)
        thread.start()
        ready = ready or (lambda: os.path.exists(self.ready))
        deadline = time.time() + 30
        while not ready() and time.time() < deadline:
            time.sleep(0.02)
        return thread, outcome

    def test_stop_lets_the_child_clean_up(self):
        thread, outcome = self.start(["sh", "-c", self.child(f'echo > "{self.marker}"; exit 1')])
        ProcessManager.cancel()
        thread.join(15)
        self.assertEqual(outcome, {"r": "CancelRequested"})
        self.assertTrue(os.path.exists(self.marker))

    def test_closing_lets_the_child_clean_up(self):
        thread, outcome = self.start(["sh", "-c", self.child(f'echo > "{self.marker}"; exit 1')])
        ProcessManager.kill_all()
        thread.join(15)
        self.assertEqual(outcome, {"r": "ShutdownRequested"})
        self.assertTrue(os.path.exists(self.marker))

    def test_a_timeout_lets_the_child_clean_up(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            ProcessManager.run(["sh", "-c", self.child(f'echo > "{self.marker}"; exit 1')], timeout=1,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(os.path.exists(self.marker))

    def test_the_orphan_sweep_lets_a_leftover_clean_up(self):
        write_fake_ytdlp(self.child(f'echo > "{self.marker}"; exit 1'))
        orphan = subprocess.Popen([Config.YT_DLP_EXE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        self.addCleanup(lambda: orphan.poll() is None and os.killpg(orphan.pid, signal.SIGKILL))
        deadline = time.time() + 10
        while not os.path.exists(self.ready) and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(ProcessManager.kill_orphans(Config.YT_DLP_EXE), 1)
        orphan.wait(5)
        self.assertTrue(os.path.exists(self.marker))

    def test_a_child_that_ends_with_0_on_sigterm_is_still_stopped_not_a_success(self):
        thread, outcome = self.start(["sh", "-c", self.child(f'echo > "{self.marker}"; exit 0')])
        ProcessManager.cancel()
        thread.join(15)
        self.assertEqual(outcome, {"r": "CancelRequested"})

    def test_a_child_that_ignores_sigterm_is_killed_all_the_same(self):
        thread, outcome = self.start(["sh", "-c", self.child("")])      # trap '' TERM: its sleep ignores it too
        started = time.time()
        ProcessManager.cancel()
        thread.join(15)
        self.assertEqual(outcome, {"r": "CancelRequested"})
        self.assertLess(time.time() - started, 10)

    @unittest.skipUnless(os.path.exists(REAL_YTDLP), "real yt-dlp not present")
    def test_a_yt_dlp_stopped_while_it_unpacks_itself_leaves_nothing_either(self):
        # In its first ~0.4 s yt-dlp is still unpacking itself and dies of SIGTERM without removing the part it has
        # unpacked (measured: 12 to 69 MiB). Every attempt starts with that window, so a Stop could hit it.
        tmp = tempfile.mkdtemp(prefix="tmp_", dir=self.base)

        def unpacking():
            return any(name.startswith("_MEI") for _, dirs, _ in os.walk(tmp) for name in dirs)
        thread, outcome = self.start([REAL_YTDLP, "--ignore-config", "--version"], ready=unpacking,
                                     env=dict(host_env(), TMPDIR=tmp))
        self.assertTrue(unpacking(), "yt-dlp never began to unpack itself")
        ProcessManager.cancel()                     # at once: it is still unpacking
        thread.join(15)
        self.assertEqual(outcome, {"r": "CancelRequested"})
        self.assertEqual(os.listdir(tmp), [])

    def test_leftovers_of_a_killed_earlier_run_are_removed_at_the_next_start(self):
        tmp = tempfile.mkdtemp(prefix="tmp_", dir=self.base)
        with mock.patch.object(tempfile, "tempdir", tmp):
            ours = os.path.join(tmp, ProcessManager.tmpdir_prefix() + "k1ll3d00")
            os.makedirs(os.path.join(ours, "_MEIabc123"))           # half unpacked, its program killed with us
            other_project = os.path.join(tmp, "xdl-run-0000000000-r0nn1ng0")
            os.makedirs(other_project)                              # another copy of the program, maybe running
            ProcessManager.remove_leftover_tmpdirs()
        self.assertEqual(sorted(os.listdir(tmp)), [os.path.basename(other_project)])

    @unittest.skipUnless(os.path.exists(REAL_YTDLP), "real yt-dlp not present")
    def test_a_stopped_yt_dlp_leaves_nothing_in_the_temp_folder(self):
        tmp = tempfile.mkdtemp(prefix="tmp_", dir=self.base)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)                          # takes the connection and never answers: yt-dlp waits
        self.addCleanup(listener.close)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/clip.mp4"
        thread, outcome = self.start([REAL_YTDLP, "--ignore-config", "--socket-timeout", "60", "--", url],
                                     ready=lambda: bool(os.listdir(tmp)), env=dict(host_env(), TMPDIR=tmp))
        self.assertTrue(os.listdir(tmp), "yt-dlp never unpacked itself")
        time.sleep(1.5)                             # unpacked, started, connected
        ProcessManager.cancel()
        thread.join(15)
        self.assertEqual(outcome, {"r": "CancelRequested"})
        self.assertEqual(os.listdir(tmp), [])


class TestLockManagerReentrancy(Base):
    """LockManager.acquire() called again in the same process: no file descriptor leaks, a deleted lock file is taken
    again, and a second instance is still kept out."""

    # --- 1. System: LockManager Inode-aware Re-entrancy ---
    def test_lock_manager_reentrancy_no_fd_leak(self):
        """Verify repeated acquire() does not leak file descriptors."""
        lm = LockManager(self.log)
        self.assertTrue(lm.acquire())
        initial_fd = lm._lock_file.fileno()

        # Call acquire 200 times
        for _ in range(200):
            self.assertTrue(lm.acquire())
            # Must reuse same file descriptor without opening new ones
            self.assertEqual(lm._lock_file.fileno(), initial_fd)

        lm.release()
        self.assertIsNone(lm._lock_file)
        self.assertFalse(os.path.exists(Config.LOCK_FILE))

    def test_lock_manager_reentrancy_handles_unlinked_file(self):
        """Verify re-entrant acquire detects unlinked lock file and re-acquires cleanly."""
        lm = LockManager(self.log)
        self.assertTrue(lm.acquire())

        # Simulate external process unlinking the lock file
        os.remove(Config.LOCK_FILE)

        # Re-entrant acquire must detect inode change/missing, close orphan fd, and create new lock
        self.assertTrue(lm.acquire())
        self.assertTrue(os.path.exists(Config.LOCK_FILE))
        # Must have re-opened a fresh file
        self.assertTrue(os.fstat(lm._lock_file.fileno()).st_ino == os.stat(Config.LOCK_FILE).st_ino)
        lm.release()

    def test_lock_manager_cross_instance_exclusion(self):
        """Verify lock exclusion between distinct instances is strictly preserved."""
        lm1 = LockManager(self.log)
        lm2 = LockManager(self.log)

        self.assertTrue(lm1.acquire())
        # lm2 must fail while lm1 holds it
        self.assertFalse(lm2.acquire())

        lm1.release()
        # After lm1 releases, lm2 can acquire
        self.assertTrue(lm2.acquire())
        lm2.release()


if __name__ == "__main__":
    unittest.main()
