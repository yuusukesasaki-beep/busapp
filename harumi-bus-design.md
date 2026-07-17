# はるみバス 設計書

自宅周辺のバス停(都バス・東京BRT・晴海ライナー)の「次のバス」を、アプリを開くだけで確認できるアプリ。最終形はAndroidで動作させる。

---

## 1. 全体アーキテクチャ

サーバーレス構成。**GitHub Actions + GitHub Pages(静的JSON) + PWA** の3層。

```
┌─ データ層(毎日自動更新)──────────────────────────┐
│ GitHub Actions (cron: 毎日 03:00 JST)                  │
│  ├ 都バス     : ODPT から ToeiBus-GTFS.zip を取得      │
│  ├ 東京BRT    : 公式サイトの時刻表をパース             │
│  ├ 晴海ライナー: 公式サイトの時刻表をパース            │
│  └ → 対象バス停のみ抽出した timetable.json を生成      │
│     → GitHub Pages に commit & deploy                  │
└────────────────────────────────────────┘
┌─ 配信層 ─────────────────────────────────┐
│ GitHub Pages: /data/timetable.json + PWA本体(静的)     │
└────────────────────────────────────────┘
┌─ アプリ層 ────────────────────────────────┐
│ PWA(単一HTML/JS + Service Worker + manifest)           │
│  ├ 起動時に timetable.json を fetch(キャッシュ併用)    │
│  ├ 「あと○分」「1時間以内の便」を計算・表示           │
│  └ バス停登録は端末内保存                              │
│ Android化: ホーム画面追加 → 将来 TWA で Play 配布可    │
└────────────────────────────────────────┘
```

### この構成を選ぶ理由
- **CORS回避**: ブラウザから他社サイトを直接fetchできない問題を、Actionsでの事前取得+同一オリジンの静的JSON配信で解決
- **無料・サーバー管理不要**: GitHub無料枠で完結
- **既存スキルセットと一致**: Claude Code + GitHub の普段のワークフローだけで開発・運用できる
- **障害に強い**: パースが失敗しても前日のJSONが残るため、アプリは古いデータで動き続ける(鮮度を`generated_at`で表示)

---

## 2. データソース仕様

### 2.1 都バス(難易度: 低)
- **提供元**: 公共交通オープンデータセンター(ODPT) https://developer.odpt.org/
- **必要な準備**: 無料のユーザー登録 → APIキー(アクセストークン)取得 → **GitHub Secrets `ODPT_TOKEN` に保存**
- **静的データ**: `ToeiBus-GTFS.zip`(GTFS-JP形式)。`stops.txt` / `stop_times.txt` / `trips.txt` / `calendar.txt` / `calendar_dates.txt` を結合して、対象バス停・方面ごとの発車時刻リストを作る
- **動的データ(Phase 3)**: GTFS-RT(VehiclePosition / TripUpdate、Protocol Buffers形式)。`gtfs-realtime-bindings` でパース
- **ライセンス**: 公共交通オープンデータ基本ライセンス。アプリ内にクレジット表記を入れること

### 2.2 東京BRT(難易度: 中)
- **提供元**: 公式サイト https://tokyo-brt.co.jp/ (バス停ごとの時刻表ページ / PDF)
- **方針**: バス停別時刻表ページのHTMLパースを第一候補。構造が不安定ならPDFパース(pdfplumber)にフォールバック
- **注意**: ODPTにGTFSが提供されているか実装時に再確認(カタログ: https://ckan.odpt.org/dataset で "東京BRT" を検索)。あればGTFSに乗り換えて都バスと同じ処理に統合
- **リアルタイム**: 公式「バスナビ」あり。Phase 3 で調査

### 2.3 晴海ライナー(難易度: 中・低頻度更新)
- **提供元**: 日立自動車交通の公式ページ(時刻表はWebページ)
- **方針**: HTMLパース。ダイヤ改正は年数回程度なので、パース失敗時は前回データを維持し、失敗をIssue自動起票で通知する運用で十分
- **注意**: 2025年9月から綾瀬ライナー関連のダイヤ変更あり。呉服橋停留所は休止中

### 2.4 スクレイピングの心得
- User-Agent明記、アクセスは1日1回、robots.txt尊重
- パーサは「取得HTML→中間構造→時刻配列」を分離し、サイト改修時はパーサだけ直せる構造に
- パース結果の**妥当性チェック**(便数が前回比±50%を超えたら異常としてデプロイ中止)を必ず入れる

---

## 3. 統合JSONスキーマ

`/data/timetable.json`(アプリが読む唯一のファイル)

```json
{
  "generated_at": "2026-07-05T03:00:12+09:00",
  "sources": {
    "toei":   {"status": "ok", "fetched_at": "..."},
    "brt":    {"status": "ok", "fetched_at": "..."},
    "harumi": {"status": "stale", "fetched_at": "...", "note": "パース失敗のため前回データ"}
  },
  "holidays": ["2026-07-20", "2026-08-11"],
  "routes": [
    {
      "id": "brt-kansen-shimbashi",
      "operator": "brt",
      "operator_name": "東京BRT",
      "route_name": "幹線ルート",
      "direction": "新橋ゆき",
      "stops": [
        {
          "stop_name": "HARUMI FLAG",
          "weekday":  ["06:10", "06:20", "..."],
          "saturday": ["06:25", "..."],
          "holiday":  ["06:30", "..."]
        }
      ]
    }
  ]
}
```

設計ポイント:
- アプリ側は**時刻計算だけ**に徹する(パースの複雑さは全部Actions側に寄せる)
- ダイヤ区分は **平日 / 土曜 / 休日(日祝)の3区分**(`weekday` / `saturday` / `holiday`)。
  都バス実データは土曜と日曜でダイヤが異なるため2区分では毎週末に不正確になる。
  アプリの区分選択: その日が「祝日 または 日曜」→ holiday、「土曜」→ saturday、それ以外→ weekday。
- `sources.status` で鮮度をUI表示("⚠ 晴海ライナーは7/3時点のデータ")
- 祝日はActions側で内閣府CSV(syukujitsu.csv)から生成して同梱 → アプリは holidays 配列を見て
  祝日を休日ダイヤに割り当てるだけ(祝日ロジックは持たない)
- 対象バス停はリポジトリの `config/my_stops.yaml` で管理(全路線を入れずファイルを小さく保つ)
- **既知の制約**: お盆・夏期などの特別ダイヤ(GTFSで calendar.txt に週フラグが無く
  calendar_dates だけで運行日が定義される service、年10数日程度)は v1 では対象外。
  鮮度表示と「公式で確認」リンクで補う(§7)。

---

## 4. リポジトリ構成

```
harumi-bus/
├ CLAUDE.md                  # Claude Code向け開発ガイド(別ファイル参照)
├ config/
│  └ my_stops.yaml           # 抽出対象のバス停・系統・方面の定義
├ pipeline/                  # Python(uv管理)
│  ├ fetch_toei.py           # ODPT GTFS-JP取得・抽出
│  ├ fetch_brt.py            # 東京BRTパース
│  ├ fetch_harumi.py         # 晴海ライナーパース
│  ├ build_timetable.py      # 統合JSON生成+妥当性チェック
│  └ tests/                  # 保存済みHTML/GTFSフィクスチャでパーサをテスト
├ app/                       # PWA(ビルドツールなし・素のHTML/JS)
│  ├ index.html
│  ├ app.js / style.css
│  ├ sw.js                   # Service Worker(オフラインキャッシュ)
│  └ manifest.webmanifest
├ .github/workflows/
│  ├ update-timetable.yml    # cron 毎日03:00 JST + 手動実行
│  └ deploy-pages.yml
└ data/                      # 生成物(timetable.json)
```

---

## 5. 実装フェーズ計画

### Phase 1: 都バスで縦に貫通させる(最初のマイルストーン)
1. リポジトリ作成、`config/my_stops.yaml` 定義
2. ODPT登録 → `ODPT_TOKEN` をGitHub Secretsへ
3. `fetch_toei.py`: GTFS-JP取得 → 対象バス停の時刻抽出(calendar.txtで平日/土日祝を判定)
4. `build_timetable.py` + Actions(cron) + Pages デプロイ
5. プロトタイプHTML(作成済み)の仮ダイヤ部分を `fetch('./data/timetable.json')` に差し替え
6. バス停登録の端末内保存、PWA化(manifest + Service Worker)

**完了条件**: スマホのホーム画面から開いて、都05-2の実ダイヤで「あと○分」が正しく出る

### Phase 2: 東京BRT・晴海ライナー対応
1. ODPTカタログで東京BRTのGTFS有無を確認 → あればGTFS、なければHTMLパース実装
2. `fetch_harumi.py` 実装(フィクスチャテスト必須)
3. 妥当性チェック+失敗時のIssue自動起票
4. UI: 鮮度表示、3社横断「今一番早く来るバス」表示

### Phase 3: リアルタイム対応(都バスGTFS-RT)
- TripUpdateで遅延反映(「12:34発 → 3分遅れ」)
- 注意: GTFS-RTは常時ポーリングが必要 → 静的構成では実現できないため、**Cloudflare Workers(無料枠)を1枚だけ**追加してRT情報の中継+APIキー秘匿を行う
- 東京BRTバスナビの調査もここで

### Phase 4: Android本格化
- まずは「ホーム画面に追加」で実用開始(Phase 1完了時点で可能)
- Playストア配布したくなったら **TWA(Bubblewrap)** でパッケージ化。WebViewラッパーより軽く、PWAの更新がそのまま反映される
- ネイティブ機能(通知・ウィジェット)が欲しくなったら Capacitor 移行を検討

---

## 6. セキュリティ・運用メモ

- ODPT APIキーは **GitHub Secrets のみ**に保存。コード・Confluence・JSONへの混入禁止(Actionsのログにも出さない)
- リポジトリをpublicにする場合、`my_stops.yaml` は自宅最寄りバス停の情報になる点に留意 → **privateリポジトリ推奨**(GitHub Pagesはprivateでも有料プランなら可。無料で運用するならJSONをpublicの別リポジトリかGistに置くか、バス停名を含む構成を許容するか選択)
- ライセンス表記: ODPT(公共交通オープンデータ基本ライセンス)のクレジットをアプリのabout欄に記載
- スクレイピング対象サイトの利用規約を実装前に一読

## 7. 既知のリスク

| リスク | 影響 | 対策 |
|---|---|---|
| BRT/晴海ライナーのサイト改修 | パース失敗 | 前回データ維持+Issue通知、フィクスチャテスト |
| ODPTのGTFS仕様変更 | 都バス更新停止 | 同上 |
| 臨時ダイヤ・運休 | 表示と実運行の乖離 | 鮮度表示+「公式サイトで確認」リンクを常設 |
| GitHub Actions無料枠 | 超過 | 1日1回・数分の実行なので実質問題なし |
