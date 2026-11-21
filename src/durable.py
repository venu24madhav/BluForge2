"""Crash-safe file writing. A power cut, kill -9 or full disk in the middle of a normal write leaves a half-written
file that later parses as garbage (or as a plausible-looking wrong result). These helpers write to a temporary file
in the same directory, flush it to disk, then rename it over the target in one atomic step: a reader sees either the
complete old file or the complete new one, never a mixture."""
import hashlib
import os
import tempfile


def fsync_dir(path):
    """Persist the rename itself (best effort; not every filesystem allows opening a directory)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path, data, mode=0o644):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(d)


def atomic_write_text(path, text, mode=0o644):
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def sha256_hex(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def sweep_temp_files(directory, older_than_s=3600):
    """Remove leftover .tmp-* files from an interrupted write (never touches real result files)."""
    import time
    n = 0
    try:
        for name in os.listdir(directory):
            if name.startswith(".tmp-"):
                p = os.path.join(directory, name)
                try:
                    if time.time() - os.path.getmtime(p) > older_than_s:
                        os.unlink(p)
                        n += 1
                except OSError:
                    pass
    except OSError:
        pass
    return n
