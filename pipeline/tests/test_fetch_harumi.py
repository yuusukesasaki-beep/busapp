"""fetch_harumi のパース・抽出ロジックのテスト。

実サイトへはアクセスせず、保存済みフィクスチャ(fixtures/harumi/)で検証する:
  - harumi-liner.html      : 公式ページ(Wix)。2026-11-10 取得。PDF リンク解決のテスト用
  - timetable-20260718.pdf : 2026-07-18 改正の時刻表 PDF。平日27便 / 土休日25便
    (土休日は東京駅八重洲北口始発の朝2便を含む)。綾瀬ライナーの表も同居している。
"""

import re
from pathlib import Path

import pytest
import yaml

import fetch_harumi as fh

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "harumi"
CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "my_stops.yaml"
HHMM = re.compile(r"^\d{2}:\d{2}$")


@pytest.fixture(scope="module")
def parsed():
    return fh.parse_timetable_pdf((FIXTURE_DIR / "timetable-20260718.pdf").read_bytes())


# --- ヘルパー -----------------------------------------------------------------
def test_normalize_name_strips_inner_spaces():
    assert fh.normalize_name("晴 海 二 丁 目") == "晴海二丁目"
    assert fh.normalize_name("日 本 橋 髙 島 屋") == "日本橋髙島屋"
    assert fh.normalize_name("　東京駅八重洲北口 ") == "東京駅八重洲北口"


def test_parse_time_cell():
    assert fh.parse_time_cell("7:10") == "07:10"
    assert fh.parse_time_cell("(13:03)") == "13:03"   # 到着側の注記つき
    assert fh.parse_time_cell("(18;49)") == "18:49"   # PDF の誤植(; → :)
    assert fh.parse_time_cell("24:05") == "00:05"     # 深夜表記は実時計に丸める
    for cell in ("ㇾ", "レ", "＝", "", None, "発", "〃", "降\n車\nの\nみ"):
        assert fh.parse_time_cell(cell) is None


# --- ページ HTML → PDF URL ------------------------------------------------------
def test_find_timetable_pdf_url_from_page():
    html = (FIXTURE_DIR / "harumi-liner.html").read_text(encoding="utf-8")
    assert fh.find_timetable_pdf_url(html) == (
        "https://www.hitachi-gr.com/_files/ugd/988479_c2c80c1377c04181affcba4afe12b994.pdf"
    )


def test_find_timetable_pdf_url_fallback_and_relative():
    html = '<p>時刻表</p><a href="/_files/x.pdf">ここ</a>'
    assert fh.find_timetable_pdf_url(html) == "https://www.hitachi-gr.com/_files/x.pdf"


def test_find_timetable_pdf_url_missing_raises():
    with pytest.raises(RuntimeError):
        fh.find_timetable_pdf_url("<html><body>準備中</body></html>")


# --- PDF → 中間構造 --------------------------------------------------------------
def test_parse_pdf_sections_and_revision(parsed):
    assert parsed["revision"] == "2026-07-18"
    assert set(parsed["sections"]) == {"平日", "土休日"}
    assert len(parsed["sections"]["平日"]) == 27
    assert len(parsed["sections"]["土休日"]) == 25


def test_parse_pdf_stop_order(parsed):
    assert parsed["stop_order"] == [
        "晴海トリトンスクエア", "晴海一丁目", "晴海二丁目北", "晴海二丁目", "晴海三丁目",
        "東銀座駅", "有楽町駅", "東京駅八重洲北口", "日本橋髙島屋",
    ]


def test_parse_pdf_first_weekday_trip_skips_tokyo(parsed):
    """平日始発(7:10)は ㇾ 表記で東京駅へ行かず、有楽町駅から晴海へ戻る。"""
    trip = parsed["sections"]["平日"][0]
    assert trip["stops"] == {
        "晴海トリトンスクエア": "07:10", "晴海一丁目": "07:12", "晴海二丁目北": "07:14",
        "晴海二丁目": "07:16", "晴海三丁目": "07:18", "東銀座駅": "07:23", "有楽町駅": "07:30",
    }
    assert trip["arrivals"] == {"晴海トリトンスクエア": "07:47"}


def test_parse_pdf_continuing_trip_has_harumi_arrivals(parsed):
    """18:30 発の便は晴海トリトン到着後も晴海二丁目まで引き続き運行(到着側に時刻あり)。"""
    trip = next(t for t in parsed["sections"]["平日"] if t["stops"].get("晴海トリトンスクエア") == "18:30")
    assert trip["stops"]["東京駅八重洲北口"] == "18:57"
    assert trip["arrivals"]["晴海二丁目"] == "19:22"


def test_parse_pdf_holiday_morning_trips_start_at_tokyo(parsed):
    """土休日の朝2便は東京駅八重洲北口始発(晴海側の発時刻を持たない)。"""
    first, second = parsed["sections"]["土休日"][:2]
    assert first["stops"] == {"東京駅八重洲北口": "07:50", "日本橋髙島屋": "07:55"}
    assert second["stops"] == {"東京駅八重洲北口": "08:30", "日本橋髙島屋": "08:35"}


def test_parse_pdf_excludes_ayase_liner(parsed):
    """同じページの綾瀬ライナー(青井駅・上野駅など)の時刻を拾わない。"""
    for trips in parsed["sections"].values():
        for t in trips:
            names = set(t["stops"]) | set(t["arrivals"])
            assert not names & {"青井駅", "綾瀬二丁目", "上野駅", "京成上野駅", "浅草駅"}


def test_parse_pdf_all_times_well_formed(parsed):
    for trips in parsed["sections"].values():
        for t in trips:
            for v in list(t["stops"].values()) + list(t["arrivals"].values()):
                assert HHMM.match(v)


# --- extract ----------------------------------------------------------------------
HARUMI_CFG = {
    "stops": [
        {"name": "晴海二丁目", "directions": ["東京駅八重洲北口", "有楽町駅"]},
        {"name": "東京駅八重洲北口", "directions": ["晴海"]},
    ],
    "routes": [],
}


def test_extract_snapshot_shape(parsed):
    routes = fh.extract(parsed, HARUMI_CFG)
    assert [(r["id"], r["direction"], [s["stop_name"] for s in r["stops"]]) for r in routes] == [
        ("harumi-tokyo", "東京駅八重洲北口ゆき", ["晴海二丁目"]),
        ("harumi-yurakucho", "有楽町駅ゆき", ["晴海二丁目"]),
        ("harumi-harumi2", "晴海二丁目ゆき", ["東京駅八重洲北口"]),
        ("harumi-triton", "晴海トリトンスクエアゆき", ["東京駅八重洲北口"]),
    ]
    for r in routes:
        assert r["operator"] == "harumi"
        assert r["operator_name"] == "晴海ライナー"
        assert r["route_name"] == "晴海ライナー"
        for s in r["stops"]:
            assert set(s) == {"stop_name", "weekday", "saturday", "holiday"}


def test_extract_depart_counts(parsed):
    """晴海二丁目(都心方面): 平日 20便が東京駅行・7便が有楽町駅止まり、土休日は 20 + 3。"""
    by_id = {r["id"]: r["stops"][0] for r in fh.extract(parsed, HARUMI_CFG)}
    assert by_id["harumi-tokyo"]["weekday"][:3] == ["08:36", "09:01", "09:26"]
    assert len(by_id["harumi-tokyo"]["weekday"]) == 20
    assert by_id["harumi-yurakucho"]["weekday"] == [
        "07:16", "07:36", "07:56", "08:16", "13:03", "18:19", "19:08",
    ]
    assert len(by_id["harumi-tokyo"]["holiday"]) == 20
    assert by_id["harumi-yurakucho"]["holiday"] == ["18:19", "19:08"]


def test_extract_return_counts(parsed):
    """東京駅八重洲北口(晴海方面): 平日 9+11=20便、土休日は朝2便を含む 12+11=23便。"""
    by_id = {r["id"]: r["stops"][0] for r in fh.extract(parsed, HARUMI_CFG)}
    assert len(by_id["harumi-triton"]["weekday"]) == 9
    assert len(by_id["harumi-harumi2"]["weekday"]) == 11
    assert by_id["harumi-harumi2"]["weekday"][0] == "12:34"
    assert by_id["harumi-triton"]["holiday"][:2] == ["07:50", "08:30"]
    assert len(by_id["harumi-triton"]["holiday"]) == 12
    assert len(by_id["harumi-harumi2"]["holiday"]) == 11


def test_extract_saturday_equals_holiday(parsed):
    for r in fh.extract(parsed, HARUMI_CFG):
        for s in r["stops"]:
            assert s["saturday"] == s["holiday"]
            assert s["saturday"] is not s["holiday"]
            assert s["weekday"] == sorted(s["weekday"])


def test_extract_directions_filter_and_name_normalization(parsed):
    cfg = {"stops": [{"name": "晴 海 三 丁 目", "directions": ["東京駅"]}]}
    routes = fh.extract(parsed, cfg)
    assert [r["direction"] for r in routes] == ["東京駅八重洲北口ゆき"]
    assert routes[0]["stops"][0]["stop_name"] == "晴 海 三 丁 目"  # config の表記を保つ


def test_extract_no_directions_includes_all(parsed):
    routes = fh.extract(parsed, {"stops": [{"name": "東京駅八重洲北口"}]})
    assert {r["direction"] for r in routes} == {"晴海二丁目ゆき", "晴海トリトンスクエアゆき"}


def test_extract_unknown_stop_yields_nothing(parsed):
    assert fh.extract(parsed, {"stops": [{"name": "呉服橋"}]}) == []


def test_extract_empty_parsed():
    assert fh.extract({"revision": None, "sections": {}, "stop_order": []}, HARUMI_CFG) == []


# --- config との整合 -------------------------------------------------------------
def test_config_stops_exist_in_pdf(parsed):
    """my_stops.yaml の harumi 停留所はすべて PDF の乗車停留所に存在すること。"""
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    for stop in (cfg.get("harumi") or {}).get("stops") or []:
        assert fh.normalize_name(stop["name"]) in parsed["stop_order"], stop["name"]
