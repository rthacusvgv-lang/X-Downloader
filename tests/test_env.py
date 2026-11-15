"""env.py: finding, downloading and updating yt-dlp and ffmpeg."""
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from common import Base, Config, DownloaderCore, EnvManager, PROJECT


class TestEnvManager(Base):
    def test_restores_exec_bit(self):
        self.install_fake_tools()
        os.chmod(Config.YT_DLP_EXE, 0o644)
        self.assertTrue(EnvManager(self.log).check_all())
        self.assertTrue(os.access(Config.YT_DLP_EXE, os.X_OK))

    def test_download_failure_is_handled(self):
        # Taken first: on a host with an ffmpeg installed, check_all() switches Config.FFMPEG_EXE to it.
        leftovers = (Config.YT_DLP_EXE + ".tmp", Config.FFMPEG_EXE + ".tmp", Config.FFMPEG_ARCHIVE)
        t0 = time.time()
        self.assertFalse(EnvManager(self.log).check_all())
        self.assertLess(time.time() - t0, 15)
        for leftover in leftovers:
            self.assertFalse(os.path.exists(leftover), leftover)

    def _serve(self):
        """Local HTTP server: /full is complete, /short promises 10000 bytes but sends 3000."""
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                promised = 3000 if self.path == "/full" else 10000
                self.send_response(200)
                self.send_header("Content-Length", str(promised))
                self.end_headers()
                self.wfile.write(b"x" * 3000)   # then the connection is closed

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_truncated_download_is_rejected(self):
        url = self._serve()
        dest = os.path.join(self.base, "dl")
        EnvManager._fetch(url + "/full", dest)
        self.assertEqual(os.path.getsize(dest), 3000)
        with self.assertRaises(OSError):
            EnvManager._fetch(url + "/short", dest)

    def test_truncated_yt_dlp_is_not_installed(self):
        # 3000 bytes would pass the "> 1024 bytes" sanity check and be reused forever
        Config.YT_DLP_URL = self._serve() + "/short"
        self.assertFalse(EnvManager(self.log)._download_yt_dlp())
        self.assertFalse(os.path.exists(Config.YT_DLP_EXE))
        self.assertFalse(os.path.exists(Config.YT_DLP_EXE + ".tmp"))

    def test_update_never_raises(self):
        env = EnvManager(self.log)
        env.update_yt_dlp()                                     # Case 1: missing binary
        with open(Config.YT_DLP_EXE, "w") as f:
            f.write("not executable")
        os.chmod(Config.YT_DLP_EXE, 0o644)
        env.update_yt_dlp()                                     # Case 2: non-executable binary (catches PermissionError)
        with open(Config.YT_DLP_EXE, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(Config.YT_DLP_EXE, 0o755)
        env.update_yt_dlp()                                     # Case 3: execution failure (catches CalledProcessError)


class TestSystemFfmpegFallback(Base):
    """Without a downloadable ffmpeg (unsupported CPU, offline) every batch aborted although an ffmpeg was
    installed on the system, which is exactly what the error message told users to do."""

    def setUp(self):
        super().setUp()
        self.install_fake_tools()
        os.remove(Config.FFMPEG_EXE)
        bindir = os.path.join(self.base, "sysbin")
        os.makedirs(bindir)
        self.system_ffmpeg = os.path.join(bindir, "ffmpeg")
        with open(self.system_ffmpeg, "w") as f:
            f.write("#!/bin/sh\n" + "#" * 2048 + "\n")
        os.chmod(self.system_ffmpeg, 0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_used_when_this_architecture_has_no_download(self):
        Config.FFMPEG_URL = ""
        self.assertTrue(EnvManager(self.log).check_all())
        self.assertEqual(Config.FFMPEG_EXE, self.system_ffmpeg)

    def test_used_when_the_download_fails(self):
        self.assertTrue(EnvManager(self.log).check_all())     # point_config_to() made the URL unreachable
        self.assertEqual(Config.FFMPEG_EXE, self.system_ffmpeg)

    def test_yt_dlp_is_pointed_at_it(self):
        Config.FFMPEG_URL = ""
        EnvManager(self.log).check_all()
        cmd = DownloaderCore(self.log)._base_cmd("https://x.com/u/status/1", [])
        self.assertEqual(cmd[cmd.index("--ffmpeg-location") + 1], os.path.dirname(self.system_ffmpeg))

    def test_the_bundled_location_is_unchanged_otherwise(self):
        self.install_fake_tools()
        self.assertTrue(EnvManager(self.log).check_all())
        cmd = DownloaderCore(self.log)._base_cmd("https://x.com/u/status/1", [])
        self.assertEqual(cmd[cmd.index("--ffmpeg-location") + 1], Config.BASE_PATH)

    def test_still_fails_cleanly_without_any_ffmpeg(self):
        Config.FFMPEG_URL = ""
        with mock.patch("xdownloader.env.shutil.which", return_value=None):
            self.assertFalse(EnvManager(self.log).check_all())


class TestProgramWithoutItsLauncher(Base):
    """Where the system cannot start a downloaded program itself (Alpine, NixOS), env.py keeps it as <name>.bin and puts
    a launcher script under <name>. A leftover .bin without the script (script deleted, or a process killed between the
    two steps) counted as "installed": _ensure() then failed with "not executable: No such file" on every start and
    never fetched the program again."""

    def setUp(self):
        super().setUp()
        from xdownloader.env import BIN_SUFFIX
        self.exe = os.path.join(self.base, "yt-dlp")
        with open(self.exe + BIN_SUFFIX, "wb") as f:
            f.write(b"x" * 4096)
        self.fetched = []

    def ensure(self):
        return EnvManager(self.log)._ensure(self.exe, "yt-dlp", lambda: self.fetched.append(1) or True)

    def test_the_program_without_its_launcher_is_fetched_again(self):
        self.assertTrue(self.ensure())
        self.assertEqual(self.fetched, [1])

    def test_the_program_with_its_launcher_is_accepted(self):
        with open(self.exe, "w") as f:
            f.write("#!/bin/sh\nexec true\n")                            # small, as the real launcher is
        os.chmod(self.exe, 0o755)
        self.assertTrue(self.ensure())
        self.assertEqual(self.fetched, [])


class TestDownloadUrlPerCpu(unittest.TestCase):
    """yt-dlp stopped publishing yt-dlp_linux_armv7l: on 32-bit ARM the first run ended with a 404, and the error
    sent the user to that same dead link. The architecture-independent build runs there instead."""

    def url_on(self, cpu: str) -> str:
        code = (f"import platform\nplatform.machine = lambda: {cpu!r}\n"
                "from xdownloader.config import Config\nprint(Config.YT_DLP_URL)\n")
        return subprocess.run([sys.executable, "-c", code], cwd=PROJECT, stdout=subprocess.PIPE, text=True,
                              check=True).stdout.strip()

    def test_32_bit_arm_gets_the_architecture_independent_build(self):
        for cpu in ("armv7l", "armv6l"):
            self.assertTrue(self.url_on(cpu).endswith("/download/yt-dlp"), cpu)


class TestFfmpegHintFitsEveryDistribution(Base):
    """Without a build for this CPU the program asks for a system ffmpeg, and `apt` only exists on some systems."""

    def test_the_message_names_no_package_manager(self):
        Config.FFMPEG_URL = ""
        with mock.patch("shutil.which", return_value=None), self.assertLogs(self.log, "ERROR") as logs:
            self.assertFalse(EnvManager(self.log)._download_ffmpeg())
        self.assertNotIn("apt", "\n".join(logs.output))


class TestDownloadedToolsMatchTheirChecksums(Base):
    """yt-dlp and ffmpeg were installed and started as soon as a download was complete: nothing compared them with the
    SHA-256 list both projects publish next to the download, so a corrupted or swapped file ran as it came."""

    PROGRAM = b"#!/bin/sh\necho 1.0\n" + b"#" * 2048 + b"\n"

    def serve(self, files: dict) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def checksums(name: str, data: bytes) -> bytes:
        """A list in the usual `sha256sum` format that gives *data*'s digest for *name*."""
        return f"{'0' * 64}  another-file\n{hashlib.sha256(data).hexdigest()}  {name}\n".encode()

    def yt_dlp_with(self, checksums) -> bool:
        files = {"/rel/yt-dlp_linux": self.PROGRAM}
        if checksums is not None:
            files["/rel/SHA2-256SUMS"] = checksums
        Config.YT_DLP_URL = self.serve(files) + "/rel/yt-dlp_linux"
        return EnvManager(self.log)._download_yt_dlp()

    def test_a_listed_download_is_installed(self):
        self.assertTrue(self.yt_dlp_with(self.checksums("yt-dlp_linux", self.PROGRAM)))
        with open(Config.YT_DLP_EXE, "rb") as f:
            self.assertEqual(f.read(), self.PROGRAM)

    def test_a_download_that_differs_from_the_list_is_not_installed(self):
        self.assertFalse(self.yt_dlp_with(self.checksums("yt-dlp_linux", b"another build")))
        self.assertFalse(os.path.exists(Config.YT_DLP_EXE))
        self.assertFalse(os.path.exists(Config.YT_DLP_EXE + ".tmp"))

    def test_a_download_the_list_does_not_name_is_not_installed(self):
        self.assertFalse(self.yt_dlp_with(self.checksums("yt-dlp_linux_aarch64", self.PROGRAM)))
        self.assertFalse(os.path.exists(Config.YT_DLP_EXE))

    def test_without_the_list_nothing_is_installed(self):
        self.assertFalse(self.yt_dlp_with(None))
        self.assertFalse(os.path.exists(Config.YT_DLP_EXE))

    def test_ffmpeg_is_checked_against_its_own_list(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tar:
            entry = tarfile.TarInfo("ffmpeg-master/bin/ffmpeg")
            entry.size = len(self.PROGRAM)
            tar.addfile(entry, io.BytesIO(self.PROGRAM))
        archive = buf.getvalue()
        name = "ffmpeg-master-latest-linux64-gpl.tar.xz"
        with mock.patch("xdownloader.env.shutil.which", return_value=None):     # no system ffmpeg to fall back on
            for listed, installed in ((b"another build", False), (archive, True)):
                Config.FFMPEG_URL = self.serve({f"/latest/{name}": archive,
                                                "/latest/checksums.sha256": self.checksums(name, listed)}) + f"/latest/{name}"
                self.assertEqual(EnvManager(self.log)._download_ffmpeg(), installed)
                self.assertEqual(os.path.exists(Config.FFMPEG_EXE), installed)


if __name__ == "__main__":
    unittest.main()
