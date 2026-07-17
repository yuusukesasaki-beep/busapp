"""fetch_toei の抽出ロジックのテスト。

実API/実サイトへはアクセスせず、保存済み GTFS フィクスチャ
(fixtures/toei_sample_gtfs/*.txt)を zip 化して load_gtfs → extract を検証する。

フィクスチャは実データの特徴を再現:
  - 系統名が全角(都０５－１)→ NFKC 正規化で都05-1 に一致
  - 平日/土曜/休日でダイヤが異なる3区分
  - 出入庫便(都０５－１出入)は完全一致で除外
  - calendar.txt に週フラグが無い service(SP)は対象外
  - 同名の別のりば(H1/H2)は1つにまとまる / 深夜便(24:30)は 00:30 表記
"""

import io
import zipfile
from pathlib import Path

import fetch_toei

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "toei_sample_gtfs"

# my_stops.yaml の toei セクション相当(テスト用)
TOEI_CFG = {
    "stops": [{"name": "はるみらい前", "match": "exact"}],
    "routes": ["都05-1"],  # 半角で指定(全角の実データに正規化して一致)
}


def _zip_fixture() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for txt in sorted(FIXTURE_DIR.glob("*.txt")):
            zf.writestr(txt.name, txt.read_text(encoding="utf-8"))
    return buf.getvalue()


def _routes():
    return fetch_toei.extract(fetch_toei.load_gtfs(_zip_fixture()), TOEI_CFG)


# --- ヘルパー -------------------------------------------------------------
def test_gtfs_time_to_seconds():
    assert fetch_toei.gtfs_time_to_seconds("06:10:00") == 6 * 3600 + 10 * 60
    assert fetch_toei.gtfs_time_to_seconds("24:30:00") == 24 * 3600 + 30 * 60


def test_seconds_to_hhmm_wraps_after_midnight():
    assert fetch_toei.seconds_to_hhmm(fetch_toei.gtfs_time_to_seconds("06:10:00")) == "06:10"
    # 深夜便 24:30 は実時計の 00:30 として表示
    assert fetch_toei.seconds_to_hhmm(fetch_toei.gtfs_time_to_seconds("24:30:00")) == "00:30"


def test_normalize_name_fullwidth():
    assert fetch_toei.normalize_name("都０５－１") == "都05-1"


def test_service_buckets_three_way():
    def cal(mon, sat, sun):
        return {"monday": mon, "tuesday": mon, "wednesday": mon, "thursday": mon,
                "friday": mon, "saturday": sat, "sunday": sun}
    assert fetch_toei.service_buckets(cal("1", "0", "0")) == {"weekday"}
    assert fetch_toei.service_buckets(cal("0", "1", "0")) == {"saturday"}
    assert fetch_toei.service_buckets(cal("0", "0", "1")) == {"holiday"}
    # 週フラグ無し(calendar_dates のみの service)は対象外
    assert fetch_toei.service_buckets(cal("0", "0", "0")) == set()


# --- 抽出(スナップショット)---------------------------------------------
def test_extract_snapshot():
    assert _routes() == [
        {
            "id": "toei-05-1-0",
            "operator": "toei",
            "operator_name": "都営バス",
            "route_name": "都05-1",  # 全角→半角に正規化
            "direction": "晴海埠頭",
            "stops": [
                {
                    "stop_name": "はるみらい前",
                    "weekday": ["07:18", "07:41"],   # H1 と H2(のりば)がまとまる
                    "saturday": ["06:32"],
                    "holiday": ["06:47", "00:30"],   # 24:30 → 00:30(生秒順は維持)
                },
            ],
        },
        {
            "id": "toei-05-1-1",
            "operator": "toei",
            "operator_name": "都営バス",
            "route_name": "都05-1",
            "direction": "東京駅丸の内南口",
            "stops": [
                {
                    "stop_name": "はるみらい前",
                    "weekday": ["06:41"],
                    "saturday": [],
                    "holiday": [],
                },
            ],
        },
    ]


def test_sat_sun_are_distinct():
    """土曜と休日が別配列になる(2区分マージだと混ざっていた問題の回帰防止)。"""
    outbound = next(r for r in _routes() if r["id"] == "toei-05-1-0")
    stop = outbound["stops"][0]
    assert stop["saturday"] == ["06:32"]
    assert "06:32" not in stop["holiday"]


def test_deadhead_and_decoy_excluded():
    """出入庫便(都０５－１出入)とフィルタ外系統(波０１)は結果に出ない。"""
    routes = _routes()
    assert all(r["route_name"] == "都05-1" for r in routes)
    # 深川車庫前(出入庫の headsign)や豊海水産埠頭(デコイ)が方面に出ないこと
    dirs = {r["direction"] for r in routes}
    assert dirs == {"晴海埠頭", "東京駅丸の内南口"}


def test_directions_filter():
    """directions を指定すると、その方面(trip_headsign)の便だけ残る。"""
    cfg = dict(TOEI_CFG, directions=["東京駅丸の内南口"])
    routes = fetch_toei.extract(fetch_toei.load_gtfs(_zip_fixture()), cfg)
    assert [r["direction"] for r in routes] == ["東京駅丸の内南口"]
    assert routes[0]["stops"][0]["weekday"] == ["06:41"]


def test_special_service_dropped():
    """calendar.txt に週フラグの無い SP(09:00発)はどの区分にも出ない。"""
    outbound = next(r for r in _routes() if r["id"] == "toei-05-1-0")
    stop = outbound["stops"][0]
    assert "09:00" not in stop["weekday"] + stop["saturday"] + stop["holiday"]
