# Amazon 価格比較（馬刺し：自社 vs 競合）

Amazon (amazon.co.jp) の自社商品と競合商品の価格を **1日1回**自動取得し、
**100g あたり単価（¥/100g）** に換算して同一容量ベースで比較。結果を
GitHub Pages で HTML 公開します。バリエーション（容量違い）にも対応しています。

公開 URL（Pages 有効化後）:
**https://choku-777.github.io/amazon_kakakutyousa/**

## 仕組み

```
config.json ──▶ scraper.py ──▶ data/history.json（価格履歴）
                    │
                    └────────▶ docs/index.html（公開ページ・グラフ付き）
```

- `.github/workflows/scrape.yml` が毎日 09:00(JST) に `scraper.py` を実行
- 取得した価格を履歴に追記し、HTML を再生成してコミット
- GitHub Pages が `docs/` を配信

## セットアップ手順（最初に1回）

1. **GitHub Pages を有効化**
   リポジトリの `Settings` → `Pages` →
   `Build and deployment` の `Source` を **GitHub Actions** に設定。

2. **ワークフローの権限**
   `Settings` → `Actions` → `General` → `Workflow permissions` を
   **Read and write permissions** に設定。

3. **初回実行**
   `Actions` タブ → 「価格スクレイピング & 公開」→ `Run workflow` で手動実行。

## 比較する商品の変更

`config.json` を編集します。

```json
{
  "unit_grams": 100,
  "products": [
    {
      "role": "自社",
      "name": "商品名",
      "url": "https://www.amazon.co.jp/dp/ASIN",
      "variations": [
        { "asin": "ASIN", "label": "300g（50g×6P）", "grams": 300 }
      ]
    }
  ]
}
```

- `grams` … そのバリエーションの内容量（g）。これを基に100g単価を算出します。
- バリエーション（容量違い）が複数ある場合は `variations` に複数追加してください。
  各バリエーションは Amazon 上で **別 ASIN** を持つため、その ASIN と容量を記載します。
- 比較単位は `unit_grams`（既定 100g）で変更可能です。

## ⚠️ Amazon のアクセスブロックについて（重要）

Amazon は **データセンター IP（GitHub Actions のサーバー）からのアクセスを
高確率でブロック** します（CAPTCHA / HTTP 500）。素の scraping は本番では
失敗しがちです。対策として `scraper.py` は以下の経由取得に対応しています。

| 方法 | 設定（リポジトリ Secrets） | 備考 |
|------|---------------------------|------|
| ScraperAPI 等の scraping API | `SCRAPERAPI_KEY` | 推奨。安定。無料枠あり |
| HTTP プロキシ（住宅IP等） | `SCRAPER_PROXY=http://user:pass@host:port` | プロキシ業者契約が必要 |
| 何もしない | — | ブロックされやすい。取得失敗時は前回値を表示 |

Secrets は `Settings` → `Secrets and variables` → `Actions` から登録します。

> 取得に失敗した場合は、直近で取得できた価格（前回値）を表示し続けます。

## ローカル実行

```bash
pip install -r requirements.txt
python scraper.py            # 取得 + HTML 生成
python scraper.py --html-only  # HTML だけ再生成
# docs/index.html をブラウザで開いて確認
```
