import json
import os
import platform

from .fsutil import atomic_write_text, remove_stale_temp_files


class Config:
    # Project root (the directory that contains the `xdownloader` package).
    BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DOWNLOAD_DIR = BASE_PATH
    # The saved download folder when it did not exist at startup (e.g. a USB drive not plugged in).
    # Downloads use DOWNLOAD_DIR meanwhile, but saving other settings must not erase the user's choice.
    MISSING_DOWNLOAD_DIR: str | None = None

    YT_DLP_EXE = os.path.join(BASE_PATH, "yt-dlp")
    FFMPEG_EXE = os.path.join(BASE_PATH, "ffmpeg")

    LIST_FILE       = os.path.join(BASE_PATH, "list.txt")
    FAILED_FILE     = os.path.join(BASE_PATH, "failed_list.txt")
    LOCK_FILE       = os.path.join(BASE_PATH, "downloader.lock")
    LOG_FILE        = os.path.join(BASE_PATH, "downloader.log")
    PROCESSING_FILE = os.path.join(BASE_PATH, ".processing.list")
    SETTINGS_FILE   = os.path.join(BASE_PATH, "settings.json")

    # Total tries per URL, the first one included (3 = first try + 2 retries; 1 = no retry). The Settings page
    # calls it "Max Attempts"; this name and the settings.json key "max_retries" predate that wording.
    MAX_RETRIES = 3
    RETRY_DELAY = 5

    # Timeouts (seconds)
    HTTP_TIMEOUT        = 60     # per socket operation when fetching yt-dlp / ffmpeg
    COOKIE_TEST_TIMEOUT = 90     # yt-dlp cookie extraction probe (keyring prompts may block)
    UPDATE_TIMEOUT      = 300    # yt-dlp self-update
    TOOL_CHECK_TIMEOUT  = 60     # does a downloaded yt-dlp / ffmpeg start at all?

    # Download parameters
    FORMAT               = "best[protocol^=http]/bestvideo+bestaudio/best"
    FORMAT_SORT          = "res,proto:https"    # highest resolution first (no cap), then prefer https
    CONCURRENT_FRAGMENTS = 4
    HTTP_CHUNK_SIZE      = "1M"
    # %(id)s keeps file names unique: tweets with an identical (truncated) title and
    # multi-video tweets would otherwise overwrite existing files or be skipped as "already downloaded".
    # `.:50` keeps the first 50 characters; `.NB` also caps the field at N *bytes*: CJK / emoji names take
    # 3-4 bytes per character and would otherwise exceed the 255-byte file name limit ("File name too long").
    # A missing uploader or title prints "[]", not "NA": to yt-dlp a slice of a missing field is an empty list, so no
    # `|NA` default applies. X always sends both. Seven variants were measured against the real yt-dlp: the ones that
    # print NA also rename files that are already downloaded (those would be fetched a second time), so it stays.
    OUTPUT_TEMPLATE = "%(uploader.:50).60B_%(title.:50).100B_%(id)s.%(ext)s"
    MERGE_FORMAT    = "mp4"
    SUPPORTED_FORMATS: tuple[str, ...] = ("mp4", "mkv")
    LANGUAGE        = "en"
    SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "zh_TW")

    # Errors that retrying cannot fix (site-specific ones live in Site.non_retryable)
    NON_RETRYABLE_PATTERNS: list[str] = [
        "is not a valid URL",
        "Unsupported URL",
        "HTTP Error 404",
        "File name too long",
        "No space left on device",
        "[Errno 13] Permission denied",       # the download folder cannot be written
        "[Errno 30] Read-only file system",
    ]

    SUPPORTED_BROWSERS: list[str] = [
        "firefox",
        "chrome",
        "chromium",
        "brave",
        "opera",
        "edge",
        "vivaldi",
    ]

    _ARCH = platform.machine().lower()
    if _ARCH in ("x86_64", "amd64"):
        YT_DLP_URL     = "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp_linux"
        FFMPEG_URL     = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    elif _ARCH in ("aarch64", "arm64"):
        YT_DLP_URL     = "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp_linux_aarch64"
        FFMPEG_URL     = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
    else:
        # Architecture-independent zipapp (runs on any Linux with Python 3.10+)
        YT_DLP_URL     = "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp"
        FFMPEG_URL     = ""

    FFMPEG_ARCHIVE = os.path.join(BASE_PATH, "_ffmpeg_temp.tar.xz")

    @classmethod
    def resolve_path(cls, raw: str) -> str:
        """Resolves a user-supplied path: ~ and $VAR are expanded as in a shell, relative paths are
        anchored to BASE_PATH."""
        cleaned = raw.replace("\x00", "").strip() if isinstance(raw, str) else ""
        if not cleaned:
            return cls.BASE_PATH
        expanded = os.path.expandvars(os.path.expanduser(cleaned))
        if not os.path.isabs(expanded):
            expanded = os.path.join(cls.BASE_PATH, expanded)
        return os.path.abspath(expanded)

    @classmethod
    def load_settings(cls):
        """Loads persistent user settings (such as custom DOWNLOAD_DIR)."""
        remove_stale_temp_files(cls.SETTINGS_FILE)       # what a save that was killed mid-write left behind
        if not os.path.isfile(cls.SETTINGS_FILE):
            return
        try:
            if os.path.getsize(cls.SETTINGS_FILE) > 65536:
                return
            with open(cls.SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cls.MISSING_DOWNLOAD_DIR = None
                d = data.get("download_dir")
                if d == "":                         # the project folder, wherever it is now (see save_settings)
                    cls.DOWNLOAD_DIR = cls.BASE_PATH
                elif d and isinstance(d, str) and d.replace("\x00", "").strip():
                    exp = cls.resolve_path(d)
                    if os.path.isdir(exp):
                        cls.DOWNLOAD_DIR = exp
                    elif not os.path.exists(exp):    # absent (unmounted?), not merely invalid
                        cls.MISSING_DOWNLOAD_DIR = exp
                retries = data.get("max_retries")
                if isinstance(retries, int) and not isinstance(retries, bool) and 1 <= retries <= 10:
                    cls.MAX_RETRIES = retries
                fmt = data.get("merge_format")
                if isinstance(fmt, str) and fmt in cls.SUPPORTED_FORMATS:
                    cls.MERGE_FORMAT = fmt
                lang = data.get("language")
                if isinstance(lang, str) and lang in cls.SUPPORTED_LANGUAGES:
                    cls.LANGUAGE = lang
        except Exception:
            pass

    @classmethod
    def save_settings(cls) -> bool:
        """Persists current user settings atomically with 0600 permissions; False if that failed."""
        folder = cls.MISSING_DOWNLOAD_DIR or cls.DOWNLOAD_DIR
        data = {
            # The project folder is saved as "", not as a path: a copied, moved or renamed project then uses its own.
            "download_dir": "" if folder == cls.BASE_PATH else folder,
            "max_retries": cls.MAX_RETRIES,
            "merge_format": cls.MERGE_FORMAT,
            "language": cls.LANGUAGE,
        }
        try:
            atomic_write_text(cls.SETTINGS_FILE, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)
            return True
        except (OSError, ValueError):       # ValueError: a path that is not valid UTF-8
            return False


Config.load_settings()
