#!/usr/bin/env python3
"""Amazon 価格比較スクレイパー / HTML 生成スクリプト.

config.json の各商品（自社・競合）の Amazon 商品ページから
バリエーション（容量違い等）を自動検出し、各バリエーションの価格を取得する。
config.json の "pairs" で「自社ASIN ↔ 競合ASIN」のペアを定義すると、
そのペア同士で価格を直接比較する。
取得結果は data/history.json に履歴として追記し、docs/index.html を生成する。

使い方:
    python scraper.py             # 取得 + HTML 生成
    python scraper.py --html-only # 既存履歴から HTML だけ再生成

環境変数（任意・本番でのブロック回避用）:
    SCRAPERAPI_KEY   ScraperAPI のキー。設定すると api.scraperapi.com 経由で取得。
    SCRAPERAPI_OPTS  ScraperAPI の追加パラメータ（例: "ultra_premium=true"）。
    SCRAPER_PROXY    例: socks5h://user:pass@host:1080 / http://user:pass@host:port
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
        "Upgrade-Insecure-Requests": "1",
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


def parse_grams(text: str | None) -> int | None:
    """'300g' や '1kg' や '50g×6' から総グラム数を推定する。"""
    if not text:
        return None
    s = str(text).lower().replace(",", "").replace("×", "x").replace("✕", "x").replace("＊", "*")
    # "50g x 6" のような掛け算表記
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g)\s*[x\*]\s*(\d+)", s)
    if m:
        base = float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)
        return int(round(base * int(m.group(3))))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g)", s)
    if m:
        return int(round(float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)))
    return None


def asin_from_url(url: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


def dp_url(asin: str, site: str) -> str:
    return f"https://www.amazon.{site}/dp/{asin}"


def apply_fetch_layer(url: str) -> tuple[str, dict | None]:
    api_key = os.environ.get("SCRAPERAPI_KEY")
    if api_key:
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
    session = requests.Session()
    last_err = None
    target, proxies = apply_fetch_layer(url)
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(target, headers=build_headers(), timeout=60, proxies=proxies)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
            elif "api-services-support@amazon.com" in resp.text or "validateCaptcha" in resp.text:
                last_err = "blocked (captcha)"
            else:
                return resp.text, None
        except requests.RequestException as exc:  # noqa: BLE001
            last_err = str(exc)
        if attempt < retries:
            wait = 2 ** attempt + random.uniform(0, 2)
            print(f"      retry {attempt}/{retries} after {wait:.1f}s ({last_err})")
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


def extract_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one("#productTitle")
    return el.get_text(strip=True) if el else None


MAX_VARIATIONS = 15


def detect_variations(html: str, main_asin: str | None) -> dict[str, str]:
    """商品ページHTMLからバリエーションを検出し {asin: ラベル} を返す。

    1) 埋め込み JSON "dimensionValuesDisplayData"（ASIN→表示ラベル配列）を最優先。
    2) 無ければ twister（バリエーション選択 UI）コンテナ内の data-asin のみを拾う。
       ※ ページ全体の data-asin（関連商品・広告等）は拾わない。
    3) それも無ければ単一商品として main_asin のみ。
    """
    result: dict[str, str] = {}
    # 1. dimensionValuesDisplayData は {"ASIN":["300g"],"ASIN2":["600g",...]} 形式
    for m in re.finditer(r'"dimensionValuesDisplayData"\s*:\s*(\{[^{}]+\})', html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for asin, labels in data.items():
            if re.fullmatch(r"[A-Z0-9]{10}", asin):
                label = " / ".join(str(x) for x in labels) if isinstance(labels, list) else str(labels)
                result[asin] = label.strip()

    # 2. twister コンテナ内の data-asin のみ（ページ全体は走査しない）
    if not result:
        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(
            "#twister, #twisterContainer, #inline-twister-row, "
            "[id*='inline-twister'], #variation_size_name, #variation_style_name, "
            "form#twister-plus-inline-twister, #tp-inline-twister-dim-values-container"
        )
        for c in containers:
            for el in c.select("[data-asin], [data-defaultasin], [asin]"):
                a = el.get("data-asin") or el.get("data-defaultasin") or el.get("asin")
                if a and re.fullmatch(r"[A-Z0-9]{10}", a):
                    label = (el.get("title") or el.get("aria-label")
                             or el.get_text(" ", strip=True) or "")[:40]
                    result.setdefault(a, label.strip())

    # 3. 単一商品 or 検出失敗 → main_asin のみ
    if main_asin and main_asin not in result:
        result[main_asin] = ""
    # 安全弁: 拾いすぎた場合は main_asin を含めて上限まで
    if len(result) > MAX_VARIATIONS:
        kept = {}
        if main_asin and main_asin in result:
            kept[main_asin] = result[main_asin]
        for a, lbl in result.items():
            if len(kept) >= MAX_VARIATIONS:
                break
            kept[a] = lbl
        result = kept
    return result


def load_json(path: Path, default):
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def last_known_prices(history: list) -> dict[str, dict]:
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
    fallback = last_known_prices(history)

    products_out: dict[str, dict] = {}
    for product in config["products"]:
        role = product["role"]
        url = product["url"]
        main_asin = asin_from_url(url)
        print(f"取得中: [{role}] {product['name']}  (main={main_asin})")

        main_html, err = fetch_html(url)
        if main_html is None:
            print(f"  メインページ取得失敗 ({err})")
        title = extract_title(main_html) if main_html else None
        var_map = detect_variations(main_html or "", main_asin)
        print(f"  検出バリエーション数: {len(var_map)} -> {list(var_map.keys())}")

        variations_out: dict[str, dict] = {}
        for asin, label in var_map.items():
            grams = parse_grams(label)
            if grams is None and asin == main_asin:
                grams = parse_grams(title)  # 単一商品はタイトルから推定
            # 価格取得（main はダウンロード済みHTMLを再利用）
            if asin == main_asin and main_html:
                price = extract_price(main_html)
                verr = None if price else "price not found"
            else:
                vhtml, verr = fetch_html(dp_url(asin, site))
                price = extract_price(vhtml) if vhtml else None
                if grams is None and vhtml:
                    grams = parse_grams(extract_title(vhtml))
            disp_label = label or (f"{grams}g" if grams else asin)
            if price is None:
                prev = fallback.get(asin)
                if prev:
                    print(f"    {disp_label} ({asin}) -> 失敗({verr}), 前回値 ￥{prev['price']:,}")
                    variations_out[asin] = {**prev, "label": disp_label, "stale": True}
                else:
                    print(f"    {disp_label} ({asin}) -> 失敗({verr}), 前回値なし")
                    variations_out[asin] = {
                        "label": disp_label, "grams": grams, "price": None, "stale": False,
                    }
            else:
                print(f"    {disp_label} ({asin}) -> ￥{price:,}"
                      + (f"  ({grams}g)" if grams else ""))
                variations_out[asin] = {
                    "label": disp_label, "grams": grams, "price": price, "stale": False,
                }

        products_out[role] = {
            "name": product["name"], "url": url, "variations": variations_out,
        }

    record = {"date": today, "products": products_out}
    history = [r for r in history if r.get("date") != today]
    history.append(record)
    history.sort(key=lambda r: r["date"])
    return history


def fmt_yen(v) -> str:
    if v is None:
        return "—"
    return f"￥{int(v):,}"


def diff_html(self_p, comp_p) -> str:
    if self_p is None or comp_p is None:
        return "—"
    d = self_p - comp_p
    if d > 0:
        return f"<span class='up'>自社が ￥{d:,} 高い</span>"
    if d < 0:
        return f"<span class='down'>自社が ￥{abs(d):,} 安い</span>"
    return "<span class='flat'>同額</span>"


def find_var(products: dict, role: str, asin: str) -> dict:
    return products.get(role, {}).get("variations", {}).get(asin, {})


def render_html(config: dict, history: list) -> str:
    title = config.get("title", "Amazon 価格比較")
    pairs = config.get("pairs", [])
    roles = [p["role"] for p in config["products"]]
    self_role = roles[0] if roles else "自社"
    comp_role = roles[1] if len(roles) > 1 else "競合"
    updated = history[-1]["date"] if history else "未取得"
    latest = history[-1]["products"] if history else {}

    # ペア比較表
    pair_rows = []
    for pair in pairs:
        sv = find_var(latest, self_role, pair.get("self", ""))
        cv = find_var(latest, comp_role, pair.get("competitor", ""))
        sp, cp = sv.get("price"), cv.get("price")
        s_stale = " <span class='stale'>(前回値)</span>" if sv.get("stale") else ""
        c_stale = " <span class='stale'>(前回値)</span>" if cv.get("stale") else ""
        pair_rows.append(f"""<tr>
        <td class="pairlabel">{pair.get('label','')}</td>
        <td>{sv.get('label', pair.get('self',''))}<br><span class="asin">{pair.get('self','')}</span></td>
        <td class="price">{fmt_yen(sp)}{s_stale}</td>
        <td>{cv.get('label', pair.get('competitor',''))}<br><span class="asin">{pair.get('competitor','')}</span></td>
        <td class="price">{fmt_yen(cp)}{c_stale}</td>
        <td class="diff">{diff_html(sp, cp)}</td>
      </tr>""")
    pair_table = (f"""<table>
      <thead><tr><th>ペア</th><th>{self_role} バリ</th><th>{self_role} 価格</th>
      <th>{comp_role} バリ</th><th>{comp_role} 価格</th><th>判定</th></tr></thead>
      <tbody>{''.join(pair_rows)}</tbody>
    </table>""" if pair_rows else "<p>ペア未設定です。config.json の \"pairs\" に ASIN を設定してください。</p>")

    # 全バリエーション一覧（ペア設定の材料）
    var_blocks = []
    for i, role in enumerate(roles):
        cur = latest.get(role, {})
        vrows = []
        for asin, v in cur.get("variations", {}).items():
            vrows.append(f"""<tr>
          <td>{v.get('label') or '—'}</td>
          <td class="asin">{asin}</td>
          <td>{(str(v.get('grams'))+'g') if v.get('grams') else '—'}</td>
          <td class="price">{fmt_yen(v.get('price'))}</td>
        </tr>""")
        color = "#2563eb" if i == 0 else "#dc2626"
        var_blocks.append(f"""<div class="vargroup">
        <h3><span class="badge" style="background:{color}">{role}</span> {cur.get('name','')}</h3>
        <table class="vartable">
          <thead><tr><th>バリエーション</th><th>ASIN</th><th>容量</th><th>価格</th></tr></thead>
          <tbody>{''.join(vrows) if vrows else '<tr><td colspan=4>データ未取得</td></tr>'}</tbody>
        </table>
      </div>""")

    # チャート: ペア別 価格差（自社 - 競合）の推移
    labels = [r["date"] for r in history]
    colors = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2"]
    datasets = []
    for idx, pair in enumerate(pairs):
        series = []
        for r in history:
            sv = find_var(r["products"], self_role, pair.get("self", ""))
            cv = find_var(r["products"], comp_role, pair.get("competitor", ""))
            sp, cp = sv.get("price"), cv.get("price")
            series.append(sp - cp if (sp is not None and cp is not None) else None)
        datasets.append({"label": pair.get("label", f"ペア{idx+1}"),
                         "color": colors[idx % len(colors)], "data": series})
    chart_data = json.dumps({"labels": labels, "datasets": datasets}, ensure_ascii=False)

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
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  h2 {{ font-size: 1.05rem; margin: 0 0 12px; color: #374151; }}
  h3 {{ font-size: .95rem; margin: 0 0 8px; }}
  .updated {{ color: #6b7280; font-size: .85rem; margin-bottom: 20px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 10px 8px; text-align: left; border-bottom: 1px solid #eef0f3; font-size: .9rem; }}
  th {{ font-size: .78rem; color: #6b7280; font-weight: 600; }}
  td.price {{ font-weight: 700; white-space: nowrap; }}
  td.pairlabel {{ font-weight: 600; }}
  td.diff {{ white-space: nowrap; }}
  .asin {{ color: #9ca3af; font-size: .75rem; }}
  .badge {{ color: #fff; padding: 2px 10px; border-radius: 999px; font-size: .8rem;
           font-weight: 600; white-space: nowrap; }}
  .up {{ color: #dc2626; }} .down {{ color: #059669; }} .flat {{ color: #6b7280; }}
  .stale {{ color: #d97706; font-size: .72rem; }}
  .vargroup {{ margin-bottom: 18px; }}
  footer {{ color: #9ca3af; font-size: .75rem; text-align: center; margin-top: 24px; line-height: 1.6; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <p class="updated">最終更新: {updated}（1日1回自動更新）</p>

  <div class="card">
    <h2>ペア比較（バリエーション同士）</h2>
    {pair_table}
  </div>

  <div class="card">
    <h2>ペア別 価格差の推移（自社 − 競合）</h2>
    <canvas id="chart" height="240"></canvas>
  </div>

  <div class="card">
    <h2>全バリエーション一覧</h2>
    <p class="updated">※ ここのASINを config.json の "pairs" に設定すると、上のペア比較に反映されます。</p>
    {''.join(var_blocks)}
  </div>

  <footer>
    価格・バリエーションは Amazon (amazon.co.jp) から自動取得した参考値です。<br>
    実際の販売価格は各商品ページをご確認ください。
  </footer>
</div>

<script>
const C = {chart_data};
new Chart(document.getElementById('chart'), {{
  type: 'line',
  data: {{
    labels: C.labels,
    datasets: C.datasets.map(d => ({{
      label: d.label, data: d.data,
      borderColor: d.color, backgroundColor: d.color + '22',
      spanGaps: true, tension: 0.2, pointRadius: 3,
    }})),
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ position: 'bottom' }},
      tooltip: {{ callbacks: {{ label: c => c.dataset.label + ': ' +
        (c.parsed.y > 0 ? '自社+￥' : '自社￥') + c.parsed.y.toLocaleString() }} }}
    }},
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
