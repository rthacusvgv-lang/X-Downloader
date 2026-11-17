"""Dependency checker: makes sure yt-dlp and ffmpeg exist, and updates yt-dlp."""
import glob
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from .config import Config
from .fsutil import atomic_write_text
from .system import ProcessManager, ShutdownRequested

# Where EnvManager._install() puts a program it replaces by a script: "yt-dlp" becomes "yt-dlp.bin".
BIN_SUFFIX = ".bin"

# Takes the place of a program this system cannot start by itself, see EnvManager._install().
# LD_LIBRARY_PATH: yt-dlp restarts itself, and the second start needs to find the libraries too. It also hands the
# variable on to whatever it starts, where our libc would break the system's programs; $ORIGIN is the folder of each
# program, so this path only leads to the libraries from the project folder.
_LOADER_SCRIPT = """#!/bin/sh
here=$(cd "$(dirname "$0")" && pwd -P)
lib="$here/{lib}"
LD_LIBRARY_PATH='$ORIGIN/{lib}' exec "$lib/{loader}" --library-path "$lib" "$here/{program}" "$@"
"""


class EnvManager:
    """Dependency checker and auto-downloader."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def check_all(self) -> bool:
        yt_dlp_ok = self._ensure(Config.YT_DLP_EXE, "yt-dlp", self._download_yt_dlp)
        ffmpeg_ok = self._ensure(Config.FFMPEG_EXE, "ffmpeg", self._download_ffmpeg)
        return yt_dlp_ok and ffmpeg_ok

    def _ensure(self, path: str, name: str, downloader) -> bool:
        # Prevent 0-byte or corrupt files from passing the check. After _install() the program may be path.bin, and
        # path is the script that starts it: a .bin without that script is not an installation.
        program = path + BIN_SUFFIX if os.path.isfile(path + BIN_SUFFIX) else path
        if os.path.isfile(path) and os.path.isfile(program) and os.path.getsize(program) > 1024:
            if not os.access(path, os.X_OK):
                try:
                    os.chmod(path, 0o755)
                except OSError as e:
                    self._logger.error("❌ %s is not executable: %s", name, e)
                    return False
            self._logger.info("✅ %s is ready", name)
            return True
        self._logger.info("Missing or corrupted %s detected, attempting to download automatically...", name)
        return downloader()

    @staticmethod
    def _fetch(url: str, dest: str, checksums: str | None = None):
        """Downloads *url* to *dest* with a socket timeout (urlretrieve may hang forever).

        Raises OSError if the connection dropped early: reading just returns b'' on a
        truncated body, so a half-downloaded file would otherwise look like a success.
        Also if *checksums*, the SHA-256 list published in the same folder as *url*, gives another digest for it.
        Raises ShutdownRequested / CancelRequested as soon as the program closes or Stop is pressed.
        """
        digest = hashlib.sha256()
        req = urllib.request.Request(url, headers={"User-Agent": "X-Downloader"})
        with urllib.request.urlopen(req, timeout=Config.HTTP_TIMEOUT) as resp, \
                open(dest, 'wb') as out:
            # read1(): whatever has arrived, instead of waiting for a full MiB, so that Stop is noticed at once.
            while chunk := resp.read1(1024 * 1024):
                ProcessManager.raise_if_stopped()
                digest.update(chunk)
                out.write(chunk)
            expected = resp.headers.get("Content-Length")
            written = out.tell()
        if expected is not None:
            expected = expected.strip()
            if not expected.isdigit():
                expected = None
        if expected is not None and written != int(expected):
            raise OSError(f"incomplete download ({written} of {expected} bytes): {url}")
        if checksums and digest.hexdigest() != EnvManager._listed_sha256(url, checksums):
            raise OSError(f"the download does not match its SHA-256 in {checksums}: {url}")

    @staticmethod
    def _listed_sha256(url: str, checksums: str) -> str:
        """The digest that the list *checksums* (`sha256sum` format) in *url*'s folder gives for *url*'s file.

        A release published between the two requests makes them disagree: that download fails, the next one works.
        """
        folder, name = url.rsplit("/", 1)
        req = urllib.request.Request(f"{folder}/{checksums}", headers={"User-Agent": "X-Downloader"})
        with urllib.request.urlopen(req, timeout=Config.HTTP_TIMEOUT) as resp:
            text = resp.read(1024 * 1024).decode("utf-8", "replace")      # a few KiB; never read without a limit
        for line in text.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].lstrip("*") == name:
                return fields[0].lower()
        raise OSError(f"{name} is not listed in {folder}/{checksums}")

    @staticmethod
    def _runs_here(program: str, version_flag: str) -> bool:
        """False if this system cannot start *program* by itself (generic Linux builds on Alpine or NixOS)."""
        try:
            result = ProcessManager.run([program, version_flag], timeout=Config.TOOL_CHECK_TIMEOUT,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return result.returncode != 127     # 127: the stub NixOS keeps at the usual place of the loader
        except FileNotFoundError:               # the file is there: what is missing is its dynamic loader
            return False
        except (OSError, subprocess.TimeoutExpired):
            return True

    def _install(self, tmp: str, path: str, version_flag: str):
        """Puts the downloaded program *tmp* in place as *path*.

        Where generic Linux builds cannot start by themselves (Alpine has another C library, NixOS keeps glibc
        elsewhere), the dynamic loader of the bundled runtime can start them: a small script takes the place of the
        program, which goes to path.bin. *tmp* is tried before anything is put in place, so a Stop during the try
        (it raises CancelRequested) leaves nothing half installed.
        """
        loaders = glob.glob(os.path.join(sys.prefix, "lib", "ld-linux-*"))
        if self._runs_here(tmp, version_flag) or not loaders:
            os.replace(tmp, path)
            return
        folder, name = os.path.split(path)
        os.replace(tmp, path + BIN_SUFFIX)
        atomic_write_text(path, _LOADER_SCRIPT.format(
            lib=os.path.relpath(os.path.dirname(loaders[0]), folder),
            loader=os.path.basename(loaders[0]),
            program=name + BIN_SUFFIX,
        ), mode=0o755)
        self._logger.info("This system cannot start %s by itself; using the bundled dynamic loader.", name)

    def _download_yt_dlp(self) -> bool:
        tmp_path = Config.YT_DLP_EXE + ".tmp"
        try:
            self._fetch(Config.YT_DLP_URL, tmp_path, "SHA2-256SUMS")
            if os.path.getsize(tmp_path) <= 1024:
                raise OSError("downloaded file is too small")
            os.chmod(tmp_path, 0o755)
            self._install(tmp_path, Config.YT_DLP_EXE, "--version")
            self._logger.info("✅ yt-dlp download complete")
            return True
        except ShutdownRequested:
            raise                               # Stop or closing: not a failure, and nothing was installed
        except Exception as e:
            self._logger.error("❌ yt-dlp download failed: %s", e, exc_info=True)
            self._logger.error("  Please download manually: %s", Config.YT_DLP_URL)
            return False
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _use_system_ffmpeg(self) -> bool:
        """Falls back to an ffmpeg installed on the system."""
        system = shutil.which("ffmpeg")
        if not system:
            return False
        Config.FFMPEG_EXE = system          # DownloaderCore passes its folder to --ffmpeg-location
        self._logger.info("✅ Using the system ffmpeg: %s", system)
        return True

    def _download_ffmpeg(self) -> bool:
        tmp_exe = Config.FFMPEG_EXE + ".tmp"
        try:
            if not Config.FFMPEG_URL:
                if self._use_system_ffmpeg():
                    return True
                self._logger.error(
                    "❌ Cannot auto-download FFmpeg for this architecture (%s). Please install ffmpeg with your system's package manager.",
                    platform.machine()
                )
                return False

            self._fetch(Config.FFMPEG_URL, Config.FFMPEG_ARCHIVE, "checksums.sha256")
            self._logger.info("Extracting ffmpeg...")

            with tarfile.open(Config.FFMPEG_ARCHIVE, 'r:xz') as tf:
                entry = next(
                    (m for m in tf.getmembers() if m.name.endswith("/bin/ffmpeg") and m.isfile()),
                    None
                )
                if entry is None:
                    self._logger.error("❌ ffmpeg not found in tar.xz")
                    return self._use_system_ffmpeg()
                with tf.extractfile(entry) as src, open(tmp_exe, 'wb') as dst:
                    shutil.copyfileobj(src, dst)

            if os.path.getsize(tmp_exe) <= 1024:
                raise OSError("extracted ffmpeg binary is too small")
            os.chmod(tmp_exe, 0o755)
            self._install(tmp_exe, Config.FFMPEG_EXE, "-version")

            self._logger.info("✅ ffmpeg extraction complete")
            return True
        except ShutdownRequested:
            raise                               # Stop or closing: no failure, no system ffmpeg instead
        except Exception as e:
            self._logger.error("❌ ffmpeg download or extraction failed: %s", e, exc_info=True)
            if self._use_system_ffmpeg():           # e.g. offline
                return True
            self._logger.error("  Please download manually: https://ffmpeg.org/download.html")
            return False
        finally:
            for leftover in (tmp_exe, Config.FFMPEG_ARCHIVE):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

    def update_yt_dlp(self):
        self._logger.info("Checking for yt-dlp updates...")
        if not os.path.isfile(Config.YT_DLP_EXE):
            self._logger.warning("Could not find %s, skipping update.", Config.YT_DLP_EXE)
            return
        try:
            # --ignore-config: without -P yt-dlp reads a yt-dlp.conf from the current folder (see DownloaderCore).
            # One try: the program installs nightly builds, and a plain -U of those stays on nightly anyway.
            result = ProcessManager.run(
                [Config.YT_DLP_EXE, "--ignore-config", "-U", "--update-to", "nightly"],
                timeout=Config.UPDATE_TIMEOUT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if result.returncode == 0:
                self._logger.info("✅ yt-dlp update check complete")
            else:
                self._logger.warning("yt-dlp update failed, continuing execution.\n%s", result.stderr or result.stdout)
        except subprocess.TimeoutExpired:
            self._logger.warning("yt-dlp update timed out, continuing execution.")
        except ShutdownRequested:
            pass
        except OSError as e:
            # e.g. PermissionError: previously escaped and froze the GUI on "Checking Updates..."
            self._logger.warning("Cannot run %s (%s), skipping update.", Config.YT_DLP_EXE, e)

