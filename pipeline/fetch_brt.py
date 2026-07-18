"""東京BRT取得・パース。

公式サイトのバス停別時刻表ページ(https://tokyo-brt.co.jp/bus-stops/<slug>)を
HTML パースする。ODPT カタログに東京BRTの GTFS が無いことは確認済み
(2026-07-18、https://ckan.odpt.org/dataset で「東京BRT」「BRT」とも該当なし)。

設計方針(CLAUDE.md / 設計書 §2.4):
  - 「取得(HTTP)」と「変換(パース)」を分離。取得は fetch_stop_page() のみ。
    parse_stop_page() / extract() は HTML 文字列を受け取る純粋関数で、
    保存済みフィクスチャ(tests/fixtures/brt/*.html)だけでテストできる。
  - スクレイピングは1日1回(Actions の cron)。User-Agent に連絡先を明記。

ページ構造(2026-03 改定時点):
  #stop-time-table > .tab-box に方面タブ。label.tab(for=tabN)がタブ見出し
  (例「（選手村ルート）【B01】新橋 行」)、div#tabItemN が本体で、
  table.table-tt.weekday(平日)と table.table-tt.holiday(土休日)を1つずつ持つ。
  行の th が「時」、td 内の .item > .time が「分」。空セルは &nbsp;。

既知の制約(v1):
  - ダイヤは平日/土休日の2区分。出力の saturday には holiday と同じ配列を入れる
    (アプリの3区分計算をそのまま使うため)。
  - .sub の注記マーク(行き先変化。例「テレ=東京テレポート行」)は無視して
    全便を含める。マークは終点の違いを示すだけで、当該バス停からの発車自体は
    全便有効なため「次のバス」用途では問題ない。
"""

from __future__ import annotations

import re
import unicodedata

OPERATOR = "brt"
OPERATOR_NAME = "東京BRT"

BASE_URL = "https://tokyo-brt.co.jp/bus-stops/"

# User-Agent には連絡先を明記する(設計書 §2.4)。TODO: 実運用の連絡先に差し替え。
USER_AGENT = "harumi-bus/0.1 (+https://github.com/USER/harumi-bus)"

#: 停留所名(正規化後)→ ページ slug。公式バス停一覧ページの表記に合わせる。
#: config/my_stops.yaml に停留所を足すときはここにも対応を追加すること。
STOP_SLUGS = {
    "虎ノ門ヒルズ": "b11-toranomon-hills",
    "新橋": "b01-shimbashi",
    "勝どきBRT": "b02-Kachidoki-brt",
    "晴海中央": "b21-harumi-chuo",
    "晴海BRTターミナル": "b22-harumi-brt-terminal",
    "はるみらい": "b31-harumirai",
    "晴海ふ頭公園": "b32-harumi-futo-park",
    "晴海五丁目ターミナル": "b33-harumi-flag",  # HARUMI FLAG(晴海五丁目ターミナル)
    "豊洲": "b23-toyosu",
    "豊洲市場前": "b03-toyosu-shijomae",
    "ミチノテラス豊洲": "b03-michino-terrace-toyosu",
    "有明テニスの森": "b04-ariaketennis-no-mori",
    "国際展示場": "b05-kokusaitenjijo",
    "東京テレポート": "b06-tokyo-teleport",
}

#: ダイヤ区分(fetch_toei.BUCKETS と同じ並び)
BUCKETS = ("weekday", "saturday", "holiday")


# --- 取得(HTTP はここだけ)-------------------------------------------------
def fetch_stop_page(stop_name: str) -> str:
    """停留所名 → 公式時刻表ページの HTML。名前は STOP_SLUGS で解決する。"""
    import requests

    slug = STOP_SLUGS.get(normalize_name(stop_name))
    if slug is None:
        raise RuntimeError(
            f"東京BRT停留所 '{stop_name}' の URL が未登録です"
            "(fetch_brt.STOP_SLUGS に追加してください)"
        )
    resp = requests.get(
        BASE_URL + slug, headers={"User-Agent": USER_AGENT}, timeout=60
    )
    resp.raise_for_status()
    return resp.text


# --- 正規化ヘルパー ----------------------------------------------------------
def normalize_name(s: str) -> str:
    """全角/半角・空白の揺れを吸収(NFKC。全角括弧→半角、nbsp→空白になる)。"""
    return unicodedata.normalize("NFKC", s or "").strip()


def _clean_text(s: str) -> str:
    """タブ見出しなどの表示テキスト → 比較しやすい1行文字列。"""
    return re.sub(r"\s+", " ", normalize_name(s)).strip()


# --- パース(純粋関数)------------------------------------------------------
def _parse_label(text: str) -> tuple[str, str, list[str]]:
    """タブ見出し → (route_name, direction, 行き先コード列)。

    例: '(選手村ルート) 【B01】新橋 行' → ('選手村ルート', '新橋ゆき', ['B01'])
    ルート名の括弧が無い見出し(例 '【B11】虎ノ門ヒルズ 行')は route_name を
    OPERATOR_NAME で代用する。
    """
    codes = re.findall(r"【([A-Za-z]\d+)】", text)
    m = re.match(r"^\((.+?)\)\s*(.*)$", text)
    route_name, rest = (m.group(1), m.group(2)) if m else (OPERATOR_NAME, text)
    dest = re.sub(r"【[^】]*】", "", rest)
    dest = re.sub(r"\s+", " ", dest).strip()  # 単語内の空白は保持(HARUMI FLAG)
    dest = re.sub(r"\s*行$", "", dest)
    return route_name, f"{dest}ゆき", codes


def _parse_table(table) -> list[str]:
    """table.table-tt 1つ → 'HH:MM' 配列(表の並び=時系列順を維持)。

    th が「時」、.item > .time が「分」。空セル(&nbsp;)や数字以外は読み飛ばす。
    """
    if table is None:
        return []
    times: list[str] = []
    for tr in table.select("tbody tr"):
        th = tr.find("th")
        if th is None:
            continue
        hh = normalize_name(th.get_text())
        if not hh.isdigit():
            continue
        hour = int(hh) % 24  # 深夜表記(24時台)は実時計に丸める(toei と同じ扱い)
        for time_el in tr.select("td .item .time"):
            mm = normalize_name(time_el.get_text())
            if not mm.isdigit():
                continue
            times.append(f"{hour:02d}:{int(mm):02d}")
    return times


def parse_stop_page(html: str) -> list[dict]:
    """時刻表ページ HTML → 方面タブごとの中間構造のリスト。

    [{label, route_name, direction, dest_codes, weekday, holiday}, ...]
    タブの並びはページの表示順(label の DOM 順)を維持する。
    """
    from bs4 import BeautifulSoup

    # 公式サイトの HTML は余分な </div> を含むことがある(新橋・幹線ルートで実例)。
    # html.parser はそこでテーブルを打ち切るため、ブラウザ同等の回復をする html5lib を使う。
    soup = BeautifulSoup(html, "html5lib")
    root = soup.find(id="stop-time-table") or soup
    tabs: list[dict] = []
    for label in root.select(".tab-menu label.tab"):
        m = re.search(r"(\d+)$", label.get("for") or "")
        if not m:
            continue
        item = root.find("div", id=f"tabItem{m.group(1)}")
        if item is None:
            continue
        txt_el = label.select_one(".txt") or label
        for a in txt_el.select("a"):  # 「印刷」ボタンを除去
            a.decompose()
        label_text = _clean_text(txt_el.get_text(""))
        route_name, direction, codes = _parse_label(label_text)
        tabs.append({
            "label": label_text,
            "route_name": route_name,
            "direction": direction,
            "dest_codes": codes,
            "weekday": _parse_table(item.select_one("table.table-tt.weekday")),
            "holiday": _parse_table(item.select_one("table.table-tt.holiday")),
        })
    return tabs


def _slug(codes: list[str]) -> str:
    return "brt-" + ("-".join(c.lower() for c in codes) if codes else "misc")


def extract(pages: dict[str, str], brt_cfg: dict) -> list[dict]:
    """{停留所名: HTML} + my_stops.yaml の brt 定義 → routes 配列(設計書 §3)。

    (route_name, direction) ごとに1オブジェクトへまとめ、停留所は config の順。
    停留所ごとの directions はタブ見出し(方面ラベル)への部分一致で絞る。
    BRT は土休日ダイヤのため saturday には holiday と同じ時刻を入れる。
    """
    grouped: dict[tuple[str, str], dict] = {}
    for cfg in brt_cfg.get("stops") or []:
        name = cfg["name"]
        html = pages.get(name)
        if not html:
            continue
        dirs = [normalize_name(d) for d in (cfg.get("directions") or [])]
        for tab in parse_stop_page(html):
            if dirs and not any(d in tab["label"] for d in dirs):
                continue  # この停留所では見ない方面
            key = (tab["route_name"], tab["direction"])
            route = grouped.setdefault(key, {
                "id": _slug(tab["dest_codes"]),
                "operator": OPERATOR,
                "operator_name": OPERATOR_NAME,
                "route_name": tab["route_name"],
                "direction": tab["direction"],
                "stops": [],
            })
            route["stops"].append({
                "stop_name": name,
                "weekday": tab["weekday"],
                "saturday": list(tab["holiday"]),  # 土休日ダイヤ(2区分)
                "holiday": tab["holiday"],
            })
    return list(grouped.values())
