"""File helpers without dependencies on the rest of the package (config.py uses them too)."""
import os
import re
import time
import uuid


def atomic_write_text(path: str, text: str, mode: int | None = None):
    """Writes *text* to *path* via temp file + fsync + os.replace (never leaves a half-written file).

    A symlinked *path* is followed, so the link itself survives the replace. A process killed mid-write
    leaves its "<path>.<uuid>.tmp" behind; remove_stale_temp_files() cleans those up.
    """
    path = os.path.realpath(path)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     mode if mode is not None else 0o666)
        try:
            if mode is not None:
                os.fchmod(fd, mode)
            f = os.fdopen(fd, 'w', encoding='utf-8')
            fd = -1  # now owned by f
            with f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if fd != -1:
                os.close(fd)
            raise
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def remove_stale_temp_files(path: str, max_age: float = 60.0):
    """Deletes the temp files of earlier atomic_write_text(*path*) calls that were killed before the rename.

    Only files older than *max_age* seconds go, so a write that is running right now (a second instance
    starting up) is never touched. Never raises.
    """
    folder, name = os.path.split(os.path.realpath(path))       # the writer creates them next to the real file
    ours = re.compile(re.escape(name) + r"\.[0-9a-f]{32}\.tmp")     # uuid4().hex, as in atomic_write_text()
    now = time.time()
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if (ours.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
                            and now - entry.stat(follow_symlinks=False).st_mtime > max_age):
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def folder_problem(path: str) -> tuple[str, str] | None:
    """Why *path* cannot be the download folder, as (code, detail); None if it is usable.

    code "name":  yt-dlp (-P) would not write where we point it: it expands $VAR / ${VAR} and strips
                  surrounding spaces; a name that is not valid UTF-8 cannot be stored in settings.json.
    code "write": nothing can be created in it. os.access() lies on network and ACL file systems, so a
                  real file is created and removed.
    """
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return "name", "not valid UTF-8"
    if os.path.expandvars(path) != path or path != path.strip():
        return "name", "contains $VARIABLE text or surrounding spaces"
    probe = os.path.join(path, f".xdl-write-test-{uuid.uuid4().hex}")
    try:
        os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        os.unlink(probe)
    except OSError as e:
        return "write", e.strerror or str(e)
    return None
