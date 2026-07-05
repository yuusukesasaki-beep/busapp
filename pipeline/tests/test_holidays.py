"""holidays.parse_holidays のテスト(内閣府CSVの保存済みフィクスチャを使用)。

境界ケース(振替休日=「休日」行・年末年始・年フィルタの端)を含める(CLAUDE.md テスト方針)。
実サイトへはアクセスしない。
"""

from pathlib import Path

import holidays

FIXTURE = Path(__file__).parent / "fixtures" / "syukujitsu_sample.csv"


def _csv_bytes() -> bytes:
    return FIXTURE.read_bytes()


def test_parse_all_years():
    result = holidays.parse_holidays(_csv_bytes())
    # 前年・翌年も含め、全行が ISO 日付になる
    assert result[0] == "2025-12-23"
    assert result[-1] == "2027-01-01"
    assert "2026-07-20" in result  # 海の日(ゼロ埋め確認)


def test_year_filter_excludes_boundary_years():
    result = holidays.parse_holidays(_csv_bytes(), year_from=2026, year_to=2026)
    assert "2025-12-23" not in result  # 前年は除外
    assert "2027-01-01" not in result  # 翌年は除外
    assert result == [
        "2026-01-01",
        "2026-01-12",
        "2026-02-11",
        "2026-05-03",
        "2026-05-04",
        "2026-05-05",
        "2026-05-06",
        "2026-07-20",
    ]


def test_substitute_holiday_included():
    """振替休日(CSV上は「休日」行)も取り込まれる。"""
    result = holidays.parse_holidays(_csv_bytes(), year_from=2026, year_to=2026)
    assert "2026-05-06" in result


def test_sorted_and_zero_padded():
    result = holidays.parse_holidays(_csv_bytes(), year_from=2026, year_to=2026)
    assert result == sorted(result)
    assert all(len(d) == 10 and d.count("-") == 2 for d in result)
