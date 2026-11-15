"""Localization (i18n) tables and helpers for GUI labels and execution logs."""
from .config import Config

LANGUAGE_OPTIONS: dict[str, str] = {
    "English": "en",
    "繁體中文": "zh_TW",
}
LANGUAGE_LABELS: dict[str, str] = {v: k for k, v in LANGUAGE_OPTIONS.items()}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "btn_yes": "Yes",
        "btn_no": "No",
        "btn_ok": "OK",
        "dlg_notice": "Notice",
        "dlg_error": "Error",
        "dlg_warning": "Warning",
        "dlg_startup_failed": "Startup Failed",
        "msg_already_running": "Another instance of X-Downloader is already running.",
        "msg_lock_failed": "Cannot use the lock file (check its owner and permissions):\n{error}",
        "msg_log_failed": "Cannot use the log file (check its owner and permissions):\n{error}",
        "msg_dir_change_busy": "Cannot change the download folder while downloads are in progress.",
        "msg_dir_create_failed": "Failed to create folder:\n{error}",
        "msg_dir_not_writable": "Cannot save files in this folder ({error}).\nPlease choose another folder.",
        "msg_dir_bad_name": (
            "This folder path cannot be used: it has $VARIABLE text that is expanded, spaces at its end, "
            "or characters that are not valid UTF-8.\n{path}"
        ),
        "msg_settings_busy": "Cannot change settings while downloads are in progress.",
        "msg_force_close": (
            "A download task is currently in progress. Force closing may result in corrupted files.\n"
            "Are you sure you want to exit?"
        ),
        "msg_no_valid_url": "Please enter at least one valid URL ({sites}).",
        "settings_title": "Settings",
        "settings_sidebar_header": "SETTINGS",
        "tab_general": "General",
        "tab_advanced": "Advanced",
        "tab_about": "About",
        "section_interface": "Interface",
        "label_language": "Language",
        "desc_language": "Select the display language for the user interface and execution log.",
        "section_storage": "Storage",
        "label_download_dir": "Save Location",
        "desc_download_dir": "Destination folder for completed video downloads.",
        "btn_browse": "Browse",
        "btn_open": "Open",
        "btn_default": "Default",
        "status_autosaved_default": "✓ Auto-saved. New downloads will be saved to this folder.",
        "status_autosaved_path": "✓ Auto-saved: {path}",
        "status_save_failed": "⚠ Could not save settings.json. This folder is used only until the app is closed.",
        "status_folder_pending": "This folder does not exist yet. Press Enter to create and use it.",
        "dlg_select_dir": "Select Download Folder",
        "section_network": "Network & Retries",
        "label_max_retries": "Max Attempts",
        "desc_max_retries": "How many times a URL is tried if downloads keep failing (the first try included; 1 = no retry).",
        "section_media": "Media Format",
        "label_container_format": "Container Format",
        "desc_container_format": "Preferred output container format for merged audio and video.",
        "section_env": "Environment & Dependencies",
        "about_version_title": "Version",
        "about_ytdlp_title": "yt-dlp Engine",
        "about_ffmpeg_title": "FFmpeg Converter",
        "about_env_title": "Execution Environment",
        "badge_ready": "Ready",
        "badge_missing": "Missing",
        "queue_count": "Download Queue ({count} valid URL(s) entered)",
        "btn_clear_list": "Clear List",
        "btn_settings": "Settings",
        "menu_paste_url": "Paste URL",
        "btn_download": "Download",
        "btn_stop": "Stop",
        "btn_checking_updates": "Checking for Updates...",
        "label_exec_log": "Execution Log",
        "btn_expand": "Expand",
        "btn_clear": "Clear",
        "log_expanded_title": "Execution Log (Expanded)",
        "btn_cancel": "Cancel",
        "btn_select": "Select",
        "btn_up": "Up",
        "section_places": "Places",
        "shortcut_home": "Home",
        "shortcut_downloads": "Downloads",
        "shortcut_documents": "Documents",
        "shortcut_desktop": "Desktop",
        "msg_empty_directory": "No subfolders",
        "msg_permission_denied": "Permission denied.",
    },
    "zh_TW": {
        "btn_yes": "是",
        "btn_no": "否",
        "btn_ok": "確定",
        "dlg_notice": "提示",
        "dlg_error": "錯誤",
        "dlg_warning": "警告",
        "dlg_startup_failed": "啟動失敗",
        "msg_already_running": "另一個 X-Downloader 實例已在執行中。",
        "msg_lock_failed": "無法使用鎖定檔（請檢查檔案擁有者與權限）：\n{error}",
        "msg_log_failed": "無法使用日誌檔（請檢查檔案擁有者與權限）：\n{error}",
        "msg_dir_change_busy": "下載進行中，無法變更下載資料夾。",
        "msg_dir_create_failed": "無法建立資料夾：\n{error}",
        "msg_dir_not_writable": "無法在此資料夾儲存檔案（{error}）。\n請改選其他資料夾。",
        "msg_dir_bad_name": (
            "無法使用此資料夾路徑：含有會被展開的 $變數 文字、結尾有空白，或含有不是有效 UTF-8 的字元。\n{path}"
        ),
        "msg_settings_busy": "下載進行中，無法變更設定。",
        "msg_force_close": (
            "目前有下載任務正在進行中，強制關閉可能導致檔案損毀。\n"
            "確定要離開嗎？"
        ),
        "msg_no_valid_url": "請輸入至少一個有效的網址（{sites}）。",
        "settings_title": "設定",
        "settings_sidebar_header": "設定",
        "tab_general": "一般",
        "tab_advanced": "進階",
        "tab_about": "關於",
        "section_interface": "介面",
        "label_language": "介面語言",
        "desc_language": "選擇使用者介面與執行日誌的顯示語言。",
        "section_storage": "儲存",
        "label_download_dir": "儲存位置",
        "desc_download_dir": "已完成下載影片的儲存資料夾。",
        "btn_browse": "瀏覽",
        "btn_open": "開啟",
        "btn_default": "預設",
        "status_autosaved_default": "✓ 已自動儲存。新下載的影片將儲存至此資料夾。",
        "status_autosaved_path": "✓ 已自動儲存：{path}",
        "status_save_failed": "⚠ 無法儲存 settings.json，此資料夾只在本次執行期間有效。",
        "status_folder_pending": "此資料夾尚未建立，按 Enter 即可建立並使用。",
        "dlg_select_dir": "選擇下載資料夾",
        "section_network": "網路與重試",
        "label_max_retries": "最大嘗試次數",
        "desc_max_retries": "單一網址持續下載失敗時，最多嘗試幾次（含第一次，1 = 不重試）。",
        "section_media": "媒體格式",
        "label_container_format": "封裝格式",
        "desc_container_format": "合併音訊與視訊時偏好的輸出封裝格式。",
        "section_env": "環境與相依套件",
        "about_version_title": "版本",
        "about_ytdlp_title": "yt-dlp 引擎",
        "about_ffmpeg_title": "FFmpeg 轉檔器",
        "about_env_title": "執行環境",
        "badge_ready": "已就緒",
        "badge_missing": "缺少",
        "queue_count": "下載佇列（已輸入 {count} 個有效網址）",
        "btn_clear_list": "清空清單",
        "btn_settings": "設定",
        "menu_paste_url": "貼上網址",
        "btn_download": "開始下載",
        "btn_stop": "停止下載",
        "btn_checking_updates": "正在檢查更新...",
        "label_exec_log": "執行日誌",
        "btn_expand": "展開",
        "btn_clear": "清除",
        "log_expanded_title": "執行日誌（展開）",
        "btn_cancel": "取消",
        "btn_select": "選擇此資料夾",
        "btn_up": "上一層",
        "section_places": "常用位置",
        "shortcut_home": "家目錄",
        "shortcut_downloads": "下載",
        "shortcut_documents": "文件",
        "shortcut_desktop": "桌面",
        "msg_empty_directory": "（無子資料夾）",
        "msg_permission_denied": "無法存取該資料夾（權限不足）。",
    },
}

LOG_TRANSLATIONS_ZH: dict[str, str] = {
    # gui.py
    "X-Downloader GUI started.": "X-Downloader GUI 已啟動。",
    "Download folder: %s": "下載資料夾：%s",
    "Saved download folder not found (drive not connected?): %s": "找不到已儲存的下載資料夾（磁碟未連接？）：%s",
    "Download folder set to: %s": "下載資料夾已設定為：%s",
    "Cannot save %s: the change lasts only until the app is closed.": "無法儲存 %s：此變更只在本次執行期間有效。",
    "Max attempts set to: %d": "最大嘗試次數已設定為：%d",
    "Merge format set to: %s": "封裝格式已設定為：%s",
    "Language set to: %s": "介面語言已設定為：%s",
    "UI callback failed": "介面回呼執行失敗",
    "yt-dlp update check failed.": "yt-dlp 更新檢查失敗。",
    "Failed to load initial list: %s": "載入初始清單失敗：%s",
    "Loaded %d unfinished URL(s) from %s.": "已載入 %d 個未完成的網址（來源：%s）。",
    "Cannot save list.txt: %s": "無法儲存 list.txt：%s",
    "User forced termination. Cleaning up background processes...": "使用者強制終止程式，正在清理背景程序...",
    "Received signal %d, closing.": "收到訊號 %d，正在關閉。",
    "Application closed.": "應用程式已關閉。",
    "Starting download task for %d URL(s).": "開始執行下載任務，共 %d 個網址。",
    # env.py
    "❌ %s is not executable: %s": "❌ %s 無法執行：%s",
    "✅ %s is ready": "✅ %s 已就緒",
    "Missing or corrupted %s detected, attempting to download automatically...": "偵測到 %s 缺失或損毀，正在嘗試自動下載...",
    "This system cannot start %s by itself; using the bundled dynamic loader.": "此系統無法直接啟動 %s，改用內附的動態載入器。",
    "✅ yt-dlp download complete": "✅ yt-dlp 下載完成",
    "❌ yt-dlp download failed: %s": "❌ yt-dlp 下載失敗：%s",
    "  Please download manually: %s": "  請手動下載：%s",
    "❌ Cannot auto-download FFmpeg for this architecture (%s). Please install ffmpeg with your system's package manager.": (
        "❌ 無法為此系統架構（%s）自動下載 FFmpeg，請用系統的套件管理工具安裝 ffmpeg。"
    ),
    "Extracting ffmpeg...": "正在解壓縮 ffmpeg...",
    "❌ ffmpeg not found in tar.xz": "❌ 在 tar.xz 壓縮檔中找不到 ffmpeg",
    "✅ ffmpeg extraction complete": "✅ ffmpeg 解壓縮完成",
    "✅ Using the system ffmpeg: %s": "✅ 改用系統的 ffmpeg：%s",
    "❌ ffmpeg download or extraction failed: %s": "❌ ffmpeg 下載或解壓縮失敗：%s",
    "  Please download manually: https://ffmpeg.org/download.html": "  請手動下載：https://ffmpeg.org/download.html",
    "Checking for yt-dlp updates...": "正在檢查 yt-dlp 更新...",
    "Could not find %s, skipping update.": "找不到 %s，略過更新。",
    "✅ yt-dlp update check complete": "✅ yt-dlp 更新檢查完成",
    "✅ yt-dlp update check complete (stable)": "✅ yt-dlp 更新檢查完成（穩定版）",
    "yt-dlp update failed, continuing execution.\n%s": "yt-dlp 更新失敗，繼續執行。\n%s",
    "yt-dlp update timed out, continuing execution.": "yt-dlp 更新逾時，繼續執行。",
    "Cannot run %s (%s), skipping update.": "無法執行 %s（%s），略過更新。",
    # core.py
    "⚠️ No valid %s authentication detected. Attempting download anyway (success rate may be low).": (
        "⚠️ 未偵測到有效的 %s 登入驗證，仍將嘗試下載（成功率可能較低）。"
    ),
    "❌ Unsupported site, skipping: %s": "❌ 不支援的網站，略過：%s",
    "🔗 URL normalized: %s → %s": "🔗 網址已標準化：%s → %s",
    "[%d/%d] Preparing download: %s": "[%d/%d] 準備下載：%s",
    "✅ Download successful": "✅ 下載成功",
    "Post-hook failed: %s": "下載後掛鉤執行失敗：%s",
    "❌ Cannot execute yt-dlp: %s": "❌ 無法執行 yt-dlp：%s",
    "❌ Download failed (attempt %d/%d): return code %d": "❌ 下載失敗（第 %d/%d 次嘗試）：返回碼 %d",
    "🔄 Trying fallback: using directly exported cookies...": "🔄 嘗試備用方案：使用直接匯出的 cookies...",
    "💡 Hint: This error usually means you are not logged into %s.": "💡 提示：此錯誤通常表示您尚未登入 %s。",
    "⚠️ Unrecoverable error detected, skipping retries: %s": "⚠️ 偵測到無法恢復的錯誤，略過重試：%s",
    "Waiting %d second(s) before retrying...": "等待 %d 秒後重試...",
    "⚠️ Reached the maximum number of attempts, giving up on download: %s": "⚠️ 已達最大嘗試次數，放棄下載：%s",
    "Queue is empty, nothing to do.": "佇列為空，無須執行任何操作。",
    "Cannot reset list.txt: %s": "無法重置 list.txt：%s",
    "Dependencies missing, download aborted.": "缺少必要相依套件，下載已中止。",
    "Cannot use the download folder %s: %s": "無法使用下載資料夾 %s：%s",
    "Processing [%d/%d]: %s": "正在處理 [%d/%d]：%s",
    "🎉 Task complete! Successfully downloaded %d / %d video(s).": "🎉 任務完成！成功下載 %d / %d 部影片。",
    "Some URLs failed to download. Check failed_list.txt for details.": "部分網址下載失敗，請查看 failed_list.txt 了解詳情。",
    "Download interrupted by shutdown; %d URL(s) kept in .processing.list for resume.": (
        "下載因程式關閉而中斷；已保留 %d 個網址於 .processing.list 供下次續傳。"
    ),
    "Download cancelled; %d URL(s) stay in the list.": "下載已取消；%d 個網址保留在清單中。",
    "Download stopped by the user.": "使用者已停止下載。",
    "Stopped %d leftover yt-dlp process group(s) from an earlier run.": "已結束上次執行遺留的 %d 組 yt-dlp 行程。",
    "Unexpected fatal error in background thread.": "背景執行緒發生未預期的嚴重錯誤。",
    "Unexpected error while downloading %s": "下載 %s 時發生未預期的錯誤",
    "Cannot record the failed URL in failed_list.txt (%s): %s": "無法把失敗的網址記入 failed_list.txt（%s）：%s",
    "The network seems to be down (%d failure(s) in a row); stopping. %d URL(s) stay in the list.": (
        "網路似乎已中斷（連續 %d 次失敗），已停止；%d 個網址保留在清單中。"
    ),
    "Cannot update list.txt: %s": "無法更新 list.txt：%s",
    # cookies.py
    "Cannot inspect %s: %s": "無法檢查 %s：%s",
    "🔍 Detected browser logged into %s: %s (%s)": "🔍 偵測到已登入 %s 的瀏覽器：%s（%s）",
    "⚠️ No browser logged into %s was detected.": "⚠️ 未偵測到已登入 %s 的瀏覽器。",
    "Failed to read Firefox cookies: %s": "讀取 Firefox cookies 失敗：%s",
    "⚠️ Cannot write %s: %s": "⚠️ 無法寫入 %s：%s",
    "✅ Auto-exported %d %s cookies from Firefox": "✅ 已從 Firefox 自動匯出 %d 個 %s cookies",
    "✅ Browser cookies available (%s), will be used automatically": "✅ 瀏覽器 cookies 可用（%s），將自動使用",
    "⚠️ yt-dlp failed to extract cookies from %s, trying fallback...": "⚠️ yt-dlp 無法從 %s 擷取 cookies，正在嘗試備用方案...",
    "Using unverified cookies from %s.": "使用來自 %s 的未驗證 cookies。",
    "⚠️ Cannot acquire cookies, will attempt download as guest (success rate is extremely low).": (
        "⚠️ 無法取得 cookies，將以訪客身分嘗試下載（成功率極低）。"
    ),
    "   It is recommended to log into %s in your browser and try again.": "   建議先在瀏覽器中登入 %s 後再試一次。",
    "Cookie extraction test failed for %s: %s": "%s 的 cookie 擷取測試失敗：%s",
    "⚠️ %s not found, will attempt download as guest.": "⚠️ 找不到 %s，將以訪客身分嘗試下載。",
    "⚠️ Cannot read %s: %s": "⚠️ 無法讀取 %s：%s",
    "⚠️ %s is missing key login cookies: %s": "⚠️ %s 缺少關鍵登入 cookies：%s",
    "   %s restricts media access for guests. Most videos will fail to download.": (
        "   %s 限制訪客存取媒體，大多數影片將無法下載。"
    ),
    "   Please log into the site in your browser first; the downloader will automatically read the browser cookies.": (
        "   請先在瀏覽器中登入該網站，下載器會自動讀取瀏覽器 cookies。"
    ),
    "✅ %s authentication is valid (%s)": "✅ %s 驗證有效（%s）",
    # system.py
    "Another instance is running. Failed to acquire lock.": "另一個實例正在執行中，無法取得鎖定。",
    "Cannot use the lock file %s: %s": "無法使用鎖定檔 %s：%s",
    "Failed to acquire lock (lock file kept changing).": "無法取得鎖定（鎖定檔持續變動）。",
    "Detected %d unfinished download task(s); restored to the queue.": "偵測到 %d 個未完成的下載任務，已還原到佇列。",
    "Updated .processing.list (%d item(s))": "已更新 .processing.list（%d 個項目）",
}

LOG_ARG_TRANSLATIONS_ZH: dict[str, str] = {
    ".processing.list (crash-resume) + list.txt": ".processing.list（異常中斷續傳）+ list.txt",
    "Please log into x.com in your browser, then restart the downloader.": "請先在瀏覽器中登入 x.com，然後重新啟動下載器。",
}

# Log messages are written in English (the source language). To add a language, give it an entry here
# (plus TRANSLATIONS, LANGUAGE_OPTIONS and Config.SUPPORTED_LANGUAGES); tests/test_i18n.py checks the coverage.
LOG_TRANSLATIONS: dict[str, dict[str, str]] = {"zh_TW": LOG_TRANSLATIONS_ZH}        # message template -> text
LOG_ARG_TRANSLATIONS: dict[str, dict[str, str]] = {"zh_TW": LOG_ARG_TRANSLATIONS_ZH}  # string argument -> text


def t(key: str, **kwargs) -> str:
    """Returns the localized UI string for *key* in the active Config.LANGUAGE."""
    lang_map = TRANSLATIONS.get(Config.LANGUAGE) or TRANSLATIONS["en"]
    template = lang_map.get(key) or TRANSLATIONS["en"].get(key, key)
    return template.format(**kwargs) if kwargs else template


def format_log_entry(msg, args, exc_text: str | None) -> str:
    """Formats a log record's message and arguments in the active Config.LANGUAGE."""
    raw_msg = str(msg)
    template = LOG_TRANSLATIONS.get(Config.LANGUAGE, {}).get(raw_msg, raw_msg)
    arg_map = LOG_ARG_TRANSLATIONS.get(Config.LANGUAGE, {})
    if isinstance(args, tuple):
        fmt_args = tuple(arg_map.get(a, a) if isinstance(a, str) else a for a in args)
    elif isinstance(args, dict):
        fmt_args = {k: (arg_map.get(v, v) if isinstance(v, str) else v) for k, v in args.items()}
    else:
        fmt_args = args

    if fmt_args:
        try:
            text = template % fmt_args
        except Exception:
            try:
                text = raw_msg % args
            except Exception:
                text = raw_msg
    else:
        text = template

    if exc_text:
        text = f"{text}\n{exc_text}" if text else exc_text
    return text
