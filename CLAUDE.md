# CLAUDE.md — はるみバス

自宅周辺のバス停(都バス・東京BRT・晴海ライナー)の「次のバス」を表示するPWA。
詳細設計は `docs/harumi-bus-design.md` を必ず先に読むこと。

## プロジェクトの原則

- **アプリ側は時刻計算のみ、パースの複雑さは全てpipeline側に寄せる**。アプリが読むのは `data/timetable.json` 1ファイルだけ
- フロントはビルドツールなしの素のHTML/JS/CSS(単一構成を維持)。フレームワーク導入はしない
- pipelineはPython、環境管理は `uv` を使う(`uv run`, `uv add`)
- 時刻はすべてJST。GitHub ActionsはUTCなのでcron指定に注意(03:00 JST = 18:00 UTC 前日)

## 絶対に守ること

- ODPTのAPIキーは `ODPT_TOKEN`(GitHub Secrets / ローカルは `.env`)。**コード・ログ・コミットに絶対に含めない**。`.env` は `.gitignore` 済みであることを毎回確認
- スクレイピングは1日1回のみ。User-Agentに連絡先を明記。テストでは**保存済みフィクスチャを使い、実サイトへアクセスしない**
- `build_timetable.py` の妥当性チェック(便数が前回比±50%超で異常終了)を無効化しない
- `data/timetable.json` を手で編集しない(必ずpipelineから生成)

## ディレクトリ

- `config/my_stops.yaml` — 抽出対象バス停の定義(ここを編集すると対象が変わる)
- `pipeline/` — fetch_toei.py / fetch_brt.py / fetch_harumi.py / build_timetable.py
- `pipeline/tests/fixtures/` — 保存済みHTML・GTFS zip(パーサテスト用)
- `app/` — PWA本体(index.html, app.js, style.css, sw.js, manifest.webmanifest)
- `data/` — 生成物。コミットはActionsが行う

## よく使うコマンド

```bash
uv run pipeline/build_timetable.py --local   # ローカルで統合JSON生成(要 .env)
uv run pytest pipeline/tests/                # パーサテスト
python -m http.server 8000                   # リポジトリ直下で配信し http://localhost:8000/app/ を開く
```

## テスト方針

- 各パーサは「入力フィクスチャ → 期待JSON」のスナップショットテストを持つ
- 平日/土日祝判定は境界日(祝日・振替休日・年末年始)のケースを必ず含める
- サイト改修でパーサが壊れたら、新しいHTMLをフィクスチャに追加してから修正する

## 現在のフェーズ

Phase 1(都バス縦貫通)。フェーズ定義は設計書 §5 を参照。
フェーズをまたぐ変更を提案する場合は、先に理由を説明して確認を取ること。
