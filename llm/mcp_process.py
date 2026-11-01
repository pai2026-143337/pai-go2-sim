"""Expose the dedicated publisher PID, then replace this process with ros-mcp."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main():
    pidfile, command, *args = sys.argv[1:]
    Path(pidfile).write_text(str(os.getpid()))
    os.execv(command, [command, *args])


if __name__ == '__main__':
    main()
