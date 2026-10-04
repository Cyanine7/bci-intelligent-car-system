"""Codex 注册此脚本绝对路径，无需依赖进程当前目录。"""

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from car_debug_mcp.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
