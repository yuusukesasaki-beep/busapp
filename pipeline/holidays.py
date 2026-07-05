"""祝日データ取り込み(内閣府 syukujitsu.csv)。

内閣府が公開する「国民の祝日」CSV(Shift_JIS)から、timetable.json の
holidays 配列(ISO日付 'YYYY-MM-DD' のリスト)を生成する。
振替休日は CSV 側に「休日」行として含まれるため、そのまま取り込まれる。

設計方針(CLAUDE.md / 設計書 §3):
  - アプリに祝日ロジックを持たせず、Actions 側で祝日一覧を同梱する。
  - 「取得(HTTP)」と「パース」を分離。取得は fetch_syukujitsu_csv() のみ。
    パースは保存済みフィクスチャでテストする(実サイトへアクセスしない)。
"""

from __future__ import annotations

import csv
import io

SYUKUJITSU_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"

# User-Agent には連絡先を明記する(設計書 §2.4)。TODO: 実運用の連絡先に差し替え。
USER_AGENT = "harumi-bus/0.1 (+https://github.com/USER/harumi-bus)"


def fetch_syukujitsu_csv() -> bytes:
    """内閣府 syukujitsu.csv を取得して bytes を返す(Shift_JIS のまま)。

    アクセスは1日1回・User-Agent に連絡先明記(設計書 §2.4)。
    """
    import requests

    resp = requests.get(
        SYUKUJITSU_URL, headers={"User-Agent": USER_AGENT}, timeout=30
    )
    resp.raise_for_status()
    return resp.content


def parse_holidays(
    csv_bytes: bytes,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[str]:
    """syukujitsu.csv のバイト列 → ソート済み ISO 日付リスト。

    year_from / year_to を与えると、その年(両端含む)だけに絞る。
    CSV は Shift_JIS(cp932)、日付列は 'YYYY/M/D' 形式、先頭行はヘッダ。
    """
    text = csv_bytes.decode("cp932")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    dates: list[str] = []
    for row in rows[1:]:  # 先頭はヘッダ
        if not row or not row[0].strip():
            continue
        raw = row[0].strip()
        try:
            y, m, d = (int(x) for x in raw.split("/"))
        except ValueError:
            continue  # ヘッダ再掲や空行など想定外はスキップ
        if year_from is not None and y < year_from:
            continue
        if year_to is not None and y > year_to:
            continue
        dates.append(f"{y:04d}-{m:02d}-{d:02d}")

    return sorted(dates)
