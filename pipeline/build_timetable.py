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

import fetch_brt
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
    """routes 配下の全ダイヤ区分(平日/土曜/休日)の発車時刻総数(便数の代理指標)。"""
    total = 0
    for r in routes:
        for s in r.get("stops", []):
            for bucket in fetch_toei.BUCKETS:
                total += len(s.get(bucket, []))
    return total


def count_departures_by_operator(routes: list[dict]) -> dict[str, int]:
    """operator ごとの発車時刻総数。妥当性チェックをソース単位で行うために使う。"""
    counts: dict[str, int] = {}
    for r in routes:
        op = r.get("operator", "")
        counts[op] = counts.get(op, 0) + count_departures([r])
    return counts


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


def validate_trip_counts_by_operator(
    prev_routes: list[dict],
    new_routes: list[dict],
    threshold: float = TRIP_COUNT_THRESHOLD,
) -> None:
    """便数チェックを operator 単位で行う。

    全体合計だと新ソース追加(例: BRT対応)が「異常な増加」に見えてしまうため、
    ソースごとに前回比 ±threshold を判定する。前回に無い operator は初回として
    通し、前回あったのに今回 0 になった operator は異常(-100%)として止める。
    """
    prev_counts = count_departures_by_operator(prev_routes)
    new_counts = count_departures_by_operator(new_routes)
    for op in sorted(set(prev_counts) | set(new_counts)):
        try:
            validate_trip_count(prev_counts.get(op), new_counts.get(op, 0), threshold)
        except ValidationError as e:
            raise ValidationError(f"[{op}] {e}") from None


# --- I/O -------------------------------------------------------------------
def load_dotenv(path: Path = ROOT / ".env") -> None:
    """.env があれば環境変数へ読み込む(既存の環境変数は上書きしない)。

    依存を増やさない軽量パーサ。ローカル実行(--local)用。値は出力しないこと。
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


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
    if local:
        load_dotenv()
    cfg = load_config()
    now = now_jst()
    prev = load_previous()
    sources: dict[str, dict] = {}

    # 都バス(Phase 1)。取得は fetch_gtfs_zip(APIキー取得後に実装)。
    token = os.environ.get("ODPT_TOKEN")
    zip_bytes = fetch_toei.fetch_gtfs_zip(token)
    gtfs = fetch_toei.load_gtfs(zip_bytes)
    routes = fetch_toei.extract(gtfs, cfg.get("toei") or {})
    sources["toei"] = {"status": "ok", "fetched_at": now.isoformat(timespec="seconds")}

    # 東京BRT(Phase 2)。公式サイトの HTML パース。取得・パースに失敗しても
    # 全体は止めず、前回の BRT 分を維持して sources.brt を stale にする(設計書 §3)。
    brt_cfg = cfg.get("brt") or {}
    if brt_cfg.get("stops"):
        try:
            pages = {
                s["name"]: fetch_brt.fetch_stop_page(s["name"])
                for s in brt_cfg["stops"]
            }
            brt_routes = fetch_brt.extract(pages, brt_cfg)
            if not brt_routes:
                raise RuntimeError("抽出結果が空(ページ構造が変わった可能性)")
            routes += brt_routes
            sources["brt"] = {
                "status": "ok", "fetched_at": now.isoformat(timespec="seconds"),
            }
        except Exception as e:  # noqa: BLE001 - 前回データで継続するため広く拾う
            print(f"[warn] 東京BRT取得失敗: {e}", file=sys.stderr)
            routes += [
                r for r in (prev or {}).get("routes", [])
                if r.get("operator") == fetch_brt.OPERATOR
            ]
            prev_src = ((prev or {}).get("sources") or {}).get("brt") or {}
            sources["brt"] = {
                "status": "stale",
                "fetched_at": prev_src.get("fetched_at", ""),
                "note": "東京BRTの取得に失敗したため前回データ",
            }

    # 祝日(内閣府CSV)。当年〜翌年ぶんを同梱。
    holiday_list = holidays.parse_holidays(
        holidays.fetch_syukujitsu_csv(), year_from=now.year, year_to=now.year + 1
    )

    # 妥当性チェック(operator ごとに前回比 ±50%)→ 不合格なら書き込まず中止
    validate_trip_counts_by_operator(prev.get("routes", []) if prev else [], routes)

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
