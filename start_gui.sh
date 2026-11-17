#!/bin/sh
# ═══════════════════════════════════════════════════════════════
# start_gui.sh — X-Downloader launcher
#
# Nothing has to be installed: the project brings its own Python and Tk (see bundled-runtime/README.md)
# and the program fetches yt-dlp and ffmpeg itself. No root password, no package manager.
# Plain sh on purpose: not every system has bash.
# ═══════════════════════════════════════════════════════════════
# A CDPATH makes cd look elsewhere first, and with "." in it (a common setting) cd prints where it went, which breaks
# rt=$(cd ...) in the bundled bin/python. Unset here, it is gone for everything started below too.
unset CDPATH
cd "$(dirname "$0")" || exit 1

# ── Color definitions ───────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'  # No Color

log_info()  { printf "${GREEN}[✓]${NC} %s\n" "$*"; }
log_warn()  { printf "${YELLOW}[!]${NC} %s\n" "$*"; }
log_err()   { printf "${RED}[✗]${NC} %s\n" "$*"; }
log_step()  { printf "${BLUE}[→]${NC} %s\n" "$*"; }

# ═══════════════════════════════════════════════════════════════
# Step 1: Pick the Python to run on
# ═══════════════════════════════════════════════════════════════
ARCH=$(uname -m)
BUNDLE="bundled-runtime/linux-$ARCH.tar.gz"
RUNTIME=".runtime/$ARCH"

if [ -f "$BUNDLE" ]; then
    # The bundled Python finds its libraries through --library-path, a colon-separated list: a ':' in the folder
    # path splits it in two. pwd -P, because that is the path the wrapper scripts compute (links resolved).
    case "$(pwd -P)" in
        *:*)
            log_err "專案資料夾的路徑含有冒號（:）：$(pwd -P)"
            log_err "內附的執行環境無法在這種路徑下啟動。請改資料夾名稱，或把專案移到沒有冒號的位置（例如 ~/X-Downloader）。"
            exit 1 ;;
    esac
    # The stamp is written last, so an interrupted unpacking starts over instead of leaving half a
    # runtime behind; a bundle newer than the stamp means the project was updated.
    unpacked() { [ -f "$RUNTIME/.unpacked" ] && ! [ "$BUNDLE" -nt "$RUNTIME/.unpacked" ]; }
    if ! unpacked; then
        # Two launches at once (a double click on the first start) must not unpack into the same folder together: one
        # started the program on a half-written runtime. mkdir is atomic, so one takes the lock and the other waits,
        # then finds the runtime ready. A lock whose launcher is gone (killed meanwhile) is taken over, as is one held
        # for over 2 minutes.
        LOCK="$RUNTIME.lock"
        mkdir -p .runtime 2>/dev/null
        mine=
        waited=0
        while :; do
            if mkdir "$LOCK" 2>/dev/null; then mine=1; break; fi
            [ -d "$LOCK" ] || break                 # no other launcher: the folder cannot be written (said below)
            holder=
            read -r holder 2>/dev/null < "$LOCK/pid"
            if { [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; } || [ "$waited" -ge 120 ]; then
                rm -rf "$LOCK"
            fi
            sleep 1
            waited=$((waited + 1))
        done
        if [ -n "$mine" ]; then
            echo $$ > "$LOCK/pid"
            trap 'rm -rf "$LOCK"' EXIT
            trap 'exit 1' HUP INT TERM
        fi
        if ! unpacked; then                         # the launcher that held the lock may have done it
            log_step "正在準備內附的執行環境（只有第一次需要）..."
            rm -rf "$RUNTIME"
            if ! { mkdir -p "$RUNTIME" && tar -xzf "$BUNDLE" -C "$RUNTIME" && touch "$RUNTIME/.unpacked"; }; then
                log_err "無法解開 $BUNDLE：請確認專案資料夾可以寫入，檔案也完整。"
                exit 1
            fi
        fi
        if [ -n "$mine" ]; then
            rm -rf "$LOCK"
            trap - EXIT HUP INT TERM
        fi
    fi
    PYTHON="$RUNTIME/bin/python"
    # A noexec mount (a USB stick, a shared partition) says no here; nohup's "Permission denied" would not explain it.
    if [ ! -x "$PYTHON" ]; then
        log_err "這個資料夾所在的磁碟不允許執行程式（以 noexec 掛載，或不支援執行權限）。請把專案複製到家目錄（例如 ~/X-Downloader）再啟動。"
        exit 1
    fi
elif python3 -c 'import sys, tkinter, customtkinter; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PYTHON=python3
else
    log_err "這個 CPU（$ARCH）沒有內附的執行環境。請自行安裝 Python 3.10 以上、tkinter 和 customtkinter 後再執行。"
    exit 1
fi

# ═══════════════════════════════════════════════════════════════
# Step 2: Launch the GUI
# ═══════════════════════════════════════════════════════════════
# yt-dlp unpacks itself into the temporary folder and runs from there, which a noexec /tmp forbids.
if grep -q ' /tmp [^ ]* [^ ]*noexec' /proc/mounts 2>/dev/null; then
    TMPDIR="$(pwd)/.tmp"
    export TMPDIR
    mkdir -p "$TMPDIR"
fi

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    log_warn "未偵測到圖形顯示環境（DISPLAY / WAYLAND_DISPLAY），GUI 可能無法啟動。"
fi

# Write startup errors to gui_startup.log (previously sent to /dev/null, so failures were silent). Made anew each time,
# not just emptied: emptying keeps the mode of the first one (0666 if that was under umask 0), and one left by a run
# with sudo cannot be written at all, which stopped every launch.
GUI_LOG="$(pwd)/gui_startup.log"
rm -f "$GUI_LOG"
nohup "$PYTHON" -m xdownloader >"$GUI_LOG" 2>&1 &
GUI_PID=$!
sleep 2
if ! kill -0 "$GUI_PID" 2>/dev/null; then
    log_err "X-Downloader 啟動失敗，錯誤訊息如下（完整內容見 $GUI_LOG）："
    tail -n 20 "$GUI_LOG"
    exit 1
fi
log_info "已啟動（PID $GUI_PID），可關閉此終端視窗。"
