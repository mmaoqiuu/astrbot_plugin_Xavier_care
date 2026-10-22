"""一次性清理身体镜像的心率点：合并平段，保留峰值。

默认 dry-run；加 --apply 才落盘（先备份 .json.bak）。
用法:
    python tools/simplify_points.py            # 看前后对比
    python tools/simplify_points.py --apply    # 真正清理
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import mirror_logic  # noqa: E402


def default_root() -> Path:
    return PLUGIN_DIR.parents[1] / "plugin_data" / PLUGIN_DIR.name / "mirror"


def describe(points: list[dict]) -> str:
    return " ".join(f"{p.get('time')}={p.get('hr')}" for p in points)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(default_root()))
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    files = sorted(root.glob(f"*/{args.date}.json"))
    if not files:
        print(f"没有找到 {args.date} 的数据：{root}")
        return 1

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[跳过] {path}: {exc}")
            continue
        old = data.get("points")
        new = mirror_logic.simplify_points(old)
        print(f"\n{path.parent.name}/{path.name}")
        print(f"  前 {len(old or [])} 点: {describe(old or [])}")
        print(f"  后 {len(new)} 点: {describe(new)}")
        if not args.apply:
            continue
        backup = path.with_suffix(".json.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        data["points"] = new
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
        print(f"  已写入，备份: {backup.name}")

    if not args.apply:
        print("\n这是 dry-run。确认无误后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
