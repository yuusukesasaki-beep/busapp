"""統合JSON生成 + 妥当性チェック。

各 fetcher の結果を結合し、data/timetable.json(アプリが読む唯一のファイル)を生成する。
祝日は内閣府 syukujitsu.csv から生成して同梱(アプリに祝日ロジックを持たせない)。

妥当性チェック: 便数が前回比 ±50% を超えたら異常として異常終了(デプロイ中止)。
このチェックは絶対に無効化しない(設計書 §2.4 / CLAUDE.md)。

  uv run pipeline/build_timetable.py --local   # ローカル生成(要 .env)

構成方針(CLAUDE.md): 組み立て・カウント・妥当性チェックは純粋関数として分離し、
フィクスチャ/モックでテストする。取得は各 fetcher の fetch_* に閉じ込める。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import fetch_toei
import holidays

JST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "my_stops.yaml"
DATA_PATH = ROOT / "data" / "timetable.json"

# 妥当性チェックのしきい値(前回比の変化率)。無効化しないこと。
TRIP_COUNT_THRESHOLD = 0.5


class ValidationError(Exception):
    """妥当性チェック不合格(デプロイ中止)。"""


# --- 純粋関数(テスト対象)--------------------------------------------------
def now_jst() -> datetime:
    return datetime.now(JST)


def count_departures(routes: list[dict]) -> int:
    """routes 配下の weekday/holiday 発車時刻の総数(便数の代理指標)。"""
    total = 0
    for r in routes:
        for s in r.get("stops", []):
            total += len(s.get("weekday", [])) + len(s.get("holiday", []))
    return total


def build_document(
    routes: list[dict],
    holiday_list: list[str],
    sources: dict[str, dict],
    generated_at: datetime,
) -> dict:
    """設計書 §3 のスキーマに沿った timetable.json 相当の dict を組み立てる。"""
    return {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "sources": sources,
        "holidays": holiday_list,
        "routes": routes,
    }


def validate_trip_count(
    prev_count: int | None,
    new_count: int,
    threshold: float = TRIP_COUNT_THRESHOLD,
) -> None:
    """便数が前回比 ±threshold を超えて変化していたら ValidationError。

    前回が無い(初回)/前回0 のときは判定不能として通す。
    """
    if not prev_count:  # None または 0
        return
    ratio = abs(new_count - prev_count) / prev_count
    if ratio > threshold:
        raise ValidationError(
            f"便数が前回比 {ratio:.0%} 変化(前回 {prev_count} → 今回 {new_count})。"
            f"しきい値 {threshold:.0%} 超のためデプロイ中止。"
        )


# --- I/O -------------------------------------------------------------------
def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_previous(path: Path = DATA_PATH) -> dict | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_document(doc: dict, path: Path = DATA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --- オーケストレーション --------------------------------------------------
def run(local: bool = False) -> dict:
    cfg = load_config()
    now = now_jst()
    sources: dict[str, dict] = {}

    # 都バス(Phase 1)。取得は fetch_gtfs_zip(APIキー取得後に実装)。
    token = os.environ.get("ODPT_TOKEN")
    zip_bytes = fetch_toei.fetch_gtfs_zip(token)
    gtfs = fetch_toei.load_gtfs(zip_bytes)
    routes = fetch_toei.extract(gtfs, cfg.get("toei") or {})
    sources["toei"] = {"status": "ok", "fetched_at": now.isoformat(timespec="seconds")}

    # 祝日(内閣府CSV)。当年〜翌年ぶんを同梱。
    holiday_list = holidays.parse_holidays(
        holidays.fetch_syukujitsu_csv(), year_from=now.year, year_to=now.year + 1
    )

    # 妥当性チェック(前回比 ±50%)→ 不合格なら書き込まず中止
    prev = load_previous()
    prev_count = count_departures(prev.get("routes", [])) if prev else None
    validate_trip_count(prev_count, count_departures(routes))

    doc = build_document(routes, holiday_list, sources, now)
    write_document(doc)
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="timetable.json を生成する")
    parser.add_argument("--local", action="store_true", help="ローカル生成(要 .env)")
    args = parser.parse_args(argv)
    try:
        doc = run(local=args.local)
    except ValidationError as e:
        print(f"[妥当性チェック不合格] {e}", file=sys.stderr)
        return 1
    print(f"生成完了: routes={len(doc['routes'])} holidays={len(doc['holidays'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
