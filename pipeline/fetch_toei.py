"""都バス(ODPT GTFS-JP)取得・抽出。

ToeiBus-GTFS.zip を ODPT から取得し、config/my_stops.yaml の `toei` 定義について
平日 / 土日祝ごとの発車時刻リストを組み立てる(設計書 §3 のスキーマ)。

設計方針(CLAUDE.md):
  - 「取得(HTTP + トークン)」と「変換(パース・抽出)」を分離する。
    取得は fetch_gtfs_zip() のみ。それ以外は zip バイト列を受け取る純粋関数で、
    保存済みフィクスチャだけでテストできる(実API不要)。
  - ODPT_TOKEN は環境変数から読む。コード・ログに出さないこと。

平日/土日祝の判定は calendar.txt の曜日フラグで行う(service_buckets)。
祝日そのものの日付判定は build_timetable 側の holidays 配列に委ね、
ここでは「平日ダイヤ / 土日祝ダイヤ」の2バケットへの振り分けだけを行う。
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from collections import defaultdict

OPERATOR = "toei"
OPERATOR_NAME = "都営バス"

# ODPT の GTFS-JP zip 取得先。正確な URL/パラメータは APIキー取得後に要確認。
ODPT_TOEI_GTFS_URL = "https://api.odpt.org/api/v4/files/odpt/ToeiBus-GTFS.zip"

_WEEKDAY_COLS = ("monday", "tuesday", "wednesday", "thursday", "friday")


# --- 取得(トークン必要・ここだけ APIキー取得後に実装)---------------------
def fetch_gtfs_zip(token: str | None = None) -> bytes:
    """ODPT から ToeiBus-GTFS.zip を取得して bytes を返す。

    token 未指定時は環境変数 ODPT_TOKEN を使う。実装は APIキー取得後。
    """
    token = token or os.environ.get("ODPT_TOKEN")
    if not token:
        raise RuntimeError("ODPT_TOKEN が未設定です(.env / GitHub Secrets)")
    # TODO(step3 残り): requests で ODPT_TOEI_GTFS_URL を取得(URL/認証方式は要確認)。
    raise NotImplementedError("GTFS zip の取得は APIキー取得後に実装する")


# --- 時刻ヘルパー -----------------------------------------------------------
def gtfs_time_to_seconds(t: str) -> int:
    """GTFS の 'HH:MM:SS'(24時超えあり)を 0時からの秒数に。"""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def seconds_to_hhmm(sec: int) -> str:
    """秒数を 'HH:MM' に。24時超え(深夜便)は 24 で丸めて実時計表記にする。

    例: 25:05 → '01:05'。並び順は呼び出し側で生秒数ソートを維持すること。
    """
    h = (sec // 3600) % 24
    m = (sec % 3600) // 60
    return f"{h:02d}:{m:02d}"


# --- サービス日の振り分け ---------------------------------------------------
def service_buckets(cal_row: dict) -> set[str]:
    """calendar.txt の1行 → {'weekday'} / {'holiday'} / 両方 / 空。

    平日フラグ(月〜金のいずれか)が立てば weekday、土日いずれかが立てば holiday。
    全日運行のサービスは両方に入る。
    """
    buckets: set[str] = set()
    if any(cal_row.get(c) == "1" for c in _WEEKDAY_COLS):
        buckets.add("weekday")
    if cal_row.get("saturday") == "1" or cal_row.get("sunday") == "1":
        buckets.add("holiday")
    return buckets


# --- GTFS 読み込み ----------------------------------------------------------
def load_gtfs(zip_bytes: bytes) -> dict[str, list[dict]]:
    """zip バイト列 → {テーブル名: [行dict, ...]}。UTF-8(BOM可)前提。"""
    tables: dict[str, list[dict]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if not name.endswith(".txt"):
                continue
            with zf.open(name) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                tables[name[:-4]] = list(reader)
    return tables


# --- 抽出(純粋関数)--------------------------------------------------------
def _matches(cfg: dict, stop_name: str) -> bool:
    target = cfg["name"]
    if cfg.get("match", "exact") == "contains":
        return target in stop_name
    return stop_name == target


def _slug(route_short: str, direction_id: str) -> str:
    r = re.sub(r"[^0-9a-z-]", "", route_short.lower())
    return f"toei-{r}-{direction_id or '0'}"


def extract(gtfs: dict[str, list[dict]], toei_cfg: dict) -> list[dict]:
    """GTFS テーブル群 + my_stops.yaml の toei 定義 → routes 配列(設計書 §3)。

    (系統, 方面) ごとに1オブジェクトを作り、その中に対象バス停の
    weekday / holiday 時刻配列を持たせる。バス停の並びは config の順を保つ。
    """
    stops_cfg = toei_cfg.get("stops") or []
    routes_filter = set(toei_cfg.get("routes") or [])

    # 1. 対象 stop_id → 表示名(config の name に正規化。のりば違いをまとめる)
    stop_id_to_name: dict[str, str] = {}
    for s in gtfs.get("stops", []):
        for cfg in stops_cfg:
            if _matches(cfg, s.get("stop_name", "")):
                stop_id_to_name[s["stop_id"]] = cfg["name"]
                break

    # 2. route_id → route_short_name
    route_short = {
        r["route_id"]: (r.get("route_short_name") or r.get("route_long_name", ""))
        for r in gtfs.get("routes", [])
    }

    # 3. service_id → バケット集合
    svc_buckets = {c["service_id"]: service_buckets(c) for c in gtfs.get("calendar", [])}

    # 4. trip_id → 系統/方面/バケット(route フィルタと空バケットを除外)
    trips: dict[str, dict] = {}
    for t in gtfs.get("trips", []):
        rs = route_short.get(t["route_id"], "")
        if routes_filter and rs not in routes_filter:
            continue
        buckets = svc_buckets.get(t.get("service_id", ""), set())
        if not buckets:
            continue
        trips[t["trip_id"]] = {
            "route_short": rs,
            "headsign": t.get("trip_headsign", ""),
            "direction_id": t.get("direction_id", ""),
            "buckets": buckets,
        }

    # 5. stop_times → (系統,方面) × バス停 × バケット の発車秒集合
    #    grouped[key][stop_name][bucket] = {秒: None}(重複排除 + 生秒でソート)
    grouped: dict[tuple, dict[str, dict[str, dict]]] = defaultdict(
        lambda: defaultdict(lambda: {"weekday": {}, "holiday": {}})
    )
    for st in gtfs.get("stop_times", []):
        tid = st.get("trip_id")
        if tid not in trips:
            continue
        name = stop_id_to_name.get(st.get("stop_id", ""))
        if name is None:
            continue
        dep = st.get("departure_time") or st.get("arrival_time")
        if not dep:
            continue
        info = trips[tid]
        key = (info["route_short"], info["direction_id"], info["headsign"])
        sec = gtfs_time_to_seconds(dep)
        for b in info["buckets"]:
            grouped[key][name][b][sec] = None

    # 6. 出力を組み立て(バス停は config の順を維持)
    routes_out: list[dict] = []
    for (rs, did, headsign), stops_map in sorted(grouped.items()):
        stops_list = []
        for cfg in stops_cfg:
            name = cfg["name"]
            if name not in stops_map:
                continue
            b = stops_map[name]
            stops_list.append({
                "stop_name": name,
                "weekday": [seconds_to_hhmm(s) for s in sorted(b["weekday"])],
                "holiday": [seconds_to_hhmm(s) for s in sorted(b["holiday"])],
            })
        if not stops_list:
            continue
        routes_out.append({
            "id": _slug(rs, did),
            "operator": OPERATOR,
            "operator_name": OPERATOR_NAME,
            "route_name": rs,
            "direction": headsign,
            "stops": stops_list,
        })
    return routes_out
