"""Windows pythonw launcher: preserve startup errors and prevent duplicate writers."""
import os
import sys
import traceback
from pathlib import Path


def main():
    root=Path(__file__).resolve().parent.parent
    os.chdir(root)
    (root/'logs').mkdir(exist_ok=True)
    with (root/'logs'/'background.log').open('a',encoding='utf-8',buffering=1) as output:
        sys.stdout=sys.stderr=output
        try:
            import msvcrt
            with (root/'logs'/'runtime.lock').open('a+b') as lock:
                lock.seek(0)
                if lock.read(1)==b'':
                    lock.write(b'0');lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                except OSError:
                    print('Existing background agent owns account writer lock.')
                    return 1
                from ops.run import main as run
                return run()
        except BaseException:
            traceback.print_exc()
            return 1


if __name__=='__main__':
    sys.exit(main())
