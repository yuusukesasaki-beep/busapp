"""都バス(ODPT GTFS-JP)取得・抽出。

ToeiBus-GTFS.zip を ODPT から取得し、config/my_stops.yaml の `toei` 定義について
平日 / 土日祝ごとの発車時刻リストを組み立てる(設計書 §3 のスキーマ)。

設計方針(CLAUDE.md):
  - 「取得(HTTP + トークン)」と「変換(パース・抽出)」を分離する。
    取得は fetch_gtfs_zip() のみ。それ以外は zip バイト列を受け取る純粋関数で、
    保存済みフィクスチャだけでテストできる(実API不要)。
  - ODPT_TOKEN は環境変数から読む。コード・ログに出さないこと。

ダイヤ区分は calendar.txt の曜日フラグで 平日 / 土曜 / 休日(日祝)の3バケットに振り分ける
(service_buckets)。都バス実データは土曜と日曜(≒休日)でダイヤが異なるため2区分では不正確。
祝日そのものの日付判定は build_timetable 側の holidays 配列に委ね、アプリが
「祝日→休日ダイヤ」を選ぶ(ここでは曜日ベースの振り分けだけを行う)。

既知の制約: お盆・夏期などの特別ダイヤ(calendar.txt に週フラグが無く calendar_dates
だけで運行日が定義される service)は v1 では対象外(曜日不明のため空バケット→除外)。
鮮度表示と「公式で確認」リンクで補う(設計書 §7)。
"""

from __future__ import annotations

import csv
import io
import os
import re
import unicodedata
import zipfile
from collections import defaultdict

OPERATOR = "toei"
OPERATOR_NAME = "都営バス"

# ODPT の GTFS-JP zip 取得先(公式カタログで確認)。
#   認証あり: 末尾に ?acl:consumerKey=<TOKEN>
#   公開ミラー: 認証不要(開発時の検証用)
ODPT_TOEI_GTFS_URL = "https://api.odpt.org/api/v4/files/Toei/data/ToeiBus-GTFS.zip"
ODPT_TOEI_GTFS_PUBLIC_URL = "https://api-public.odpt.org/api/v4/files/Toei/data/ToeiBus-GTFS.zip"

# User-Agent には連絡先を明記する(設計書 §2.4)。TODO: 実運用の連絡先に差し替え。
USER_AGENT = "harumi-bus/0.1 (+https://github.com/USER/harumi-bus)"

_WEEKDAY_COLS = ("monday", "tuesday", "wednesday", "thursday", "friday")


# --- 取得(トークン必要・ここだけ APIキー取得後に実装)---------------------
def fetch_gtfs_zip(token: str | None = None) -> bytes:
    """ODPT から ToeiBus-GTFS.zip を取得して bytes を返す。

    token 未指定時は環境変数 ODPT_TOKEN を使う。認証は ?acl:consumerKey=<TOKEN>。
    トークンが URL に載るため、失敗時も例外メッセージにトークンを出さないこと。
    """
    import requests

    token = token or os.environ.get("ODPT_TOKEN")
    if not token:
        raise RuntimeError("ODPT_TOKEN が未設定です(.env / GitHub Secrets)")
    try:
        resp = requests.get(
            ODPT_TOEI_GTFS_URL,
            params={"acl:consumerKey": token},
            headers={"User-Agent": USER_AGENT},
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        status = getattr(e.response, "status_code", None)
        # トークン混入を避けるため URL/元例外は出さない(from None)
        raise RuntimeError(f"ODPT GTFS 取得に失敗しました(status={status})") from None
    return resp.content


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
#: ダイヤ区分の順序(出力・カウントで共通利用)
BUCKETS = ("weekday", "saturday", "holiday")


def service_buckets(cal_row: dict) -> set[str]:
    """calendar.txt の1行 → {'weekday'|'saturday'|'holiday'} の集合。

    月〜金のいずれか→weekday、土曜→saturday、日曜→holiday(≒休日/日祝)。
    複数曜日にまたがる service は複数バケットに入る。週フラグが全て0の
    (calendar_dates のみで定義される)service は空を返す=対象外(既知の制約)。
    """
    buckets: set[str] = set()
    if any(cal_row.get(c) == "1" for c in _WEEKDAY_COLS):
        buckets.add("weekday")
    if cal_row.get("saturday") == "1":
        buckets.add("saturday")
    if cal_row.get("sunday") == "1":
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
def normalize_name(s: str) -> str:
    """全角/半角の揺れを吸収(NFKC)。都バスGTFSは系統名が全角(例: 都０５－１)。"""
    return unicodedata.normalize("NFKC", s or "").strip()


def _matches(cfg: dict, stop_name: str) -> bool:
    target = normalize_name(cfg["name"])
    name = normalize_name(stop_name)
    if cfg.get("match", "exact") == "contains":
        return target in name
    return name == target


def _slug(route_short: str, direction_id: str) -> str:
    # route_short は normalize 済み前提(全角→半角)。
    r = re.sub(r"[^0-9a-z-]", "", route_short.lower())
    return f"toei-{r}-{direction_id or '0'}"


def extract(gtfs: dict[str, list[dict]], toei_cfg: dict) -> list[dict]:
    """GTFS テーブル群 + my_stops.yaml の toei 定義 → routes 配列(設計書 §3)。

    (系統, 方面) ごとに1オブジェクトを作り、その中に対象バス停の
    weekday / holiday 時刻配列を持たせる。バス停の並びは config の順を保つ。
    """
    stops_cfg = toei_cfg.get("stops") or []
    routes_filter = {normalize_name(r) for r in (toei_cfg.get("routes") or [])}
    # バス停ごとの方面フィルタ(stop cfg の directions を trip_headsign で照合)。
    # 空/未指定なら全方面。自宅側は都心方向、都心側は帰り方向、と停ごとに変えられる。
    stop_dirs = {
        cfg["name"]: {normalize_name(d) for d in (cfg.get("directions") or [])}
        for cfg in stops_cfg
    }

    # 1. 対象 stop_id → 表示名(config の name に正規化。のりば違いをまとめる)
    stop_id_to_name: dict[str, str] = {}
    for s in gtfs.get("stops", []):
        for cfg in stops_cfg:
            if _matches(cfg, s.get("stop_name", "")):
                stop_id_to_name[s["stop_id"]] = cfg["name"]
                break

    # 2. route_id → route_short_name(全角→半角に正規化)
    route_short = {
        r["route_id"]: normalize_name(
            r.get("route_short_name") or r.get("route_long_name", "")
        )
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
        lambda: defaultdict(lambda: {b: {} for b in BUCKETS})
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
            dirs = stop_dirs.get(name)
            if dirs and normalize_name(headsign) not in dirs:
                continue  # このバス停では見ない方面
            b = stops_map[name]
            entry = {"stop_name": name}
            for bucket in BUCKETS:
                entry[bucket] = [seconds_to_hhmm(s) for s in sorted(b[bucket])]
            stops_list.append(entry)
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
