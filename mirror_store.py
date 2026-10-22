"""身体镜像的按会话、按天存储。只依赖标准库。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path


def umo_hash(umo: str) -> str:
    return hashlib.sha1(str(umo).encode("utf-8")).hexdigest()[:12]


class MirrorStore:
    def __init__(self, data_dir: Path, keep_days: int = 7):
        self.root = Path(data_dir) / "mirror"
        self.keep_days = max(1, int(keep_days or 7))

    def _path(self, umo: str, date_str: str) -> Path:
        return self.root / umo_hash(umo) / f"{date_str}.json"

    def load(self, umo: str, date_str: str) -> dict:
        path = self._path(umo, date_str)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, umo: str, date_str: str, state: dict) -> None:
        path = self._path(umo, date_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        state["umo"] = umo
        state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def latest_today(self, date_str: str) -> dict:
        """当天最近更新的一份状态；没有就返回空。"""
        best = {}
        best_key = ""
        for path in self.root.glob(f"*/{date_str}.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                key = str(data.get("last_point_at") or data.get("updated_at") or "")
                if key >= best_key:
                    best_key = key
                    best = data
            except Exception:
                continue
        return best

    def all_today(self, date_str: str) -> list[dict]:
        out: list[dict] = []
        for path in self.root.glob(f"*/{date_str}.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("umo"):
                    out.append(data)
            except Exception:
                continue
        return out

    def prune(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        cutoff = (now - timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        try:
            for day_dir in self.root.glob("*"):
                if not day_dir.is_dir():
                    continue
                for f in day_dir.glob("*.json"):
                    if f.stem < cutoff:
                        try:
                            f.unlink()
                        except OSError:
                            pass
        except Exception:
            pass
