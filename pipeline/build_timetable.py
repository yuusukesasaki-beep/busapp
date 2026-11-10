"""統合JSON生成 + 妥当性チェック。

各 fetcher の結果を結合し、data/timetable.json(アプリが読む唯一のファイル)を生成する。
祝日は内閣府 syukujitsu.csv から生成して同梱(アプリに祝日ロジックを持たせない)。

妥当性チェック: 便数が前回比 ±50% を超えたら異常として異常終了(デプロイ中止)。
このチェックは絶対に無効化しない(設計書 §2.4 / CLAUDE.md)。

  uv run pipeline/build_timetable.py --local   # ローカル生成(要 .env)
  uv run pipeline/build_timetable.py --report data/build-report.json   # Actions 用

ソース(都バス/東京BRT/晴海ライナー)はそれぞれ独立に取得し、1つが失敗しても
前回の当該ソース分を維持して sources.<op>.status を stale にする(設計書 §3)。
失敗は --report の JSON に記録され、Actions が GitHub Issue を自動起票する。

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
import fetch_harumi
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


def stop_group_map(cfg: dict) -> dict[str, str]:
    """my_stops.yaml 全セクション → {停留所名: group}。

    group は depart(おでかけ: 晴海→都心)/ return(帰宅: 都心→晴海)。
    アプリはこの値で停留所カードを区分け表示する(未指定なら付与しない)。
    """
    mapping: dict[str, str] = {}
    for section in ("toei", "brt", "harumi"):
        for stop in (cfg.get(section) or {}).get("stops") or []:
            group = stop.get("group")
            if group:
                mapping[stop["name"]] = group
    return mapping


def annotate_stop_groups(routes: list[dict], group_map: dict[str, str]) -> list[dict]:
    """routes 配下の各停留所エントリに group を付与する(in-place で返す)。"""
    for r in routes:
        for s in r.get("stops", []):
            group = group_map.get(s.get("stop_name", ""))
            if group:
                s["group"] = group
    return routes


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


# --- ソース単位の取得(失敗時は前回データで継続)-----------------------------
#: operator キー → 表示名(stale 時の note に使う)
SOURCE_LABELS = {
    fetch_toei.OPERATOR: fetch_toei.OPERATOR_NAME,
    fetch_brt.OPERATOR: fetch_brt.OPERATOR_NAME,
    fetch_harumi.OPERATOR: fetch_harumi.OPERATOR_NAME,
}


def fetch_toei_routes(cfg: dict, token: str | None) -> tuple[list[dict], dict]:
    gtfs = fetch_toei.load_gtfs(fetch_toei.fetch_gtfs_zip(token))
    return fetch_toei.extract(gtfs, cfg), {}


def fetch_brt_routes(cfg: dict) -> tuple[list[dict], dict]:
    pages = {s["name"]: fetch_brt.fetch_stop_page(s["name"]) for s in cfg["stops"]}
    return fetch_brt.extract(pages, cfg), {}


def fetch_harumi_routes(cfg: dict) -> tuple[list[dict], dict]:
    parsed = fetch_harumi.parse_timetable_pdf(fetch_harumi.fetch_timetable_pdf())
    extra = {"revision": parsed["revision"]} if parsed.get("revision") else {}
    return fetch_harumi.extract(parsed, cfg), extra


def collect_source(
    op: str,
    fetcher,
    prev: dict | None,
    now: datetime,
    routes: list[dict],
    sources: dict[str, dict],
) -> bool:
    """1ソースぶんの取得・抽出を行い routes / sources に積む。

    取得・パースに失敗しても全体は止めず、前回の当該 operator 分を維持して
    sources[op] を stale にする(設計書 §3「障害に強い」)。前回分も無ければ missing。
    戻り値は成功したかどうか(呼び出し側がレポート/終了コードに使う)。
    """
    label = SOURCE_LABELS.get(op, op)
    fetched_at = now.isoformat(timespec="seconds")
    try:
        new_routes, extra = fetcher()
        if not new_routes:
            raise RuntimeError("抽出結果が空(サイト/データ構造が変わった可能性)")
        routes += new_routes
        sources[op] = {"status": "ok", "fetched_at": fetched_at, **extra}
        return True
    except Exception as e:  # noqa: BLE001 - 前回データで継続するため広く拾う
        # 例外メッセージに秘密情報が混ざらないよう各 fetcher が責任を持つ(fetch_toei 参照)
        err = str(e)[:200] or e.__class__.__name__
        print(f"[warn] {label} の取得に失敗: {err}", file=sys.stderr)
        prev_routes = [
            r for r in (prev or {}).get("routes", []) if r.get("operator") == op
        ]
        prev_src = ((prev or {}).get("sources") or {}).get(op) or {}
        if prev_routes:
            routes += prev_routes
            sources[op] = {
                **{k: v for k, v in prev_src.items() if k not in ("status", "note", "error")},
                "status": "stale",
                "fetched_at": prev_src.get("fetched_at", ""),
                "note": f"{label}の取得に失敗したため前回データ",
                "error": err,
            }
        else:
            sources[op] = {
                "status": "missing",
                "fetched_at": "",
                "note": f"{label}のデータを取得できていません",
                "error": err,
            }
        return False


# --- オーケストレーション --------------------------------------------------
def run(local: bool = False) -> tuple[dict, list[str]]:
    """timetable.json を生成する。戻り値は (生成した dict, 失敗した operator の一覧)。"""
    if local:
        load_dotenv()
    cfg = load_config()
    now = now_jst()
    prev = load_previous()
    sources: dict[str, dict] = {}
    routes: list[dict] = []
    failed: list[str] = []

    # 都バス(Phase 1): ODPT GTFS-JP
    toei_cfg = cfg.get("toei") or {}
    token = os.environ.get("ODPT_TOKEN")
    if not collect_source(
        fetch_toei.OPERATOR, lambda: fetch_toei_routes(toei_cfg, token),
        prev, now, routes, sources,
    ):
        failed.append(fetch_toei.OPERATOR)

    # 東京BRT(Phase 2): 公式サイトの HTML パース
    brt_cfg = cfg.get("brt") or {}
    if brt_cfg.get("stops"):
        if not collect_source(
            fetch_brt.OPERATOR, lambda: fetch_brt_routes(brt_cfg),
            prev, now, routes, sources,
        ):
            failed.append(fetch_brt.OPERATOR)

    # 晴海ライナー(Phase 2): 公式ページ → 時刻表 PDF パース
    harumi_cfg = cfg.get("harumi") or {}
    if harumi_cfg.get("stops"):
        if not collect_source(
            fetch_harumi.OPERATOR, lambda: fetch_harumi_routes(harumi_cfg),
            prev, now, routes, sources,
        ):
            failed.append(fetch_harumi.OPERATOR)

    if not routes:
        raise RuntimeError("全ソースの取得に失敗し、前回データもありません(生成中止)")

    # 祝日(内閣府CSV)。当年〜翌年ぶんを同梱。失敗時は前回の一覧を維持。
    try:
        holiday_list = holidays.parse_holidays(
            holidays.fetch_syukujitsu_csv(), year_from=now.year, year_to=now.year + 1
        )
        if not holiday_list:
            raise RuntimeError("祝日一覧が空")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 祝日CSVの取得に失敗: {str(e)[:200]}", file=sys.stderr)
        holiday_list = (prev or {}).get("holidays") or []
        if not holiday_list:
            raise
        failed.append("holidays")

    # 停留所に区分け(おでかけ/帰宅)を付与(アプリの表示用)
    annotate_stop_groups(routes, stop_group_map(cfg))

    # 妥当性チェック(operator ごとに前回比 ±50%)→ 不合格なら書き込まず中止
    validate_trip_counts_by_operator(prev.get("routes", []) if prev else [], routes)

    doc = build_document(routes, holiday_list, sources, now)
    write_document(doc)
    return doc, failed


def write_report(path: Path, doc: dict | None, failed: list[str], error: str | None) -> None:
    """Actions が Issue 起票に使う実行レポート(JSON)。秘密情報は含めない。"""
    report = {
        "ok": error is None,
        "error": error,
        "failed_sources": failed,
        "sources": (doc or {}).get("sources", {}),
        "counts": count_departures_by_operator((doc or {}).get("routes", [])),
        "generated_at": (doc or {}).get("generated_at"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="timetable.json を生成する")
    parser.add_argument("--local", action="store_true", help="ローカル生成(要 .env)")
    parser.add_argument(
        "--report", type=Path, default=None,
        help="実行レポート(JSON)の出力先。Actions の Issue 自動起票に使う",
    )
    args = parser.parse_args(argv)
    doc, failed, error = None, [], None
    try:
        doc, failed = run(local=args.local)
    except ValidationError as e:
        error = f"[妥当性チェック不合格] {e}"
    except Exception as e:  # noqa: BLE001 - レポートに残してから異常終了
        error = f"[生成失敗] {str(e)[:300]}"
    finally:
        if args.report:
            write_report(args.report, doc, failed, error)

    if error:
        print(error, file=sys.stderr)
        return 1
    status = "一部ソース失敗(前回データで継続): " + ", ".join(failed) if failed else "全ソース成功"
    print(f"生成完了: routes={len(doc['routes'])} holidays={len(doc['holidays'])} / {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
