"""fetch_brt のパース・抽出ロジックのテスト。

実サイトへはアクセスせず、保存済み HTML フィクスチャ(fixtures/brt/*.html)で
parse_stop_page → extract を検証する(スナップショット方針)。

フィクスチャは実データの特徴を再現:
  - b33-harumi-flag: 2タブ(選手村ルート新橋行 / 幹線ルート豊洲市場前・国際展示場行)
  - b01-shimbashi:   4タブ。幹線ルートのテーブルに余分な </div> がある壊れた
                     マークアップ(実サイト由来)→ html5lib での回復を回帰テスト
  - b22-harumi-brt-terminal: 2タブ(都心方向は新橋・虎ノ門ヒルズ行の1タブ)
"""

import re
from pathlib import Path

import yaml

import fetch_brt

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "brt"
CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "my_stops.yaml"

HHMM = re.compile(r"^\d{2}:\d{2}$")


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# --- parse_stop_page --------------------------------------------------------
def test_parse_b33_tabs():
    tabs = fetch_brt.parse_stop_page(_load("b33-harumi-flag.html"))
    assert [(t["route_name"], t["direction"]) for t in tabs] == [
        ("選手村ルート", "新橋ゆき"),
        ("幹線ルート", "豊洲市場前・国際展示場ゆき"),
    ]
    assert tabs[0]["dest_codes"] == ["B01"]
    assert tabs[1]["dest_codes"] == ["B03", "B05"]


def test_parse_b33_times():
    tab = fetch_brt.parse_stop_page(_load("b33-harumi-flag.html"))[0]
    # 平日: 始発 06:28 / 最終 22:13 / 75便(5時台の空セルは読み飛ばす)
    assert tab["weekday"][0] == "06:28"
    assert tab["weekday"][-1] == "22:13"
    assert len(tab["weekday"]) == 75
    # 土休日は別ダイヤ
    assert tab["holiday"][0] == "06:30"
    assert len(tab["holiday"]) == 62
    # すべて HH:MM 形式・時系列順
    for times in (tab["weekday"], tab["holiday"]):
        assert all(HHMM.match(t) for t in times)
        assert times == sorted(times)


def test_parse_b01_labels():
    tabs = fetch_brt.parse_stop_page(_load("b01-shimbashi.html"))
    dirs = [t["direction"] for t in tabs]
    assert dirs == [
        "国際展示場・東京テレポートゆき",
        "HARUMI FLAG(晴海五丁目ターミナル)ゆき",  # 単語内の空白は保持
        "晴海BRTターミナル・豊洲・ミチノテラス豊洲ゆき",
        "虎ノ門ヒルズゆき",
    ]
    # ルート名括弧の無いタブは事業者名で代用
    assert tabs[3]["route_name"] == "東京BRT"


def test_parse_b01_broken_markup_recovered():
    """幹線ルートの表は余分な </div> を含む(実サイト由来の壊れたHTML)。

    html.parser だと 07:25 でテーブルが打ち切られ 4便になる。html5lib で
    全便(平日118/土休日82)拾えることの回帰テスト。
    """
    kansen = fetch_brt.parse_stop_page(_load("b01-shimbashi.html"))[0]
    assert kansen["route_name"] == "幹線ルート"
    assert len(kansen["weekday"]) == 118
    assert kansen["weekday"][-1] == "22:32"
    assert len(kansen["holiday"]) == 82


def test_parse_b22_tabs():
    tabs = fetch_brt.parse_stop_page(_load("b22-harumi-brt-terminal.html"))
    assert [(t["route_name"], t["direction"]) for t in tabs] == [
        ("晴海・豊洲ルート", "新橋・虎ノ門ヒルズゆき"),
        ("豊洲ルート", "豊洲・ミチノテラス豊洲ゆき"),
    ]
    assert tabs[0]["weekday"][0] == "06:04"
    assert len(tabs[0]["weekday"]) == 56


# --- extract ----------------------------------------------------------------
def _pages():
    return {
        "晴海五丁目ターミナル": _load("b33-harumi-flag.html"),
        "晴海BRTターミナル": _load("b22-harumi-brt-terminal.html"),
        "新橋": _load("b01-shimbashi.html"),
    }


BRT_CFG = {
    "stops": [
        {"name": "晴海五丁目ターミナル", "directions": ["新橋"]},
        {"name": "晴海BRTターミナル", "directions": ["新橋", "虎ノ門ヒルズ"]},
        {"name": "新橋", "directions": ["晴海"]},
    ],
    "routes": [],
}


def test_extract_directions_filter():
    routes = fetch_brt.extract(_pages(), BRT_CFG)
    got = {(r["direction"], s["stop_name"]) for r in routes for s in r["stops"]}
    assert got == {
        ("新橋ゆき", "晴海五丁目ターミナル"),
        ("新橋・虎ノ門ヒルズゆき", "晴海BRTターミナル"),
        # 新橋からの帰り: 「晴海」を含む2方面(幹線ルートは晴海に停まらないので出ない)
        ("HARUMI FLAG(晴海五丁目ターミナル)ゆき", "新橋"),
        ("晴海BRTターミナル・豊洲・ミチノテラス豊洲ゆき", "新橋"),
    }


def test_extract_schema():
    routes = fetch_brt.extract(_pages(), BRT_CFG)
    for r in routes:
        assert r["operator"] == "brt"
        assert r["operator_name"] == "東京BRT"
        assert r["id"].startswith("brt-b")
        for s in r["stops"]:
            assert set(s) == {"stop_name", "weekday", "saturday", "holiday"}


def test_extract_saturday_equals_holiday():
    """BRT は土休日ダイヤ(2区分)→ saturday は holiday と同内容の別リスト。"""
    routes = fetch_brt.extract(_pages(), BRT_CFG)
    for r in routes:
        for s in r["stops"]:
            assert s["saturday"] == s["holiday"]
            assert s["saturday"] is not s["holiday"]  # 共有せずコピー


def test_extract_no_directions_includes_all_tabs():
    cfg = {"stops": [{"name": "晴海BRTターミナル"}]}
    routes = fetch_brt.extract(
        {"晴海BRTターミナル": _load("b22-harumi-brt-terminal.html")}, cfg
    )
    assert len(routes) == 2


def test_extract_groups_same_direction_across_stops():
    """同じ(ルート, 方面)タブを持つ停留所は1つの route にまとまり、config 順を保つ。"""
    html = _load("b22-harumi-brt-terminal.html")
    cfg = {
        "stops": [
            {"name": "晴海中央", "directions": ["新橋"]},
            {"name": "晴海BRTターミナル", "directions": ["新橋"]},
        ]
    }
    routes = fetch_brt.extract({"晴海中央": html, "晴海BRTターミナル": html}, cfg)
    assert len(routes) == 1
    assert [s["stop_name"] for s in routes[0]["stops"]] == ["晴海中央", "晴海BRTターミナル"]


# --- config との整合 ---------------------------------------------------------
def test_stop_slugs_cover_config():
    """my_stops.yaml の brt 停留所はすべて STOP_SLUGS に URL 対応を持つこと。"""
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    for stop in (cfg.get("brt") or {}).get("stops") or []:
        assert fetch_brt.normalize_name(stop["name"]) in fetch_brt.STOP_SLUGS, stop["name"]
