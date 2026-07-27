from __future__ import annotations

import os
import sys


_READY_BYTE = b"\x01"


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 4 or arguments[0] != "--ready-fd":
        return 64
    try:
        ready_fd = int(arguments[1])
    except ValueError:
        return 64
    if arguments[2] != "--":
        return 64
    command = arguments[3:]
    if not command:
        return 64

    try:
        with os.fdopen(ready_fd, "rb", closefd=True) as ready_pipe:
            ready = ready_pipe.read(1)
    except OSError:
        return 70
    if ready != _READY_BYTE:
        return 70

    try:
        os.execvpe(command[0], command, os.environ)
    except OSError:
        return 71


if __name__ == "__main__":
    raise SystemExit(main())
