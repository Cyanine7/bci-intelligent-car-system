"""Independently check a saved v1/v2 run without Qt, hardware or live MCP."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from car_host.measurement_analysis import recompute_run


def main():
    parser = argparse.ArgumentParser(description="独立原始CRC/字段/分类复算；原始文件不改写")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--no-save", action="store_true", help="仅只读验证，不写recompute.json")
    args = parser.parse_args()
    try:
        result = recompute_run(args.directory, save=not args.no_save)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps(dict(verified=False, error=str(exc)), ensure_ascii=False))
        return 1
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
