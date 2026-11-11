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


# --- validate_trip_counts_by_operator ---------------------------------------
def _route(op: str, weekday: list[str], stop_name: str = "x") -> dict:
    return {
        "operator": op,
        "stops": [{"stop_name": stop_name, "weekday": weekday, "saturday": [], "holiday": []}],
    }


def test_count_departures_by_operator():
    routes = [_route("toei", ["07:00", "07:10"]), _route("brt", ["08:00"])]
    assert bt.count_departures_by_operator(routes) == {"toei": 2, "brt": 1}


def test_validate_by_operator_allows_new_source():
    """新ソース追加(前回に無い operator)は全体便数が急増しても止めない。"""
    prev = [_route("toei", ["07:00", "07:10"])]
    new = [_route("toei", ["07:00", "07:10"]), _route("brt", ["08:00"] * 100)]
    bt.validate_trip_counts_by_operator(prev, new)


def test_validate_by_operator_catches_single_source_anomaly():
    """1ソースだけ半減超なら、全体では±50%以内でも止める。"""
    prev = [_route("toei", ["07:00"] * 100), _route("brt", ["08:00"] * 100)]
    new = [_route("toei", ["07:00"] * 100), _route("brt", ["08:00"] * 10)]
    with pytest.raises(bt.ValidationError, match=r"\[brt\]"):
        bt.validate_trip_counts_by_operator(prev, new)


def test_stop_group_map_and_annotate():
    """config の group(depart/return)が routes 配下の停留所に付与される。"""
    cfg = {
        "toei": {"stops": [{"name": "はるみらい前", "group": "depart"},
                           {"name": "銀座四丁目", "group": "return"}]},
        "brt": {"stops": [{"name": "新橋", "group": "return"},
                          {"name": "無指定"}]},  # group 無し → 付与されない
    }
    gmap = bt.stop_group_map(cfg)
    assert gmap == {"はるみらい前": "depart", "銀座四丁目": "return", "新橋": "return"}

    routes = [
        {"operator": "toei", "stops": [{"stop_name": "はるみらい前"}]},
        {"operator": "brt", "stops": [{"stop_name": "新橋"}, {"stop_name": "無指定"}]},
    ]
    bt.annotate_stop_groups(routes, gmap)
    assert routes[0]["stops"][0]["group"] == "depart"
    assert routes[1]["stops"][0]["group"] == "return"
    assert "group" not in routes[1]["stops"][1]


def test_validate_by_operator_catches_vanished_source():
    """前回あった operator が今回 0 便なら異常として止める。"""
    prev = [_route("toei", ["07:00"] * 10), _route("brt", ["08:00"] * 10)]
    new = [_route("toei", ["07:00"] * 10)]
    with pytest.raises(bt.ValidationError, match=r"\[brt\]"):
        bt.validate_trip_counts_by_operator(prev, new)


# --- collect_source(失敗時は前回データで継続)--------------------------------
NOW = datetime(2026, 11, 10, 3, 0, 0, tzinfo=JST)


def _prev_doc():
    return {
        "generated_at": "2026-11-09T03:00:00+09:00",
        "sources": {
            "brt": {"status": "ok", "fetched_at": "2026-11-09T03:00:00+09:00"},
            "harumi": {"status": "ok", "fetched_at": "2026-11-09T03:00:00+09:00",
                       "revision": "2026-07-18"},
        },
        "holidays": ["2026-11-23"],
        "routes": [_route("brt", ["08:00"], "b") | {"id": "brt-b01"},
                   _route("harumi", ["09:00"], "h") | {"id": "harumi-tokyo"}],
    }


def test_collect_source_ok_records_fetched_at_and_extra():
    routes, sources = [], {}
    ok = bt.collect_source(
        "harumi", lambda: ([_route("harumi", ["07:16"])], {"revision": "2026-07-18"}),
        _prev_doc(), NOW, routes, sources,
    )
    assert ok
    assert len(routes) == 1 and routes[0]["stops"][0]["weekday"] == ["07:16"]
    assert sources["harumi"] == {
        "status": "ok", "fetched_at": "2026-11-10T03:00:00+09:00", "revision": "2026-07-18",
    }


def test_collect_source_failure_keeps_previous_as_stale():
    def boom():
        raise RuntimeError("PDF のリンクが見つかりません")

    routes, sources = [], {}
    ok = bt.collect_source("harumi", boom, _prev_doc(), NOW, routes, sources)
    assert not ok
    assert [r["id"] for r in routes] == ["harumi-tokyo"]  # 前回の harumi 分だけ
    src = sources["harumi"]
    assert src["status"] == "stale"
    assert src["fetched_at"] == "2026-11-09T03:00:00+09:00"  # 前回の取得時刻を保つ
    assert src["revision"] == "2026-07-18"                    # 付帯情報も保つ
    assert "晴海ライナー" in src["note"]
    assert "PDF" in src["error"]


def test_collect_source_empty_result_is_failure():
    routes, sources = [], {}
    ok = bt.collect_source("brt", lambda: ([], {}), _prev_doc(), NOW, routes, sources)
    assert not ok
    assert sources["brt"]["status"] == "stale"
    assert [r["id"] for r in routes] == ["brt-b01"]


def test_collect_source_failure_without_previous_is_missing():
    def boom():
        raise RuntimeError("ODPT_TOKEN が未設定です")

    routes, sources = [], {}
    ok = bt.collect_source("toei", boom, _prev_doc(), NOW, routes, sources)
    assert not ok
    assert routes == []
    assert sources["toei"]["status"] == "missing"
    assert sources["toei"]["fetched_at"] == ""


# --- run / main(fetcher をモックした統合テスト)------------------------------
def _patch_io(monkeypatch, tmp_path, prev: dict | None):
    data_path = tmp_path / "timetable.json"
    if prev is not None:
        data_path.write_text(__import__("json").dumps(prev), encoding="utf-8")
    monkeypatch.setattr(bt, "DATA_PATH", data_path)
    monkeypatch.setattr(bt, "load_previous", lambda path=None: prev)
    monkeypatch.setattr(bt, "write_document", lambda doc, path=None: data_path.write_text(
        __import__("json").dumps(doc, ensure_ascii=False), encoding="utf-8"))
    monkeypatch.setattr(bt, "load_config", lambda path=None: {
        "toei": {"stops": [{"name": "t", "group": "depart"}], "routes": []},
        "brt": {"stops": [{"name": "b"}]},
        "harumi": {"stops": [{"name": "h", "group": "return"}]},
    })
    monkeypatch.setattr(bt, "now_jst", lambda: NOW)
    monkeypatch.setattr(bt.holidays, "fetch_syukujitsu_csv", lambda: b"")
    monkeypatch.setattr(bt.holidays, "parse_holidays", lambda *a, **k: ["2026-11-23"])
    return data_path


def test_run_partial_failure_writes_doc_and_reports(monkeypatch, tmp_path):
    data_path = _patch_io(monkeypatch, tmp_path, _prev_doc())
    monkeypatch.setattr(bt, "fetch_toei_routes", lambda cfg, token: ([_route("toei", ["07:00"], "t")], {}))
    monkeypatch.setattr(bt, "fetch_brt_routes", lambda cfg: ([_route("brt", ["08:05"], "b")], {}))

    def harumi_fail(cfg):
        raise RuntimeError("site changed")
    monkeypatch.setattr(bt, "fetch_harumi_routes", harumi_fail)

    doc, failed = bt.run()
    assert failed == ["harumi"]
    assert doc["sources"]["toei"]["status"] == "ok"
    assert doc["sources"]["brt"]["status"] == "ok"
    assert doc["sources"]["harumi"]["status"] == "stale"
    assert {r["operator"] for r in doc["routes"]} == {"toei", "brt", "harumi"}
    # 区分け付与(toei=depart, harumi=return)
    groups = {r["operator"]: r["stops"][0].get("group") for r in doc["routes"]}
    assert groups["toei"] == "depart" and groups["harumi"] == "return"
    assert data_path.exists()


def test_run_aborts_when_everything_fails_without_previous(monkeypatch, tmp_path):
    _patch_io(monkeypatch, tmp_path, None)

    def fail(*a, **k):
        raise RuntimeError("down")
    for name in ("fetch_toei_routes", "fetch_brt_routes", "fetch_harumi_routes"):
        monkeypatch.setattr(bt, name, fail)
    with pytest.raises(RuntimeError, match="全ソース"):
        bt.run()


def test_run_validation_failure_does_not_write(monkeypatch, tmp_path):
    prev = _prev_doc()
    prev["routes"] = [_route("toei", ["07:00"] * 100)]
    data_path = _patch_io(monkeypatch, tmp_path, prev)
    before = data_path.read_text(encoding="utf-8")
    monkeypatch.setattr(bt, "fetch_toei_routes", lambda cfg, token: ([_route("toei", ["07:00"] * 10)], {}))
    monkeypatch.setattr(bt, "fetch_brt_routes", lambda cfg: ([_route("brt", ["08:05"])], {}))
    monkeypatch.setattr(bt, "fetch_harumi_routes", lambda cfg: ([_route("harumi", ["09:00"])], {}))
    with pytest.raises(bt.ValidationError, match=r"\[toei\]"):
        bt.run()
    assert data_path.read_text(encoding="utf-8") == before


def test_main_writes_report_on_validation_error(monkeypatch, tmp_path):
    def run_fail(local=False):
        raise bt.ValidationError("[toei] 便数が前回比 90% 変化")
    monkeypatch.setattr(bt, "run", run_fail)
    report = tmp_path / "r.json"
    assert bt.main(["--report", str(report)]) == 1
    data = __import__("json").loads(report.read_text(encoding="utf-8"))
    assert data["ok"] is False
    assert "妥当性チェック不合格" in data["error"]
    assert data["failed_sources"] == []


def test_main_writes_report_on_partial_failure(monkeypatch, tmp_path):
    doc = {"generated_at": "2026-11-10T03:00:00+09:00", "holidays": ["x"],
           "sources": {"toei": {"status": "ok"}, "brt": {"status": "stale"}},
           "routes": [_route("toei", ["07:00"]), _route("brt", ["08:00", "08:10"])]}
    monkeypatch.setattr(bt, "run", lambda local=False: (doc, ["brt"]))
    report = tmp_path / "r.json"
    assert bt.main(["--report", str(report)]) == 0  # 一部失敗でも生成は成功(終了コード0)
    data = __import__("json").loads(report.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["failed_sources"] == ["brt"]
    assert data["counts"] == {"toei": 1, "brt": 2}
    assert data["sources"]["brt"]["status"] == "stale"
