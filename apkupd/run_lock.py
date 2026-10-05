"""OS-backed lock, released even when the process exits unexpectedly."""
from contextlib import contextmanager


@contextmanager
def single_instance(path):
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        if file.tell() == 0:
            file.write(b'0')
            file.flush()
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('已有更新任务运行，请等待其结束') from exc
        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
