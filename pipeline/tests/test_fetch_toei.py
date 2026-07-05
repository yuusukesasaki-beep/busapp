"""fetch_toei の抽出ロジックのテスト。

実API/実サイトへはアクセスせず、保存済み GTFS フィクスチャ
(fixtures/toei_sample_gtfs/*.txt)を zip 化して load_gtfs → extract を検証する。
"""

import io
import zipfile
from pathlib import Path

import fetch_toei

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "toei_sample_gtfs"

# my_stops.yaml の toei セクション相当(テスト用)
TOEI_CFG = {
    "stops": [
        {"name": "晴海五丁目", "match": "exact"},
        {"name": "晴海埠頭", "match": "exact"},
    ],
    "routes": ["都05-2"],
}


def _zip_fixture() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for txt in sorted(FIXTURE_DIR.glob("*.txt")):
            zf.writestr(txt.name, txt.read_text(encoding="utf-8"))
    return buf.getvalue()


# --- ヘルパー -------------------------------------------------------------
def test_gtfs_time_to_seconds():
    assert fetch_toei.gtfs_time_to_seconds("06:10:00") == 6 * 3600 + 10 * 60
    assert fetch_toei.gtfs_time_to_seconds("25:05:00") == 25 * 3600 + 5 * 60


def test_seconds_to_hhmm_wraps_after_midnight():
    assert fetch_toei.seconds_to_hhmm(fetch_toei.gtfs_time_to_seconds("06:10:00")) == "06:10"
    # 深夜便 25:05 は実時計の 01:05 として表示
    assert fetch_toei.seconds_to_hhmm(fetch_toei.gtfs_time_to_seconds("25:05:00")) == "01:05"


def test_service_buckets():
    assert fetch_toei.service_buckets(
        {"monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
         "friday": "1", "saturday": "0", "sunday": "0"}
    ) == {"weekday"}
    assert fetch_toei.service_buckets(
        {"monday": "0", "tuesday": "0", "wednesday": "0", "thursday": "0",
         "friday": "0", "saturday": "1", "sunday": "1"}
    ) == {"holiday"}
    # 全日運行は両方に入る
    assert fetch_toei.service_buckets(
        {"monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
         "friday": "1", "saturday": "1", "sunday": "1"}
    ) == {"weekday", "holiday"}


# --- 抽出(スナップショット)---------------------------------------------
def test_extract_snapshot():
    gtfs = fetch_toei.load_gtfs(_zip_fixture())
    routes = fetch_toei.extract(gtfs, TOEI_CFG)

    assert routes == [
        {
            "id": "toei-05-2-0",
            "operator": "toei",
            "operator_name": "都営バス",
            "route_name": "都05-2",
            "direction": "晴海埠頭",
            "stops": [
                {"stop_name": "晴海五丁目", "weekday": ["06:10", "07:15"], "holiday": ["08:30"]},
                {"stop_name": "晴海埠頭", "weekday": ["06:20", "07:25"], "holiday": ["01:05"]},
            ],
        },
        {
            "id": "toei-05-2-1",
            "operator": "toei",
            "operator_name": "都営バス",
            "route_name": "都05-2",
            "direction": "東京駅丸の内南口",
            "stops": [
                {"stop_name": "晴海五丁目", "weekday": ["06:45"], "holiday": []},
                {"stop_name": "晴海埠頭", "weekday": ["06:40"], "holiday": []},
            ],
        },
    ]


def test_route_filter_excludes_decoy():
    """routes フィルタ外(都03)とデコイ停留所は結果に出ない。"""
    gtfs = fetch_toei.load_gtfs(_zip_fixture())
    routes = fetch_toei.extract(gtfs, TOEI_CFG)
    assert all(r["route_name"] == "都05-2" for r in routes)
    all_stops = {s["stop_name"] for r in routes for s in r["stops"]}
    assert "勝どき駅前" not in all_stops


def test_noriba_merged_by_name():
    """同名の別のりば(H5A/H5B)が1つの『晴海五丁目』にまとまる。"""
    gtfs = fetch_toei.load_gtfs(_zip_fixture())
    routes = fetch_toei.extract(gtfs, TOEI_CFG)
    outbound = next(r for r in routes if r["id"] == "toei-05-2-0")
    h5 = next(s for s in outbound["stops"] if s["stop_name"] == "晴海五丁目")
    assert h5["weekday"] == ["06:10", "07:15"]  # H5A と H5B の両便
