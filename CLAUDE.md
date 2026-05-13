# JKK空き家監視スクリプト

JKK（東京都住宅供給公社）の空き家情報を定期取得し、変化があればLINEで通知するPythonスクリプト。

## メインファイル

- `jkk_line_notify.py` — 唯一の本番スクリプト。スクレイピング・差分検出・LINE通知をすべて担う
- `requirements.txt` — 依存パッケージ
- `user_prefs.json` — LINEユーザーID → 通知エリア設定（LINE webhookが更新する）

## 状態管理ファイル（last_*.json）

スクリプトが前回実行時の状態を保存するファイル群。GitHub Actionsがコミットして差分を追跡する。

| ファイル                  | 内容                        |
|-----------------------|---------------------------|
| `last_data.json`      | 物件名 → 合算件数               |
| `last_rooms.json`     | 入れ替わり検知用ハッシュ             |
| `last_rooms_detail.json` | 間取り別件数                 |
| `last_images.json`    | 物件名 → 外観画像URL            |
| `last_location.json`  | 物件名 → 地域                 |
| `last_rates.json`     | 物件名 → 間取り → {面積, 家賃, 管理費} |

## 実行方法

```bash
# 通常実行
python jkk_line_notify.py

# テスト送信（last_data.jsonの先頭1件でLINE通知テスト）
python jkk_line_notify.py --test

# データディレクトリを変更して実行
JKK_DATA_DIR=/path/to/data python jkk_line_notify.py
```

## GitHub Actions

`.github/workflows/` で定期実行。スクリプト実行後、`last_*.json` の変化をコミットして差分を永続化する。

## 連携先

- **jkk-akiya-data リポジトリ**：`vacancies.json` を push → JKK-WEBサイトが表示に使用
- **LINE Messaging API**：通知送信 + webhook受信（地域フィルター設定）
- **jkk-akiya-monitor リポジトリ**：`user_prefs.json` の読み書き

## 注意

- `user_prefs.json` はLINE webhookとスクリプトの両方が読み書きするため、競合に注意
- HTMLはすべて `.gitignore` で除外済み（`*.html`）。デバッグ用HTMLはリポジトリに含めない
