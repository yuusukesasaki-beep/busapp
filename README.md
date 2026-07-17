# はるみバス

自宅周辺のバス停(都バス・東京BRT・晴海ライナー)の「次のバス」を表示するPWA。

- 設計: [`harumi-bus-design.md`](./harumi-bus-design.md)
- 開発ガイド: [`CLAUDE.md`](./CLAUDE.md)

## 構成

- `config/my_stops.yaml` — 抽出対象バス停の定義
- `pipeline/` — timetable.json を生成する Python パイプライン(uv 管理)
- `app/` — PWA本体(ビルドツールなしの素の HTML/JS/CSS)
- `data/` — 生成物(`timetable.json`。commit は Actions が行う)

## セットアップ

```bash
cp .env.example .env      # ODPT_TOKEN を記入(https://developer.odpt.org/)
uv sync                   # 依存関係をインストール
```

## よく使うコマンド

```bash
uv run pipeline/build_timetable.py --local   # ローカルで統合JSON生成(要 .env)
uv run pytest pipeline/tests/                # パーサテスト
python -m http.server 8000                   # 直下で配信 → http://localhost:8000/app/ を開く
```

## ステータス

Phase 1(都バス縦貫通)ほぼ完了。パイプライン(ODPT取得〜3区分JSON生成)+ Actions日次更新
+ Pages配信 + PWA本体まで稼働。公開: https://yuusukesasaki-beep.github.io/busapp/
