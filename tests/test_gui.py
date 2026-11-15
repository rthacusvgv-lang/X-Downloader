"""
GUI integration tests: drive the real CustomTkinter App (needs a display).

Run from the project root:
    .runtime/$(uname -m)/bin/python -m unittest discover -s tests -v
Skipped automatically when no DISPLAY / WAYLAND_DISPLAY is available.
"""
import logging
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tkinter as tk
import unittest
import uuid
from unittest import mock

from common import (
    BY_HOST, Base, Config, LockManager, ProcessManager, SANDBOX, atomic_write_lines, install_fake_tools,
    point_config_to, process_alive, read_calls, read_lines, reset_shutdown, sites, write_fake_ytdlp,
)

HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
if HAS_DISPLAY:
    from xdownloader import gui


def new_base():
    """Fresh isolated project directory with fake yt-dlp/ffmpeg and a clean app logger."""
    base = tempfile.mkdtemp(prefix="gui_", dir=SANDBOX)
    point_config_to(base)
    install_fake_tools()
    Config.RETRY_DELAY = 0
    lg = logging.getLogger("XDownloader")
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()
    reset_shutdown()
    return base


def pump(app, cond=lambda: False, timeout=5.0):
    """Runs the Tk event loop until cond() is true or the timeout expires."""
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def no_dialog_show(self):
    """Replacement for CustomMessageBox.show(): answers 'Yes' without blocking."""
    self.destroy()
    return True


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


# A real App in a sandbox (argv: base dir, tests dir, mode) that starts one download whose fake yt-dlp sleeps.
GUI_CHILD = textwrap.dedent('''\
    import os, signal, sys
    sys.path.insert(0, sys.argv[2])
    from common import Config, install_fake_tools, point_config_to
    base, mode = sys.argv[1], sys.argv[3]
    if mode == "ignore-sigint":
        signal.signal(signal.SIGINT, signal.SIG_IGN)     # like a background job started by a script
    point_config_to(base)
    install_fake_tools()
    with open(Config.YT_DLP_EXE, "w") as f:
        f.write("#!/usr/bin/env bash\\n"
                "case \\" $* \\" in *\\" -U \\"*) exit 0 ;; esac\\n"
                "echo $$ > " + base + "/ytdlp.pid\\n"
                "exec sleep 60\\n# pad " + "x" * 2048 + "\\n")
    os.chmod(Config.YT_DLP_EXE, 0o755)
    from xdownloader import gui
    app = gui.App()
    print("SIGINT:", "ignored" if signal.getsignal(signal.SIGINT) is signal.SIG_IGN else "handled", flush=True)
    def start():
        if app._updating:
            app.after(50, start)
            return
        app._listbox.insert("1.0", "https://x.com/u/status/1\\n")
        app._on_start_download()
    app.after(50, start)
    app.mainloop()
''')


@unittest.skipUnless(HAS_DISPLAY, "no display available")
class TestGui(unittest.TestCase):
    app = None

    def tearDown(self):
        # make sure a failed test does not leave a window / lock behind
        if self.app is not None:
            try:
                if not self.app._closing:
                    ProcessManager.kill_all()
                    self.app._lock.release()
                    self.app._shutdown_ui()
            except Exception:
                pass
            self.app = None
        reset_shutdown()

    def check(self, cond, msg):
        with self.subTest(msg):
            self.assertTrue(cond, msg)

    def listbox_text(self):
        return self.app._listbox.get("1.0", "end-1c")

    # ------------------------------------------------------------------
    def test_startup_ui_and_download_run(self):
        base = new_base()
        atomic_write_lines(Config.PROCESSING_FILE, ["https://x.com/a/status/1", "https://x.com/b/status/2"])
        atomic_write_lines(Config.LIST_FILE, ["https://x.com/b/status/2", "https://x.com/c/status/3"])

        self.app = app = gui.App()
        self.check(pump(app, lambda: app._start_btn.cget("state") == "normal", 10),
                   "startup update finishes and re-enables the Download button")
        self.check(app._start_btn.cget("text") == "Download", "button label restored after update")
        self.check(self.listbox_text().split() == [
            "https://x.com/a/status/1", "https://x.com/b/status/2", "https://x.com/c/status/3"],
            "crash-resume queue merged with list.txt (deduplicated)")
        self.check(not os.path.exists(Config.PROCESSING_FILE),
                   ".processing.list handed over (no stale resurrection)")
        self.check(read_lines(Config.LIST_FILE) == self.listbox_text().split(),
                   "list.txt persisted with merged list")
        self.check("3 valid" in app._count_label.cget("text"), "count label shows 3")
        self.check(not LockManager(logging.getLogger("t")).acquire(), "lock held by running App")

        # context-menu paste: consecutive pastes stay on separate lines
        app._on_clear_list()
        app.clipboard_clear()
        app.clipboard_append("https://x.com/p/status/10")
        app._on_context_paste()
        app._on_context_paste()            # duplicate -> counted once
        app.clipboard_clear()
        app.clipboard_append("https://x.com/q/status/11")
        app._on_context_paste()
        pump(app, timeout=0.2)
        lines = [ln for ln in self.listbox_text().splitlines() if ln.strip()]
        self.check(lines == ["https://x.com/p/status/10", "https://x.com/p/status/10",
                             "https://x.com/q/status/11"], f"pasted URLs on separate lines: {lines}")
        self.check("2 valid" in app._count_label.cget("text"), "duplicates counted once")

        # log records emitted from a worker thread arrive through the main-loop queue
        threading.Thread(target=lambda: logging.getLogger("XDownloader").warning("from-worker-thread")).start()
        self.check(pump(app, lambda: "from-worker-thread" in app._log_box.get("1.0", "end"), 3),
                   "worker-thread log shown in log box")

        # expanded log window mirrors new records and detaches cleanly on close
        app._on_expand_log()
        pump(app, timeout=0.3)
        win = app._log_window
        self.check(win is not None and win.winfo_exists(), "expanded log window opened")
        logging.getLogger("XDownloader").info("mirrored-line")
        expanded = app._gui_handler.textboxes[-1]
        self.check(pump(app, lambda: "mirrored-line" in expanded.get("1.0", "end"), 3),
                   "expanded window receives logs")
        app.tk.call(win.protocol("WM_DELETE_WINDOW"))
        pump(app, timeout=0.3)
        self.check(len(app._gui_handler.textboxes) == 1 and app._log_window is None,
                   "expanded window detached on close")

        # settings button and dialog test
        self.check(app._settings_btn.cget("text") == "Settings", "settings button exists and labeled Settings")
        app._on_settings()
        pump(app, timeout=0.3)
        dlg = app._settings_window
        self.check(dlg is not None and dlg.winfo_exists(), "settings dialog opened")
        test_dir = os.path.join(base, "new_downloads")
        dlg._dir_entry.delete(0, "end")
        dlg._dir_entry.insert(0, test_dir)
        dlg._on_save()
        pump(app, timeout=0.3)
        self.check(Config.DOWNLOAD_DIR == os.path.abspath(test_dir), "Config.DOWNLOAD_DIR updated via settings dialog")
        self.check(os.path.isdir(test_dir), "new download directory created")
        self.check(os.path.exists(Config.SETTINGS_FILE), "settings.json persisted")

        # test browse uses native system dialog (zenity) and respects cancellation
        browsed_dir = os.path.join(base, "browsed_downloads")
        with mock.patch("xdownloader.gui.shutil.which", side_effect=lambda cmd: "/usr/bin/zenity" if cmd == "zenity" else None), \
             mock.patch("xdownloader.gui.subprocess.run") as m_run, \
             mock.patch.object(gui.DirectoryChooserDialog, "show", return_value="/custom/fallback") as m_dark_dlg:
            m_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=browsed_dir + "\n")
            dlg._on_browse()
            self.check(m_run.called and m_run.call_args[0][0][:3] == ["zenity", "--file-selection", "--directory"],
                       "browse invokes native zenity directory chooser")
            self.check(not m_dark_dlg.called, "Dark DirectoryChooserDialog not called when zenity succeeds")
            self.check(Config.DOWNLOAD_DIR == os.path.abspath(browsed_dir), "browsed directory applied")

            # user cancels zenity -> returncode 1 -> does not fall back to dark dialog or change path
            m_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
            dlg._on_browse()
            self.check(not m_dark_dlg.called, "cancelling zenity does not trigger dark dialog fallback")
            self.check(Config.DOWNLOAD_DIR == os.path.abspath(browsed_dir), "cancelled browse keeps existing path")

        # test fallback to custom dark DirectoryChooserDialog when system tools are unavailable
        fallback_dir = os.path.join(base, "dark_fallback_dir")
        with mock.patch("xdownloader.gui.shutil.which", return_value=None), \
             mock.patch.object(gui.DirectoryChooserDialog, "show", return_value=fallback_dir) as m_dark_dlg:
            dlg._on_browse()
            self.check(m_dark_dlg.called, "custom dark DirectoryChooserDialog called when no system tools exist")
            self.check(Config.DOWNLOAD_DIR == os.path.abspath(fallback_dir), "fallback dark dialog directory applied")

        # test reset to default in settings dialog
        app._on_settings()
        pump(app, timeout=0.3)
        dlg = app._settings_window
        dlg._on_reset()
        self.check(dlg._dir_entry.get() == Config.BASE_PATH, "reset button sets path back to default")
        dlg.destroy()

        # full download run: one success, one non-retryable failure
        app._on_clear_list()
        app._listbox.insert("end", "https://x.com/ok/status/20\nhttps://x.com/gone/status/21\n")
        app._refresh_count()
        app._on_start_download()
        self.check(app._listbox.cget("state") == "disabled", "list disabled during download")
        self.check(app._settings_btn.cget("state") == "disabled", "settings disabled during download")
        self.check(pump(app, lambda: not app._is_downloading, 20), "download finished")
        self.check(app._listbox.cget("state") == "normal", "list re-enabled after download")
        self.check(self.listbox_text().strip() == "", "list cleared after successful batch")
        self.check(read_lines(Config.FAILED_FILE) == ["https://x.com/gone/status/21"],
                   "failed URL recorded")
        self.check("Task complete" in app._log_box.get("1.0", "end"), "completion logged in GUI")

        # modal dialog: grab works (no "grab failed") and the result is returned
        def close_box():
            for w in app.winfo_children():
                if isinstance(w, gui.CustomMessageBox):
                    w._on_yes()
        app.after(400, close_box)
        res = gui.CustomMessageBox(app, "T", "msg", mode="askyesno").show()
        self.check(res is True, "CustomMessageBox shows and returns result (no grab error)")

        # normal close saves the list and releases the lock
        app._listbox.insert("end", "https://x.com/keep/status/30\n")
        app._on_closing()
        self.check(read_lines(Config.LIST_FILE) == ["https://x.com/keep/status/30"], "list saved on close")
        self.check(not os.path.exists(Config.LOCK_FILE), "lock released on close")
        with self.assertRaises(tk.TclError, msg="window destroyed on close"):
            app.winfo_exists()

    # ------------------------------------------------------------------
    def test_force_close_mid_download(self):
        base = new_base()
        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)
        app._listbox.insert("end", "https://x.com/slow/status/40\nhttps://x.com/z/status/41\n")
        app._on_start_download()
        self.assertTrue(pump(app, lambda: any("slow/status/40" in c for c in read_calls(base)), 10),
                        "slow download did not start")

        orig_show = gui.CustomMessageBox.show
        gui.CustomMessageBox.show = no_dialog_show      # auto-confirm the "really exit?" dialog
        try:
            t0 = time.time()
            app._on_closing()
        finally:
            gui.CustomMessageBox.show = orig_show
        self.check(time.time() - t0 < 3, "force close is immediate")
        time.sleep(0.5)
        self.check(read_lines(Config.PROCESSING_FILE) ==
                   ["https://x.com/slow/status/40", "https://x.com/z/status/41"],
                   "resume queue kept after forced close")
        self.check(not os.path.exists(Config.FAILED_FILE), "interrupted URL not marked failed")
        n_calls = len(read_calls(base))
        self.check(n_calls == 2, f"no yt-dlp spawned after close (calls={n_calls}: update + 1 download)")

    # ------------------------------------------------------------------
    def test_second_instance_refused(self):
        new_base()
        holder = LockManager(logging.getLogger("t"))
        self.assertTrue(holder.acquire())
        orig_show = gui.CustomMessageBox.show
        gui.CustomMessageBox.show = no_dialog_show
        try:
            with self.assertRaises(SystemExit) as cm:
                gui.App()
            self.assertEqual(cm.exception.code, 1)
        finally:
            gui.CustomMessageBox.show = orig_show
            holder.release()

    def test_log_handler_limits_history(self):
        new_base()
        self.app = app = gui.App()
        handler = app._gui_handler
        for i in range(handler.MAX_LINES + 300):
            logging.getLogger("XDownloader").info("line-%d", i)
        pump(app, lambda: "line-%d" % (handler.MAX_LINES + 299) in app._log_box.get("1.0", "end"), 10)
        n_lines = int(float(app._log_box.index("end-1c")))
        self.check(n_lines <= handler.MAX_LINES, f"log box capped ({n_lines} lines)")
        self.check("line-%d" % (handler.MAX_LINES + 299) in app._log_box.get("1.0", "end"),
                   "newest line kept after trimming")

    def test_language_switch_and_persistence_across_restart(self):
        base = new_base()
        self.app = app = gui.App()
        self.check(pump(app, lambda: app._start_btn.cget("state") == "normal", 10),
                   "startup update finishes")

        # 1. Default UI is English
        self.check(Config.LANGUAGE == "en", "default language is en")
        self.check(app._start_btn.cget("text") == "Download", "default start button in English")
        self.check(app._settings_btn.cget("text") == "Settings", "default settings button in English")
        self.check("X-Downloader GUI started." in app._log_box.get("1.0", "end"), "default startup log in English")

        # 2. Open Settings, adjust directory, retries, format, and switch language to Chinese
        app._on_settings()
        pump(app, timeout=0.3)
        dlg = app._settings_window
        self.check(dlg._lang_opt.get() == "English", "language selector initially shows English")

        custom_dir = os.path.join(base, "persistent_dl")
        dlg._dir_entry.delete(0, "end")
        dlg._dir_entry.insert(0, custom_dir)
        # Switch language to Chinese (also commits pending directory)
        dlg._on_language_change("繁體中文")
        pump(app, timeout=0.3)

        self.check(Config.LANGUAGE == "zh_TW", "Config.LANGUAGE updated to zh_TW")
        self.check(Config.DOWNLOAD_DIR == os.path.abspath(custom_dir), "pending directory committed on language change")
        self.check(dlg.title() == "設定", "settings dialog title in Chinese")
        self.check(dlg._nav_buttons["General"].cget("text") == "一般", "sidebar General tab in Chinese")
        self.check(dlg._nav_buttons["Advanced"].cget("text") == "進階", "sidebar Advanced tab in Chinese")
        self.check(dlg._nav_buttons["About"].cget("text") == "關於", "sidebar About tab in Chinese")
        self.check(app._start_btn.cget("text") == "開始下載", "main start button updated to Chinese")
        self.check(app._settings_btn.cget("text") == "設定", "main settings button updated to Chinese")
        self.check(app._clear_btn.cget("text") == "清空清單", "main clear button updated to Chinese")
        self.check(app._log_label.cget("text") == "執行日誌", "log header updated to Chinese")
        self.check("下載佇列" in app._count_label.cget("text"), "queue count label in Chinese")

        # 3. Execution log re-rendered existing logs & shows new logs in Chinese
        log_text = app._log_box.get("1.0", "end")
        self.check("X-Downloader GUI 已啟動。" in log_text, "existing startup log translated to Chinese")
        self.check("介面語言已設定為：繁體中文" in log_text, "language change logged in Chinese")

        # Check About tab: verify subtitles are removed and row titles & badges are aligned
        dlg._show_tab("About")
        pump(app, timeout=0.2)
        card = [w for w in dlg._content_container.winfo_children() if isinstance(w, gui.ctk.CTkFrame)][-1]
        about_rows = [w for w in card.winfo_children() if isinstance(w, gui.ctk.CTkFrame) and w.cget("height") != 1]
        self.check(len(about_rows) == 4, "About tab has 4 rows")
        expected_about = [("版本", "1.0"), ("yt-dlp 引擎", "已就緒"), ("FFmpeg 轉檔器", "已就緒"), ("執行環境", "Linux")]
        for idx, (exp_title, exp_badge) in enumerate(expected_about):
            r = about_rows[idx]
            left_frame, badge_label = r.winfo_children()[0], r.winfo_children()[1]
            title_label = left_frame.winfo_children()[0]
            self.check(len(left_frame.winfo_children()) == 1, f"row {idx} has no subtitle label")
            self.check(title_label.cget("text") == exp_title, f"row {idx} title is {exp_title}")
            self.check(badge_label.cget("text") == exp_badge, f"row {idx} badge is {exp_badge}")
            self.check(title_label.winfo_rooty() == badge_label.winfo_rooty(), f"row {idx} title and badge vertically aligned")

        # Also change Advanced settings and close Settings
        dlg._show_tab("Advanced")
        dlg._on_retry_change("5")
        dlg._on_format_change("mkv")
        dlg._on_close()
        pump(app, timeout=0.2)

        # Run a download in Chinese mode and check Chinese execution logs
        app._listbox.insert("end", "https://x.com/ok/status/88\n")
        app._refresh_count()
        app._on_start_download()
        self.check(pump(app, lambda: not app._is_downloading, 20), "download finished in Chinese mode")
        log_text_after = app._log_box.get("1.0", "end")
        self.check("開始執行下載任務" in log_text_after, "batch start logged in Chinese")
        self.check("下載成功" in log_text_after, "download success logged in Chinese")
        self.check("任務完成" in log_text_after, "task completion logged in Chinese")

        # 4. Close GUI, tamper with in-memory Config, and reopen GUI -> settings must persist
        app._on_closing()
        self.app = None
        reset_shutdown()

        Config.LANGUAGE = "en"
        Config.DOWNLOAD_DIR = base
        Config.MAX_RETRIES = 1
        Config.MERGE_FORMAT = "mp4"

        self.app = app2 = gui.App()
        self.check(pump(app2, lambda: app2._start_btn.cget("state") == "normal", 10),
                   "reopened app startup update finishes")
        self.check(Config.LANGUAGE == "zh_TW", "language setting persisted after restart")
        self.check(Config.DOWNLOAD_DIR == os.path.abspath(custom_dir), "download_dir persisted after restart")
        self.check(Config.MAX_RETRIES == 5, "max_retries persisted after restart")
        self.check(Config.MERGE_FORMAT == "mkv", "merge_format persisted after restart")
        self.check(app2._start_btn.cget("text") == "開始下載", "reopened app button remains in Chinese")
        self.check("X-Downloader GUI 已啟動。" in app2._log_box.get("1.0", "end"),
                   "reopened app execution log starts in Chinese")
        app2._on_closing()
        self.app = None

    def test_custom_directory_chooser_dialog(self):
        base = new_base()
        sub1 = os.path.join(base, "folder_a")
        sub2 = os.path.join(base, "folder_b")
        os.makedirs(sub1, exist_ok=True)
        os.makedirs(sub2, exist_ok=True)

        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)

        # 1. Dialog appearance and dark theme matching UI
        cdlg = gui.DirectoryChooserDialog(app, initialdir=base)
        self.check(cdlg.cget("fg_color") == gui.Theme.BG, "dialog background matches Theme.BG")
        self.check(cdlg._list_frame.cget("fg_color") == gui.Theme.BOX_BG, "list background matches Theme.BOX_BG")
        pump(app, timeout=0.2)

        # 2. Directory contents listed
        folder_names = [b.cget("text") for b in cdlg._folder_buttons]
        self.check(any("folder_a" in n for n in folder_names), "folder_a listed in dialog")
        self.check(any("folder_b" in n for n in folder_names), "folder_b listed in dialog")

        # 3. Navigate into subfolder
        cdlg._navigate_to(sub1)
        self.check(cdlg._current_path == sub1, "navigated into folder_a")
        self.check(cdlg._path_entry.get() == sub1, "path entry reflects folder_a")

        # 4. Navigate up
        cdlg._on_up()
        self.check(cdlg._current_path == base, "navigated up back to base")

        # 5. Select directory
        cdlg._on_select()
        self.check(cdlg.result == base, "selected base directory confirmed")
        cdlg.destroy()

        # 6. Cancellation returns empty string
        cdlg2 = gui.DirectoryChooserDialog(app, initialdir=base)
        pump(app, timeout=0.2)
        cdlg2._on_cancel()
        self.check(cdlg2.result == "", "cancelling custom dialog returns empty string")
        cdlg2.destroy()


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class TestTerminationSignals(unittest.TestCase):
    """kill / pkill / shutdown (SIGTERM) and Ctrl+C ended the GUI at once while yt-dlp, in its own session,
    kept downloading; the next launch then resumed the same URL next to that orphan."""

    def start_app(self, mode="default"):
        base = tempfile.mkdtemp(prefix="signal_", dir=SANDBOX)
        child = subprocess.Popen([sys.executable, "-c", GUI_CHILD, base, TESTS_DIR, mode],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        pidfile = os.path.join(base, "ytdlp.pid")
        deadline = time.time() + 30
        while not os.path.exists(pidfile) and time.time() < deadline and child.poll() is None:
            time.sleep(0.1)
        time.sleep(0.3)
        if not os.path.exists(pidfile):
            self.fail(f"the download never started: {child.communicate(timeout=5)[1][-2000:]}")
        with open(pidfile) as f:
            ytdlp = int(f.read())
        self.addCleanup(lambda: process_alive(ytdlp) and os.kill(ytdlp, signal.SIGKILL))
        return base, child, ytdlp

    def assert_closed_cleanly(self, base, child, ytdlp):
        _, err = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 0, err[-2000:])
        deadline = time.time() + 3
        while process_alive(ytdlp) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_alive(ytdlp), "yt-dlp kept running as an orphan")
        self.assertTrue(os.path.exists(os.path.join(base, ".processing.list")))   # resumed on the next launch

    def test_sigterm_and_ctrl_c_stop_the_download(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                base, child, ytdlp = self.start_app()
                child.send_signal(sig)
                self.assert_closed_cleanly(base, child, ytdlp)

    def test_an_ignored_sigint_stays_ignored(self):
        # start_gui.sh starts the app in the background, where SIGINT is ignored: keep it that way.
        base, child, ytdlp = self.start_app("ignore-sigint")
        self.assertEqual(child.stdout.readline().strip(), "SIGINT: ignored")
        child.send_signal(signal.SIGTERM)
        self.assert_closed_cleanly(base, child, ytdlp)


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class TestStaleTempFilesOfTheOtherFiles(Base):
    """Only settings.json had the leftovers of a killed write (temp file written, rename not done) cleaned up. Those of
    list.txt, .processing.list, failed_list.txt and the cookies file piled up next to the program for good; the last one
    holds a session token."""

    def temp_file(self, name, age=3600.0):
        path = os.path.join(self.base, f"{name}.{uuid.uuid4().hex}.tmp")
        with open(path, "w") as f:
            f.write("x")
        mtime = time.time() - age
        os.utime(path, (mtime, mtime))
        return path

    def test_the_app_removes_them_at_startup_and_nothing_else(self):
        from xdownloader import gui
        self.install_fake_tools()
        stale = [self.temp_file(name) for name in ("list.txt", ".processing.list", "failed_list.txt", sites.X.cookies_name)]
        kept = [self.temp_file("list.txt", age=0),                    # a write that is running right now
                self.temp_file("notes.txt"), self.temp_file("downloader.log")]       # not files of ours
        app = gui.App()

        def close():
            try:
                app._on_closing()
            except Exception:
                pass
        self.addCleanup(close)
        self.assertEqual([p for p in stale if os.path.exists(p)], [])
        self.assertEqual([p for p in kept if not os.path.exists(p)], [])


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class TestMessageBoxFitsItsText(Base):
    """The message box was fixed at 400x200: a message of six lines or more (a long folder path in an error) pushed its
    buttons out of the window. The height now follows the text and stays 200 for a short one."""
    LONG_PATH = "/srv/media/archive/" + "/".join(["Videos-from-the-twitter-archive-2026"] * 4)

    def setUp(self):
        super().setUp()
        import customtkinter as ctk
        from xdownloader import gui
        self.ctk, self.gui = ctk, gui
        self.install_fake_tools()
        self.app = gui.App()

        def close():
            try:
                self.app._on_closing()
            except Exception:
                pass
        self.addCleanup(close)

    def box(self, message):
        box = self.gui.CustomMessageBox(self.app, "T", message, mode="askyesno")
        self.addCleanup(box.destroy)
        for _ in range(10):
            box.update()
        return box

    def test_the_buttons_stay_inside_the_window_for_a_long_message(self):
        from xdownloader.i18n import t
        box = self.box(t("msg_dir_bad_name", path=self.LONG_PATH))
        buttons = [w for w in box.winfo_children() if isinstance(w, self.ctk.CTkFrame)][0]
        self.assertGreaterEqual(buttons.winfo_height(), buttons.winfo_reqheight())          # not cut off
        self.assertLessEqual(buttons.winfo_rooty() + buttons.winfo_height(), box.winfo_rooty() + box.winfo_height())
        self.assertGreater(box.winfo_height(), 200)                                         # the window grew

    def test_a_short_message_keeps_the_usual_size(self):
        box = self.box("Done.")
        self.assertEqual((box.winfo_width(), box.winfo_height()), (400, 200))


@unittest.skipUnless(HAS_DISPLAY, "no display available")
class TestStopButton(Base):

    def setUp(self):
        super().setUp()
        self.install_fake_tools()
        write_fake_ytdlp(BY_HOST)
        ProcessManager.clear_cancel()
        self.addCleanup(ProcessManager.clear_cancel)
        from xdownloader import gui
        self.gui = gui

    def pump(self, app, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            app.update()
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    def test_download_button_turns_into_stop(self):
        with mock.patch("xdownloader.env.EnvManager.update_yt_dlp"):
            app = self.gui.App()
        try:
            self.assertTrue(self.pump(app, lambda: not app._updating))
            urls = ["https://x.com/slow/status/1", "https://x.com/ok/status/2"]
            app._listbox.insert("1.0", "\n".join(urls) + "\n")
            app._on_start_download()
            self.assertEqual(app._start_btn.cget("text"), "Stop")
            self.assertEqual(app._start_btn.cget("state"), "normal")
            self.assertEqual(app._listbox.cget("state"), "disabled")
            self.assertTrue(self.pump(app, lambda: len(self.calls()) >= 1))

            app._start_btn.invoke()                                   # the same button now stops the download
            self.assertTrue(self.pump(app, lambda: not app._is_downloading), "download did not stop")
            self.assertEqual(app._get_valid_urls(), urls)             # both URLs are back in the list
            self.assertEqual(app._start_btn.cget("text"), "Download")
            self.assertEqual(app._start_btn.cget("state"), "normal")
            self.assertFalse(os.path.exists(Config.FAILED_FILE))
        finally:
            app._on_closing(confirm=False)
            app.update()


class TestWindowDuringTheStartupUpdate(Base):
    """The window stays usable while yt-dlp is checked for updates at startup, and switching the language then refreshes
    the Download button."""

    def setUp(self):
        super().setUp()
        if not os.environ.get("DISPLAY"):
            self.skipTest("No DISPLAY available for GUI testing.")
        import customtkinter as ctk
        from xdownloader import gui
        self.ctk = ctk
        self.gui = gui

    def test_gui_controls_usable_during_startup_update(self):
        """Verify listbox and other controls remain fully usable while update is in progress."""
        self.install_fake_tools()

        # Make update_yt_dlp block temporarily so we can interact during update
        update_started = threading.Event()
        release_update = threading.Event()

        def mock_update(self_env):
            update_started.set()
            release_update.wait(timeout=5)

        with mock.patch("xdownloader.env.EnvManager.update_yt_dlp", side_effect=mock_update, autospec=True):
            app = self.gui.App()
            try:
                # Wait for update thread to enter mock_update
                update_started.wait(timeout=2)
                self.assertTrue(app._updating)
                self.assertEqual(app._start_btn.cget("state"), "disabled")

                # 1. Listbox MUST be normal and editable
                self.assertEqual(app._listbox.cget("state"), "normal")
                app._listbox.insert("1.0", "https://x.com/user/status/123\n")
                app._refresh_count()
                app.update()
                self.assertEqual(app._get_valid_urls(), ["https://x.com/user/status/123"])

                # 2. Clear button MUST be normal
                self.assertEqual(app._clear_btn.cget("state"), "normal")
                app._on_clear_list()
                app.update()
                self.assertEqual(len(app._get_valid_urls()), 0)

                # 3. Settings button MUST be normal
                self.assertEqual(app._settings_btn.cget("state"), "normal")

                # 4. Starting download MUST be safely blocked while updating
                app._listbox.insert("1.0", "https://x.com/user/status/123\n")
                app._on_start_download()
                self.assertFalse(app._is_downloading)

            finally:
                release_update.set()
                # Wait for update to complete
                end = time.time() + 5
                while app._updating and time.time() < end:
                    app.update()
                    time.sleep(0.05)

                self.assertFalse(app._updating)
                self.assertEqual(app._start_btn.cget("state"), "normal")
                self.assertEqual(app._start_btn.cget("text"), "Download")
                app._on_closing()
                app.update()

    def test_gui_language_refresh_during_and_after_update(self):
        """Verify language switch works smoothly and refreshes start button properly."""
        self.install_fake_tools()
        app = self.gui.App()
        try:
            # Complete startup update
            end = time.time() + 5
            while app._updating and time.time() < end:
                app.update()
                time.sleep(0.05)

            # Switch language to zh_TW
            Config.LANGUAGE = "zh_TW"
            app.refresh_language()
            app.update()
            self.assertEqual(app._start_btn.cget("text"), "開始下載")

            # Switch back to en
            Config.LANGUAGE = "en"
            app.refresh_language()
            app.update()
            self.assertEqual(app._start_btn.cget("text"), "Download")
        finally:
            app._on_closing()
            app.update()


if __name__ == "__main__":
    unittest.main(verbosity=2)
