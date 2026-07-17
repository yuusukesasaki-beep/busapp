"""build_timetable の純粋関数(組み立て・カウント・妥当性チェック)のテスト。

取得(fetch_*)には触れず、モックした routes / 便数で検証する。
"""

from datetime import datetime, timezone, timedelta

import pytest

import build_timetable as bt

JST = timezone(timedelta(hours=9))

SAMPLE_ROUTES = [
    {
        "id": "toei-05-1-0",
        "operator": "toei",
        "operator_name": "都営バス",
        "route_name": "都05-1",
        "direction": "晴海埠頭",
        "stops": [
            {
                "stop_name": "はるみらい前",
                "weekday": ["07:18", "07:41"],
                "saturday": ["06:32"],
                "holiday": ["06:47"],
            },
        ],
    },
]


# --- count_departures ------------------------------------------------------
def test_count_departures():
    # weekday 2 + saturday 1 + holiday 1 = 4
    assert bt.count_departures(SAMPLE_ROUTES) == 4
    assert bt.count_departures([]) == 0


# --- build_document --------------------------------------------------------
def test_build_document_schema():
    now = datetime(2026, 7, 5, 3, 0, 12, tzinfo=JST)
    sources = {"toei": {"status": "ok", "fetched_at": "2026-07-05T03:00:00+09:00"}}
    doc = bt.build_document(SAMPLE_ROUTES, ["2026-07-20"], sources, now)

    assert doc["generated_at"] == "2026-07-05T03:00:12+09:00"
    assert doc["sources"] == sources
    assert doc["holidays"] == ["2026-07-20"]
    assert doc["routes"] is SAMPLE_ROUTES
    assert set(doc) == {"generated_at", "sources", "holidays", "routes"}


# --- validate_trip_count ---------------------------------------------------
def test_validate_ok_within_threshold():
    bt.validate_trip_count(100, 60)   # -40% は許容
    bt.validate_trip_count(100, 140)  # +40% は許容


def test_validate_boundary_exactly_50pct_ok():
    bt.validate_trip_count(100, 50)   # ちょうど -50% は許容(超過ではない)
    bt.validate_trip_count(100, 150)  # ちょうど +50% は許容


def test_validate_abort_when_exceeds():
    with pytest.raises(bt.ValidationError):
        bt.validate_trip_count(100, 40)   # -60%
    with pytest.raises(bt.ValidationError):
        bt.validate_trip_count(100, 151)  # +51%


def test_validate_skips_without_previous():
    bt.validate_trip_count(None, 999)  # 初回
    bt.validate_trip_count(0, 999)     # 前回0(判定不能)
