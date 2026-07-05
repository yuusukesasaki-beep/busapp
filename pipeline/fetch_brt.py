"""東京BRT取得・パース。

第一候補はバス停別時刻表ページの HTML パース、不安定なら PDF パース(pdfplumber)。
ただし ODPT に GTFS が提供されていれば fetch_toei と同じ GTFS 処理に統合する。

スクレイピングは1日1回のみ。User-Agent に連絡先を明記。
テストは保存済みフィクスチャを使い、実サイトへアクセスしない(設計書 §2.4)。

実装は Phase 2。
"""

# TODO(Phase 2):
#   - ODPT カタログで東京BRTの GTFS 有無を確認(https://ckan.odpt.org/dataset)
#   - GTFS があれば toei と統合、無ければ HTML パース(取得HTML→中間構造→時刻配列)
