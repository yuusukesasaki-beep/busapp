"""晴海ライナー取得・パース。

日立自動車交通の公式ページ(https://www.hitachi-gr.com/harumi-liner)から時刻表を取得する。
設計書 §2.3 は「時刻表は Web ページ」としていたが、実サイト(2026-11 時点・Wix 製)では
時刻表本体は PDF(「時刻表をダウンロード」ボタン)でしか提供されていない。
そのため §2.2 の方針どおり pdfplumber で PDF をパースする。

取得フロー(HTTP は fetch_* の2関数だけ):
  1. fetch_page()            : 公式ページ HTML を取得
  2. find_timetable_pdf_url(): HTML から「時刻表をダウンロード」リンクの PDF URL を抜く
                                (ダイヤ改正で PDF のファイル名が変わっても追従できる)
  3. fetch_pdf(url)          : PDF バイト列を取得
  4. parse_timetable_pdf()   : PDF → 中間構造(ダイヤ区分ごとの便リスト)
  5. extract()               : 中間構造 + my_stops.yaml の harumi 定義 → routes 配列(設計書 §3)

4・5 は純粋関数で、保存済みフィクスチャ(tests/fixtures/harumi/)だけでテストできる。
スクレイピングは1日1回(Actions の cron)。User-Agent に連絡先を明記。

PDF の構造(2026-07-18 改正版):
  A4 横 1 ページに「平日ダイヤ」「土休日ダイヤ」の2セクション。各セクションは
  行=停留所、列=便 の表で、
    - 2列目にマーク: 「発」(先頭行) / 「〃」(以降の乗車停留所) / 「降車のみ」(到着側)
    - 停留所名は「晴 海 二 丁 目」のように文字間に空白が入る
    - セル: 'H:MM' / 'ㇾ'(通過=東京駅まで行かず有楽町駅から晴海へ戻る便) /
            '＝'(運行なし) / '(H:MM)'(到着側の注記つき時刻) / 空
  同じセクションに綾瀬ライナー(青井駅⇔東京駅)の表も続くが、停留所名列が空の行
  (空行または注記行)で区切られているため、そこで晴海ライナーの表を打ち切る。

運行形態は 晴海トリトンスクエア → 晴海一〜三丁目 → 東銀座駅 → 有楽町駅 →
東京駅八重洲北口 → 日本橋髙島屋 → 晴海トリトンスクエア(降車のみ)の循環。
本モジュールでは乗車できる停留所を次の2方面に分ける:
  - 都心方面: 「発」〜有楽町駅(CITY_BOUND_END)まで。行き先は便ごとに
              東京駅八重洲北口 / 有楽町駅(ㇾ の便)
  - 晴海方面: 東京駅八重洲北口・日本橋髙島屋(RETURN_STOPS)。行き先は便ごとに
              晴海二丁目(降車側に晴海二丁目の到着時刻がある便) / 晴海トリトンスクエア
ダイヤは平日/土休日の2区分なので saturday には holiday と同じ配列を入れる(BRT と同じ)。

既知の制約(v1):
  - 呉服橋停留所は 2024-04 から休止中(PDF にも無い)。復活したら RETURN_STOPS に追加。
  - 綾瀬ライナー(2025-09 ダイヤ変更)は対象外。東京駅八重洲北口発の綾瀬方面便を
    晴海方面と取り違えないよう、綾瀬ライナーの表は読まない。
  - 有楽町駅から晴海へ直接戻る ㇾ 便は「有楽町駅ゆき」扱い(有楽町駅での晴海方面乗車は
    対象外)。年末年始・大規模交通規制時の臨時ダイヤは反映しない(鮮度表示+公式リンクで補う)。
"""

from __future__ import annotations

import io
import re
import unicodedata
from urllib.parse import urljoin

OPERATOR = "harumi"
OPERATOR_NAME = "晴海ライナー"
ROUTE_NAME = "晴海ライナー"

PAGE_URL = "https://www.hitachi-gr.com/harumi-liner"

# User-Agent には連絡先を明記する(設計書 §2.4)。TODO: 実運用の連絡先に差し替え。
USER_AGENT = "harumi-bus/0.1 (+https://github.com/USER/harumi-bus)"

#: ダイヤ区分(fetch_toei.BUCKETS と同じ並び)
BUCKETS = ("weekday", "saturday", "holiday")

#: PDF のセクション見出し → 出力バケット。土休日は saturday / holiday 両方に入れる。
SECTION_BUCKETS = {
    "平日": ("weekday",),
    "土休日": ("saturday", "holiday"),
}

#: 都心方面の乗車区間の終点(この停留所まで=都心方面、以降は晴海方面の乗車停留所)
CITY_BOUND_END = "有楽町駅"
#: 晴海方面(帰り)の乗車停留所。呉服橋は休止中(2024-04〜)。復活したら追加。
RETURN_STOPS = ("東京駅八重洲北口", "日本橋髙島屋")
#: 都心方面の代表行き先(この停留所の時刻がある便はこの行き先)
CITY_TERMINAL = "東京駅八重洲北口"
#: 晴海方面の行き先候補(降車側で到着時刻がある最後の停留所を採用)。
#: 先頭が基本の終点、以降は「引き続き運行」で延長される停留所。
RETURN_TERMINALS = ("晴海トリトンスクエア", "晴海一丁目", "晴海二丁目北", "晴海二丁目")

_TIME_RE = re.compile(r"^\(?\s*(\d{1,2})\s*[:;：]\s*(\d{2})\s*\)?$")
_SECTION_RE = re.compile(r"^(平日|土休日)ダイヤ(?:\s*(\d{4})年(\d{1,2})月(\d{1,2})日改正)?")


# --- 取得(HTTP はここだけ)-------------------------------------------------
def _get(url: str, timeout: int = 60):
    import requests

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_page() -> str:
    """公式ページの HTML を取得する。"""
    return _get(PAGE_URL).text


def fetch_pdf(url: str) -> bytes:
    """時刻表 PDF を取得してバイト列を返す。"""
    resp = _get(url)
    ctype = resp.headers.get("Content-Type", "")
    if "pdf" not in ctype and not resp.content.startswith(b"%PDF"):
        raise RuntimeError(f"時刻表 PDF ではない応答(Content-Type={ctype})")
    return resp.content


def fetch_timetable_pdf() -> bytes:
    """公式ページ → PDF URL 解決 → PDF バイト列(build_timetable から呼ぶ入口)。"""
    return fetch_pdf(find_timetable_pdf_url(fetch_page()))


# --- 正規化ヘルパー ----------------------------------------------------------
def normalize_name(s: str) -> str:
    """停留所名の揺れを吸収: NFKC + 文字間の空白除去(PDF は '晴 海 二 丁 目' 表記)。"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s)


def parse_time_cell(cell: str | None) -> str | None:
    """表のセル → 'HH:MM' または None(通過・運行なし・空)。

    '(13:03)' のような注記つきも時刻として扱う。'18;49' の誤植(; → :)も吸収する。
    """
    if not cell:
        return None
    m = _TIME_RE.match(unicodedata.normalize("NFKC", cell).strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh >= 48 or mm >= 60:
        return None
    return f"{hh % 24:02d}:{mm:02d}"


# --- ページ HTML → PDF URL ----------------------------------------------------
def find_timetable_pdf_url(html: str, base_url: str = PAGE_URL) -> str:
    """公式ページ HTML から時刻表 PDF の URL を返す。

    第一候補: aria-label / テキストが「時刻表をダウンロード」のリンク。
    第二候補: 「時刻表」という語の後ろで最初に現れる .pdf リンク。
    見つからなければ RuntimeError(ページ改修の検知)。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        label = (a.get("aria-label") or "") + " " + a.get_text(" ", strip=True)
        if "時刻表" in label and "ダウンロード" in label and a["href"].lower().split("?")[0].endswith(".pdf"):
            return urljoin(base_url, a["href"])
    # フォールバック: 「時刻表」見出し以降の最初の PDF リンク
    idx = html.find("時刻表")
    if idx >= 0:
        m = re.search(r'href="([^"]+\.pdf)"', html[idx:])
        if m:
            return urljoin(base_url, m.group(1))
    raise RuntimeError("公式ページに時刻表 PDF のリンクが見つかりません(ページ構造が変わった可能性)")


# --- PDF → 中間構造(純粋関数)---------------------------------------------
def _cell(row: list, i: int) -> str:
    return (row[i] or "").strip() if i < len(row) else ""


def _is_blank_row(row: list) -> bool:
    return all(not (c or "").strip() for c in row)


def _section_of(row: list) -> tuple[str, str | None] | None:
    """セクション見出し行なら (区分, 改正日 'YYYY-MM-DD' or None)。"""
    for c in row:
        if not c:
            continue
        m = _SECTION_RE.match(unicodedata.normalize("NFKC", c).strip())
        if m:
            rev = None
            if m.group(2):
                rev = f"{int(m.group(2)):04d}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
            return m.group(1), rev
    return None


def _parse_section_rows(rows: list[list]) -> list[dict]:
    """1セクションの表の行 → 便リスト [{'stops': {停留所: 'HH:MM'}, 'arrivals': {...}}]。

    「発」行から始め、停留所名列(2列目)が空になる行で打ち切る(綾瀬ライナー等の除外)。
    「降車のみ」マーク以降の行は arrivals(行き先判定用)、それ以前は stops(乗車可能)。
    便は列単位(時刻セルが1つでもある列)。
    """
    start = None
    for i, row in enumerate(rows):
        if _cell(row, 2) == "発":
            start = i
            break
    if start is None:
        return []

    board_rows: list[tuple[str, list]] = []
    arrive_rows: list[tuple[str, list]] = []
    arriving = False
    for row in rows[start:]:
        name = normalize_name(_cell(row, 1))
        if not name or "方面行" in name:
            break  # 晴海ライナーの表はここまで(以降は綾瀬ライナー/注記)
        marker = normalize_name(_cell(row, 2))
        if marker.startswith("降車"):
            arriving = True
        (arrive_rows if arriving else board_rows).append((name, row[3:]))

    if not board_rows:
        return []
    width = max(len(cells) for _, cells in board_rows + arrive_rows)
    trips: list[dict] = []
    for col in range(width):
        stops = {n: t for n, cells in board_rows
                 if (t := parse_time_cell(cells[col] if col < len(cells) else None))}
        if not stops:
            continue  # 空列(表の区切りなど)
        arrivals = {n: t for n, cells in arrive_rows
                    if (t := parse_time_cell(cells[col] if col < len(cells) else None))}
        trips.append({"stops": stops, "arrivals": arrivals})
    return trips


def parse_timetable_pdf(pdf_bytes: bytes) -> dict:
    """時刻表 PDF → 中間構造。

    {
      "revision": "2026-07-18" | None,
      "sections": {"平日": [trip, ...], "土休日": [trip, ...]},
      "stop_order": ["晴海トリトンスクエア", "晴海一丁目", ...]   # 乗車停留所の並び
    }
    trip は {"stops": {停留所: 'HH:MM'}, "arrivals": {停留所: 'HH:MM'}}。
    """
    import pdfplumber

    rows: list[list] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                rows.extend(table)

    # セクション見出しで分割
    sections: dict[str, list[list]] = {}
    revision: str | None = None
    current: str | None = None
    for row in rows:
        sec = _section_of(row)
        if sec:
            current, rev = sec
            revision = revision or rev
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(row)

    parsed = {name: _parse_section_rows(r) for name, r in sections.items()}

    # 乗車停留所の並び(最初のセクションの「発」行から降車行の手前まで)
    stop_order: list[str] = []
    for r in next(iter(sections.values()), []):
        if _cell(r, 2) == "発" or stop_order:
            name = normalize_name(_cell(r, 1))
            if not name or normalize_name(_cell(r, 2)).startswith("降車"):
                break
            stop_order.append(name)
    return {"revision": revision, "sections": parsed, "stop_order": stop_order}


# --- 中間構造 → routes(純粋関数)------------------------------------------
def _trip_legs(trip: dict, stop_order: list[str]) -> list[tuple[str, str, str]]:
    """1便 → [(方面ラベル, 停留所, 時刻), ...]。

    都心方面: stop_order の先頭〜CITY_BOUND_END。行き先は CITY_TERMINAL の時刻が
             あればそれ、無ければ都心方面で最後に時刻がある停留所。
    晴海方面: RETURN_STOPS。行き先は arrivals にある RETURN_TERMINALS の最後。
    """
    stops = trip["stops"]
    city_stops: list[str] = []
    for name in stop_order:
        city_stops.append(name)
        if name == CITY_BOUND_END:
            break
    legs: list[tuple[str, str, str]] = []

    city_times = [(n, stops[n]) for n in city_stops if n in stops]
    if city_times:
        if CITY_TERMINAL in stops:
            dest = CITY_TERMINAL
        else:
            dest = city_times[-1][0]
        legs += [(f"{dest}ゆき", n, t) for n, t in city_times]

    ret_times = [(n, stops[n]) for n in RETURN_STOPS if n in stops]
    if ret_times:
        dest = RETURN_TERMINALS[0]
        for cand in RETURN_TERMINALS:
            if cand in trip.get("arrivals", {}):
                dest = cand
        legs += [(f"{dest}ゆき", n, t) for n, t in ret_times]
    return legs


#: 方面ラベル → route id の接尾辞(出力順もこの並び)。未知の方面は misc-N で連番。
_DIRECTION_SLUGS = {
    "東京駅八重洲北口ゆき": "tokyo",
    "有楽町駅ゆき": "yurakucho",
    "晴海二丁目ゆき": "harumi2",
    "晴海トリトンスクエアゆき": "triton",
}


def _direction_order(direction: str) -> tuple[int, str]:
    known = list(_DIRECTION_SLUGS)
    return (known.index(direction) if direction in known else len(known), direction)


def extract(parsed: dict, harumi_cfg: dict) -> list[dict]:
    """中間構造 + my_stops.yaml の harumi 定義 → routes 配列(設計書 §3)。

    方面(行き先)ごとに1オブジェクトへまとめ、停留所は config の順。
    停留所ごとの directions は方面ラベルへの部分一致で絞る。
    土休日ダイヤは saturday / holiday の両方に入れる。
    """
    stop_order = parsed.get("stop_order") or []
    # grouped[direction][stop_name][bucket] = [times]
    grouped: dict[str, dict[str, dict[str, list[str]]]] = {}
    for sec_name, trips in (parsed.get("sections") or {}).items():
        buckets = SECTION_BUCKETS.get(sec_name)
        if not buckets:
            continue
        for trip in trips:
            for direction, stop, t in _trip_legs(trip, stop_order):
                per_stop = grouped.setdefault(direction, {}).setdefault(
                    stop, {b: [] for b in BUCKETS}
                )
                for b in buckets:
                    per_stop[b].append(t)

    routes: list[dict] = []
    misc_n = 0
    for direction in sorted(grouped, key=_direction_order):
        per_stop = grouped[direction]
        stops_out = []
        for cfg in harumi_cfg.get("stops") or []:
            name = normalize_name(cfg["name"])
            if name not in per_stop:
                continue
            dirs = [normalize_name(d) for d in (cfg.get("directions") or [])]
            if dirs and not any(d in direction for d in dirs):
                continue
            entry = {"stop_name": cfg["name"]}
            for b in BUCKETS:
                entry[b] = sorted(set(per_stop[name][b]), key=_sort_key)
            stops_out.append(entry)
        if not stops_out:
            continue
        suffix = _DIRECTION_SLUGS.get(direction)
        if suffix is None:
            misc_n += 1
            suffix = f"misc-{misc_n}"
        routes.append({
            "id": f"harumi-{suffix}",
            "operator": OPERATOR,
            "operator_name": OPERATOR_NAME,
            "route_name": ROUTE_NAME,
            "direction": direction,
            "stops": stops_out,
        })
    return routes


def _sort_key(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)
