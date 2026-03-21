# JKK 空き家数差分表示スクリプト

> **要件・仕様（Claude Code 等で一括実装する場合）**  
> - [`docs/01_要件定義書.md`](docs/01_要件定義書.md)  
> - [`docs/02_仕様書.md`](docs/02_仕様書.md)

JKKの検索結果ページ（空き家数変更）から **物件名・号室・現在の空き家数** を取得し、前回実行時との差分を比較して、**空き家数が増えた物件だけ**を表示します。

## 使い方

```bash
# 依存関係（初回のみ）
pip install -r requirements.txt

# 実行
python jkk_akiya_diff.py
```

またはプロジェクト内の venv を使う場合:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python jkk_akiya_diff.py
```

- **初回実行**: 現在の一覧を取得し `jkk_previous_state.json` に保存。この時点では「前回」がないため増加分は表示されません。
- **2回目以降**: 今回の一覧と前回保存分を比較し、**空き家数が増えた物件のみ**を表示します。

## ファイル

| ファイル | 説明 |
|----------|------|
| `jkk_akiya_diff.py` | メインスクリプト |
| `jkk_previous_state.json` | 前回の物件一覧（自動生成） |
| `jkk_debug.html` | データが0件だった場合に、取得したHTMLを保存（デバッグ用） |

## 注意

- 対象URLが「おわび」ページの場合は物件データが取れず、0件として `jkk_debug.html` が保存されます。通常運用に戻った後に再度実行してください。
- ページのHTML構造（表の列名・レイアウト）が変わった場合は、スクリプト内の `parse_items` のセレクタを実ページに合わせて調整してください。

---

## LINE通知スクリプト（`jkk_line_notify.py`）

空き家（募集戸数）の増加・新規・間取り別の入れ替わりを検知し、LINE Messaging API で通知します。

- 一覧表は **`table.cell666666`** の **住宅名 / 間取り / 募集戸数** 列を解釈します。
- 同一 **住宅名** の複数行は **募集戸数を合算** して `last_data.json`（物件名→件数）に保存します。
- 入れ替わり検知は、行ごとに **間取り + 募集戸数 + senPage 引数** のシグネチャでハッシュしています。

### 保存先（`last_data.json` / `last_rooms.json`）

- **既定**: `jkk_line_notify.py` と**同じフォルダ**に保存されます（実行時のカレントディレクトリは関係しません）。
- **別フォルダにしたい場合**: 環境変数 `JKK_DATA_DIR` にディレクトリパスを指定（Colab の Google ドライブ等）。

```bash
export JKK_DATA_DIR="/content/drive/MyDrive/jkk_watch"
```

### 監視する一覧URL（`JKK_TARGET_URL`）

- **既定**: [akiyaJyokenDirect](https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyokenDirect)（空き家条件の検索結果ページ）
- **変更数だけ追う**場合などは `AKIYAchangeCount` に切り替え可能です。

```bash
export JKK_TARGET_URL="https://jhomes.to-kousya.or.jp/search/jkknet/service/AKIYAchangeCount"
```

### 取得の流れ（ウォームアップ）

- `https://jhomes.to-kousya.or.jp/` は **404** のため使いません。
- 既定では `www.to-kousya.or.jp` → 賃貸案内 → `akiyaJyoukenStartInit` のあと **`JKK_TARGET_URL`**（既定は `akiyaJyokenDirect`）を取得します。
- ウォームアップURLを変えたい場合: `JKK_JHOMES_WARMUP_URL`（空にすると jhomes ウォームアップをスキップ）。

### 詳細URL（senPage → `view?danchi=&room=`）

詳細ボタンは `href` が空で `onclick="senPage('','L8851','1280950','0000')"` の形式です。  
**未設定時は** 次の既定で組み立てます（団地コード `p3`、行コード `p2` を想定）。

- 既定ベース: `https://jhomes.to-kousya.or.jp/search/jkknet/view`
- 既定クエリ: `danchi={p3}&room={p2}`

実ブラウザのURLとパスが違う場合は、ベースだけ差し替えられます。

```bash
# 全体を上書き（{p1}〜{p4} は senPage の第1〜第4引数）
export JKK_DETAIL_URL_TEMPLATE='https://example.jp/path/view?danchi={p3}&room={p2}&sub={p4}'

# またはベース + クエリだけ変更
export JKK_DETAIL_VIEW_BASE='https://jhomes.to-kousya.or.jp/実際のパス/view'
export JKK_DETAIL_QUERY_TEMPLATE='danchi={p3}&room={p2}&line={p4}'
```

`{p1}` … 第1引数（空のことが多い） / `{p2}` … 例: `L8851` / `{p3}` … 例: `1280950` / `{p4}` … 例: `0000`
