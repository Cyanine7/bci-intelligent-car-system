"""python -m car_debug_mcp；仅 stdio，不启动工作台。"""

import argparse
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="附加到已有小车自动化工作台的 stdio MCP")
    parser.add_argument("--project-directory", type=Path,
                        default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    try:
        from .server import create_server
        server = create_server(args.project_directory.resolve())
    except ModuleNotFoundError as exc:
        if exc.name == "mcp":
            print("官方 MCP SDK 尚未安装；请先运行本项目 setup-mcp.ps1。", file=sys.stderr)
            return 1
        raise
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
