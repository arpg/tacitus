# The resident_io child: the program an owner process runs as the resident,
#   sudo -n -u ea-bot /usr/bin/python3 -I <this file> <op> <octal mode> <path> [<path>]
# in / with PATH as its whole environment, the bytes on stdin. It imports
# only the standard library, and -I keeps this directory off sys.path.
# resident_io.py holds the same program as `_CHILD` and passes it inline
# (-c) when this file is not usable; it names this file only when the file
# holds `_CHILD` after these comment lines, and
# tests/test_resident_io_child_file.py keeps the two the same.

import fcntl, os, stat, sys, tempfile
OK = b"RESIDENT-IO-OK "
MARK = "RESIDENT-IO-REFUSED:"
MISSING = b"RESIDENT-IO-MISSING\n"
MAX_READ = 134217728
FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def plain(fd, path, st=None):
    st = os.fstat(fd) if st is None else st
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise OSError(path + " is not a plain single-linked file")
    return st


def put(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def take(fd):
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def reply(n, payload=b""):
    sys.stdout.buffer.write(OK + str(n).encode() + b"\n" + payload)
    sys.stdout.buffer.flush()


def parent_of(path):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    return parent


def main(op, mode, paths):
    os.umask(0o002)
    path = paths[0]
    if op == "append":
        data = sys.stdin.buffer.read()
        parent_of(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | FLAGS, 0o666)
        try:
            plain(fd, path)
            put(fd, data)
        finally:
            os.close(fd)
        reply(len(data))
    elif op == "replace":
        data = sys.stdin.buffer.read()
        fd, tmp = tempfile.mkstemp(dir=parent_of(path),
                                   prefix="." + os.path.basename(path) + ".", suffix=".tmp")
        try:
            try:
                os.fchmod(fd, mode)
                put(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        reply(len(data))
    elif op == "update":
        parent_of(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | FLAGS, 0o666)
        try:
            plain(fd, path)
            fcntl.flock(fd, fcntl.LOCK_EX)
            old = take(fd)
            reply(len(old), old)
            head = sys.stdin.buffer.readline(64)
            if head.startswith(b"WRITE ") and head[6:].strip().isdigit():
                n = int(head[6:])
                data = sys.stdin.buffer.read(n)
                if len(data) != n:
                    raise OSError("the new content arrived short")
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                put(fd, data)
                os.fsync(fd)
                try:
                    os.fchmod(fd, mode)
                except OSError:
                    pass
                reply(len(data))
        finally:
            os.close(fd)
    elif op == "mkdir":
        parent_of(path)
        try:
            os.mkdir(path, 0o700)
            made = True
        except FileExistsError:
            made = False
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | FLAGS)
        try:
            if made:
                os.fchmod(fd, mode)
        finally:
            os.close(fd)
        reply(0)
    elif op == "touch":
        parent_of(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | FLAGS, 0o666)
        try:
            plain(fd, path)
        finally:
            os.close(fd)
        reply(0)
    elif op == "move":
        os.rename(path, paths[1])
        reply(0)
    elif op == "read":
        for last in (False, True):
            try:
                fd = os.open(path, os.O_RDONLY | FLAGS)
            except FileNotFoundError:
                sys.stdout.buffer.write(MISSING)
                sys.stdout.buffer.flush()
                return
            try:
                st = os.fstat(fd)
                if not last and st.st_nlink == 0:
                    continue
                if plain(fd, path, st).st_size > MAX_READ:
                    raise OSError(path + " is larger than " + str(MAX_READ) + " bytes")
                data = take(fd)
            finally:
                os.close(fd)
            break
        if len(data) > MAX_READ:
            raise OSError(path + " is larger than " + str(MAX_READ) + " bytes")
        reply(len(data), data)
    elif op == "list":
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | FLAGS)
        except FileNotFoundError:
            sys.stdout.buffer.write(MISSING)
            sys.stdout.buffer.flush()
            return
        try:
            rows = []
            for name in sorted(os.listdir(fd)):
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = ("d" if stat.S_ISDIR(st.st_mode) else
                        "f" if stat.S_ISREG(st.st_mode) else
                        "l" if stat.S_ISLNK(st.st_mode) else "o")
                rows.append(kind + name)
        finally:
            os.close(fd)
        data = "\0".join(rows).encode("utf-8", "surrogateescape")
        if len(data) > MAX_READ:
            raise OSError(path + " lists more than " + str(MAX_READ) + " bytes")
        reply(len(data), data)
    else:
        raise OSError("unknown op " + repr(op))


try:
    args = sys.argv[1:]
    want = 4 if args[:1] == ["move"] else 3
    if len(args) != want or not all(os.path.isabs(p) for p in args[2:]):
        raise OSError("usage: <op> <octal mode> <absolute path> [<absolute path>]")
    main(args[0], int(args[1], 8), args[2:])
except Exception as e:
    sys.stderr.write(MARK + " " + str(e))
    sys.exit(4)
