#!/usr/bin/env python3
"""Amazon 価格比較スクレイパー / HTML 生成スクリプト.

config.json に定義した 2 商品（自社・競合）の Amazon 価格を、
各商品のバリエーション（容量違い = 別 ASIN）ごとに取得し、
「100g あたり単価（¥/100g）」に換算して同一容量ベースで比較する。
取得結果は data/history.json に履歴として追記し、docs/index.html を生成する。

使い方:
    python scraper.py             # スクレイピング + HTML 生成
    python scraper.py --html-only # 既存履歴から HTML だけ再生成（取得しない）

環境変数（任意・本番でのブロック回避用）:
    SCRAPER_PROXY    例: http://user:pass@host:port （全リクエストをこの proxy 経由に）
    SCRAPERAPI_KEY   ScraperAPI のキー。設定すると https://api.scraperapi.com 経由で取得。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_PATH = BASE_DIR / "data" / "history.json"
HTML_PATH = BASE_DIR / "docs" / "index.html"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def build_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div span.a-price span.a-offscreen",
    "#corePrice_feature_div span.a-price span.a-offscreen",
    "#corePrice_desktop span.a-price span.a-offscreen",
    "span.priceToPay span.a-offscreen",
    "span.apexPriceToPay span.a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#priceblock_saleprice",
    "span.a-price span.a-offscreen",
]


def parse_price(text: str) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def request_url(asin: str, site: str) -> str:
    return f"https://www.amazon.{site}/dp/{asin}"


def apply_fetch_layer(url: str) -> tuple[str, dict | None]:
    """proxy / ScraperAPI 設定に応じて、実リクエスト URL と proxies を返す。"""
    api_key = os.environ.get("SCRAPERAPI_KEY")
    if api_key:
        # SCRAPERAPI_OPTS で追加パラメータを付与可能（例: "ultra_premium=true" や
        # "premium=true&render=true"）。未設定なら標準リクエスト（1クレジット）。
        opts = os.environ.get("SCRAPERAPI_OPTS", "").strip()
        extra = f"&{opts}" if opts else ""
        target = (
            "https://api.scraperapi.com/?api_key="
            f"{api_key}&country_code=jp{extra}&url={quote(url, safe='')}"
        )
        return target, None
    proxy = os.environ.get("SCRAPER_PROXY")
    if proxy:
        return url, {"http": proxy, "https": proxy}
    return url, None


def fetch_html(url: str, retries: int = 4) -> tuple[str | None, str | None]:
    """URL を取得して HTML 本文を返す。失敗時は (None, error)。"""
    session = requests.Session()
    last_err = None
    target, proxies = apply_fetch_layer(url)
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(
                target, headers=build_headers(), timeout=40, proxies=proxies
            )
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
            elif "api-services-support@amazon.com" in resp.text or (
                "validateCaptcha" in resp.text
            ):
                last_err = "blocked (captcha)"
            else:
                return resp.text, None
        except requests.RequestException as exc:  # noqa: BLE001
            last_err = str(exc)
        if attempt < retries:
            wait = 2 ** attempt + random.uniform(0, 2)
            print(f"    retry {attempt}/{retries} after {wait:.1f}s ({last_err})")
            time.sleep(wait)
    return None, last_err


def extract_price(html: str) -> int | None:
    soup = BeautifulSoup(html, "lxml")
    for sel in PRICE_SELECTORS:
        node = soup.select_one(sel)
        price = parse_price(node.get_text()) if node else None
        if price:
            return price
    return None


def load_json(path: Path, default):
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def last_known_unit_prices(history: list) -> dict[str, dict]:
    """asin -> 最後に取得できた {price, grams, unit_price}。フォールバック用。"""
    known: dict[str, dict] = {}
    for rec in history:
        for prod in rec.get("products", {}).values():
            for asin, v in prod.get("variations", {}).items():
                if v.get("price") is not None:
                    known[asin] = v
    return known


def scrape(config: dict, history: list) -> list:
    today = dt.date.today().isoformat()
    site = config.get("site", "co.jp")
    unit_grams = config.get("unit_grams", 100)
    fallback = last_known_unit_prices(history)

    products_out: dict[str, dict] = {}
    for product in config["products"]:
        role = product["role"]
        print(f"取得中: [{role}] {product['name']}")
        variations_out: dict[str, dict] = {}
        for var in product["variations"]:
            asin = var["asin"]
            grams = var["grams"]
            url = request_url(asin, site)
            print(f"  - {var['label']} ({asin})")
            html, err = fetch_html(url)
            price = extract_price(html) if html else None
            if price is None:
                err = err or "price not found"
                prev = fallback.get(asin)
                if prev:
                    print(f"    -> 取得失敗 ({err}). 前回値: ￥{prev['price']:,}")
                    variations_out[asin] = {**prev, "stale": True}
                else:
                    print(f"    -> 取得失敗 ({err}). 前回値なし")
                    variations_out[asin] = {
                        "price": None, "grams": grams, "unit_price": None,
                        "label": var["label"], "stale": False,
                    }
                continue
            unit_price = round(price / grams * unit_grams, 1)
            print(f"    -> ￥{price:,}  =  ￥{unit_price:,}/{unit_grams}g")
            variations_out[asin] = {
                "price": price, "grams": grams, "unit_price": unit_price,
                "label": var["label"], "stale": False,
            }

        # 代表 = 100g 単価が最安のバリエーション（最もお得な容量）
        valid = {a: v for a, v in variations_out.items() if v.get("unit_price") is not None}
        best_asin = min(valid, key=lambda a: valid[a]["unit_price"]) if valid else None
        products_out[role] = {
            "name": product["name"],
            "url": product["url"],
            "best_asin": best_asin,
            "best_unit_price": valid[best_asin]["unit_price"] if best_asin else None,
            "variations": variations_out,
        }

    record = {"date": today, "unit_grams": unit_grams, "products": products_out}
    history = [r for r in history if r.get("date") != today]
    history.append(record)
    history.sort(key=lambda r: r["date"])
    return history


def fmt_yen(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and not v.is_integer():
        return f"￥{v:,.1f}"
    return f"￥{int(v):,}"


def diff_html(cur, prev) -> str:
    if cur is None or prev is None:
        return "—"
    d = round(cur - prev, 1)
    if d > 0:
        return f"<span class='up'>▲ +{fmt_yen(d)}</span>"
    if d < 0:
        return f"<span class='down'>▼ -{fmt_yen(abs(d))}</span>"
    return "<span class='flat'>±0</span>"


def render_html(config: dict, history: list) -> str:
    title = config.get("title", "Amazon 価格比較")
    unit_grams = config.get("unit_grams", 100)
    updated = history[-1]["date"] if history else "未取得"
    latest = history[-1]["products"] if history else {}
    prev = history[-2]["products"] if len(history) >= 2 else {}

    colors = ["#2563eb", "#dc2626"]
    roles = [p["role"] for p in config["products"]]
    meta = {p["role"]: p for p in config["products"]}

    # メインの比較テーブル（100g 単価ベース）
    rows = []
    for i, role in enumerate(roles):
        cur = latest.get(role, {})
        pre = prev.get(role, {})
        color = colors[i % len(colors)]
        best_asin = cur.get("best_asin")
        best_var = cur.get("variations", {}).get(best_asin, {}) if best_asin else {}
        unit = cur.get("best_unit_price")
        pre_unit = pre.get("best_unit_price")
        price = best_var.get("price")
        label = best_var.get("label", "—")
        stale = best_var.get("stale")
        unit_disp = fmt_yen(unit) + (" <span class='stale'>(前回値)</span>" if stale else "")
        rows.append(f"""<tr>
        <td><span class="badge" style="background:{color}">{role}</span></td>
        <td class="name"><a href="{meta[role]['url']}" target="_blank" rel="noopener">{meta[role]['name']}</a></td>
        <td>{label}</td>
        <td class="price">{fmt_yen(price)}</td>
        <td class="unit">{unit_disp}</td>
        <td class="diff">{diff_html(unit, pre_unit)}</td>
      </tr>""")

    # 単価差サマリー
    summary = ""
    if len(roles) >= 2:
        a = latest.get(roles[0], {}).get("best_unit_price")
        b = latest.get(roles[1], {}).get("best_unit_price")
        if a is not None and b is not None:
            d = round(a - b, 1)
            if d > 0:
                summary = (f"<p class='summary'>{unit_grams}g 単価で自社は競合より "
                           f"<strong class='up'>{fmt_yen(d)} 高い</strong></p>")
            elif d < 0:
                summary = (f"<p class='summary'>{unit_grams}g 単価で自社は競合より "
                           f"<strong class='down'>{fmt_yen(abs(d))} 安い</strong></p>")
            else:
                summary = "<p class='summary'>自社と競合は<strong>同単価</strong></p>"

    # バリエーション内訳
    var_blocks = []
    for i, role in enumerate(roles):
        cur = latest.get(role, {})
        color = colors[i % len(colors)]
        vrows = []
        for asin, v in cur.get("variations", {}).items():
            best = " ★最安" if asin == cur.get("best_asin") else ""
            vrows.append(f"""<tr>
          <td>{v.get('label','—')}{best}</td>
          <td>{v.get('grams','—')}g</td>
          <td>{fmt_yen(v.get('price'))}</td>
          <td>{fmt_yen(v.get('unit_price'))}</td>
        </tr>""")
        if vrows:
            var_blocks.append(f"""<div class="vargroup">
        <h3><span class="badge" style="background:{color}">{role}</span> {cur.get('name','')}</h3>
        <table class="vartable">
          <thead><tr><th>バリエーション</th><th>容量</th><th>価格</th><th>{unit_grams}g単価</th></tr></thead>
          <tbody>{''.join(vrows)}</tbody>
        </table>
      </div>""")

    # チャート用データ（100g 単価の推移）
    chart = {
        "labels": [r["date"] for r in history],
        "datasets": [
            {
                "role": role,
                "color": colors[i % len(colors)],
                "data": [
                    r["products"].get(role, {}).get("best_unit_price")
                    for r in history
                ],
            }
            for i, role in enumerate(roles)
        ],
    }
    data_json = json.dumps(chart, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", "Hiragino Kaku Gothic ProN",
         "Yu Gothic", Meiryo, sans-serif; margin: 0; background: #f4f6f9; color: #1f2937; }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 24px 16px 64px; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  h2 {{ font-size: 1.05rem; margin: 0 0 12px; color: #374151; }}
  h3 {{ font-size: .95rem; margin: 0 0 8px; }}
  .updated {{ color: #6b7280; font-size: .85rem; margin-bottom: 20px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 10px 8px; text-align: left; border-bottom: 1px solid #eef0f3; font-size: .92rem; }}
  th {{ font-size: .78rem; color: #6b7280; font-weight: 600; }}
  td.unit {{ font-size: 1.15rem; font-weight: 700; white-space: nowrap; }}
  td.price, td.diff {{ white-space: nowrap; }}
  .name a {{ color: #2563eb; text-decoration: none; }}
  .name a:hover {{ text-decoration: underline; }}
  .badge {{ color: #fff; padding: 2px 10px; border-radius: 999px; font-size: .8rem;
           font-weight: 600; white-space: nowrap; }}
  .up {{ color: #dc2626; }} .down {{ color: #059669; }} .flat {{ color: #6b7280; }}
  .stale {{ color: #d97706; font-size: .72rem; }}
  .summary {{ font-size: 1.05rem; margin: 8px 0 0; }}
  .vargroup {{ margin-bottom: 18px; }}
  .vartable th, .vartable td {{ font-size: .85rem; }}
  footer {{ color: #9ca3af; font-size: .75rem; text-align: center; margin-top: 24px; line-height: 1.6; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <p class="updated">最終更新: {updated}（1日1回自動更新） / 比較単位: {unit_grams}g あたり単価</p>

  <div class="card">
    <h2>サマリー（{unit_grams}g 単価で比較）</h2>
    <table>
      <thead><tr><th>区分</th><th>商品</th><th>最安バリエーション</th><th>価格</th><th>{unit_grams}g単価</th><th>前回比</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    {summary}
  </div>

  <div class="card">
    <h2>{unit_grams}g 単価の推移</h2>
    <canvas id="chart" height="240"></canvas>
  </div>

  <div class="card">
    <h2>バリエーション内訳</h2>
    {''.join(var_blocks) if var_blocks else '<p>データ未取得</p>'}
  </div>

  <footer>
    価格・容量は Amazon (amazon.co.jp) から自動取得した参考値です。<br>
    バリエーションにより内容量が異なるため、{unit_grams}g あたりの単価に換算して比較しています。<br>
    実際の販売価格は各商品ページをご確認ください。
  </footer>
</div>

<script>
const C = {data_json};
new Chart(document.getElementById('chart'), {{
  type: 'line',
  data: {{
    labels: C.labels,
    datasets: C.datasets.map(d => ({{
      label: d.role,
      data: d.data,
      borderColor: d.color,
      backgroundColor: d.color + '22',
      spanGaps: true, tension: 0.2, pointRadius: 3,
    }})),
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ position: 'bottom' }} }},
    scales: {{ y: {{ ticks: {{ callback: v => '￥' + v.toLocaleString() }} }} }}
  }}
}});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, None)
    if config is None:
        print("config.json が見つかりません", file=sys.stderr)
        return 1

    history = load_json(DATA_PATH, [])
    if not args.html_only:
        history = scrape(config, history)
        save_json(DATA_PATH, history)

    html = render_html(config, history)
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"HTML 生成完了: {HTML_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
