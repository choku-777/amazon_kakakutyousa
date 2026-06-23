#!/usr/bin/env python3
"""Amazon 価格比較スクレイパー / HTML 生成スクリプト（ペア指定方式）.

config.json の "pairs" に「自社1商品 + 競合1〜複数商品」を ASIN で明示指定する。
各 ASIN の価格を Amazon から取得し、ペアごとに自社 vs 競合を直接比較する。
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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
    """'1kg' や '50g×20' から総グラム数を推定する。"""
    if not text:
        return None
    s = str(text).lower().replace(",", "").replace("×", "x").replace("✕", "x").replace("＊", "*")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g)\s*[x\*]\s*(\d+)", s)
    if m:
        base = float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)
        return int(round(base * int(m.group(3))))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g)", s)
    if m:
        return int(round(float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)))
    return None


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


def fetch_price(asin: str, site: str, retries: int = 3, timeout: int = 30) -> tuple[int | None, str | None]:
    """ASIN の商品ページを取得し価格を返す。失敗時は (None, error)。"""
    url = dp_url(asin, site)
    target, proxies = apply_fetch_layer(url)
    session = requests.Session()
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(target, headers=build_headers(), timeout=timeout, proxies=proxies)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
            elif "api-services-support@amazon.com" in resp.text or "validateCaptcha" in resp.text:
                last_err = "blocked (captcha)"
            else:
                soup = BeautifulSoup(resp.text, "lxml")
                for sel in PRICE_SELECTORS:
                    node = soup.select_one(sel)
                    price = parse_price(node.get_text()) if node else None
                    if price:
                        return price, None
                last_err = "price not found"
        except requests.RequestException as exc:  # noqa: BLE001
            last_err = str(exc)
        if attempt < retries:
            wait = 2 ** attempt + random.uniform(0, 1)
            print(f"      retry {attempt}/{retries} after {wait:.1f}s ({last_err})", flush=True)
            time.sleep(wait)
    return None, last_err


def load_json(path: Path, default):
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def last_known_prices(history: list) -> dict[str, int]:
    known: dict[str, int] = {}
    for rec in history:
        for pair in rec.get("pairs", {}).values():
            items = [pair.get("self", {})] + list(pair.get("competitors", {}).values())
            for it in items:
                if it.get("price") is not None:
                    known[it.get("asin")] = it["price"]
    return known


def fetch_item(item: dict, site: str, fallback: dict[str, int]) -> dict:
    """1商品(self or competitor)を取得して結果dictを返す。"""
    asin = item["asin"]
    name = item.get("name", asin)
    grams = parse_grams(name)
    price, err = fetch_price(asin, site)
    if price is None:
        prev = fallback.get(asin)
        if prev is not None:
            print(f"    {name} ({asin}) -> 失敗({err}), 前回値 ￥{prev:,}", flush=True)
            return {"name": name, "asin": asin, "grams": grams, "price": prev, "stale": True}
        print(f"    {name} ({asin}) -> 失敗({err}), 前回値なし", flush=True)
        return {"name": name, "asin": asin, "grams": grams, "price": None, "stale": False}
    print(f"    {name} ({asin}) -> ￥{price:,}" + (f"  ({grams}g)" if grams else ""), flush=True)
    return {"name": name, "asin": asin, "grams": grams, "price": price, "stale": False}


def scrape(config: dict, history: list) -> list:
    today = dt.date.today().isoformat()
    site = config.get("site", "co.jp")
    fallback = last_known_prices(history)

    pairs_out: dict[str, dict] = {}
    for pair in config.get("pairs", []):
        label = pair["label"]
        print(f"ペア: {label}", flush=True)
        print("  [自社]", flush=True)
        self_out = fetch_item(pair["self"], site, fallback)
        comps_out: dict[str, dict] = {}
        print("  [競合]", flush=True)
        for comp in pair.get("competitors", []):
            comps_out[comp["asin"]] = fetch_item(comp, site, fallback)
        pairs_out[label] = {"self": self_out, "competitors": comps_out}

    record = {"date": today, "pairs": pairs_out}
    history = [r for r in history if r.get("date") != today]
    history.append(record)
    history.sort(key=lambda r: r["date"])
    return history


def notify_discord(history: list) -> None:
    """毎回、全ペアの比較内容を Discord Webhook に通知する（変動有無を問わず）。

    前日比で変動があった商品にはその差分も併記する。
    """
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url or not history:
        return
    today = history[-1]
    prev = history[-2] if len(history) >= 2 else {}

    # 取得失敗（前回値 or 価格なし）の件数を集計して警告に使う
    total = failed = 0
    for pair in today.get("pairs", {}).values():
        for it in [pair.get("self", {})] + list(pair.get("competitors", {}).values()):
            total += 1
            if it.get("stale") or it.get("price") is None:
                failed += 1

    def delta(cur, old) -> str:
        if cur is None or old is None or cur == old:
            return ""
        d = cur - old
        return f"（前日比 {'🔺+' if d > 0 else '🔻-'}￥{abs(d):,}）"

    def compare(self_p, comp_p) -> str:
        if self_p is None or comp_p is None:
            return ""
        d = self_p - comp_p
        if d > 0:
            return f" → 自社が ￥{d:,} 高い"
        if d < 0:
            return f" → 自社が ￥{abs(d):,} 安い"
        return " → 同額"

    blocks = []
    for label, pair in today.get("pairs", {}).items():
        pprev = prev.get("pairs", {}).get(label, {})
        s = pair.get("self", {})
        sp = s.get("price")
        s_mark = " ⚠前回値" if s.get("stale") else ""
        lines = [f"自社: {fmt_yen(sp)}{s_mark} {delta(sp, pprev.get('self', {}).get('price'))}".rstrip()]
        for asin, comp in pair.get("competitors", {}).items():
            cp = comp.get("price")
            oc = pprev.get("competitors", {}).get(asin, {}).get("price")
            c_mark = " ⚠前回値" if comp.get("stale") else ""
            lines.append(
                f"・{comp.get('name', asin)}: {fmt_yen(cp)}{c_mark} "
                f"{delta(cp, oc)}{compare(sp, cp)}".replace("  ", " ").rstrip()
            )
        blocks.append(f"**【{label}】**\n" + "\n".join(lines))

    header = f"📊 **Amazon価格レポート**（{today['date']}）\n"
    if failed:
        header = (f"⚠️ **取得失敗 {failed}/{total}件**（前回値を表示中）\n"
                  f"→ ScraperAPIのクレジット残量・APIキーをご確認ください\n\n"
                  + header)
    content = (header + "\n" + "\n\n".join(blocks)
               + "\n\nhttps://choku-777.github.io/amazon_kakakutyousa/")
    try:
        r = requests.post(url, json={"content": content[:1900]}, timeout=20)
        print(f"Discord通知: HTTP {r.status_code}", flush=True)
    except requests.RequestException as exc:  # noqa: BLE001
        print(f"Discord通知失敗: {exc}", flush=True)


def fmt_yen(v) -> str:
    return "—" if v is None else f"￥{int(v):,}"


def diff_html(self_p, comp_p) -> str:
    if self_p is None or comp_p is None:
        return "—"
    d = self_p - comp_p
    if d > 0:
        return f"<span class='up'>自社が ￥{d:,} 高い</span>"
    if d < 0:
        return f"<span class='down'>自社が ￥{abs(d):,} 安い</span>"
    return "<span class='flat'>同額</span>"


def render_html(config: dict, history: list) -> str:
    title = config.get("title", "Amazon 価格比較")
    updated = history[-1]["date"] if history else "未取得"
    latest = history[-1]["pairs"] if history else {}

    labels = [r["date"] for r in history]
    self_color = "#2563eb"
    comp_colors = ["#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2", "#db2777"]

    # ペアごとの比較ブロック（比較表 + そのペアの価格推移グラフ）
    pair_blocks = []
    charts = []  # 各ペアの価格推移チャート定義
    for i, pair_cfg in enumerate(config.get("pairs", [])):
        label = pair_cfg["label"]
        pdata = latest.get(label, {})
        s = pdata.get("self", {})
        s_stale = " <span class='stale'>(前回値)</span>" if s.get("stale") else ""
        s_grams = f"（{s['grams']}g）" if s.get("grams") else ""

        comp_rows = []
        for comp_cfg in pair_cfg.get("competitors", []):
            casin = comp_cfg["asin"]
            comp = pdata.get("competitors", {}).get(casin, {})
            c_stale = " <span class='stale'>(前回値)</span>" if comp.get("stale") else ""
            c_grams = f"（{comp['grams']}g）" if comp.get("grams") else ""
            comp_rows.append(f"""<tr>
          <td data-label="競合">{comp.get('name', comp_cfg.get('name',''))}{c_grams}<br><span class="asin">{casin}</span></td>
          <td class="price" data-label="価格">{fmt_yen(comp.get('price'))}{c_stale}</td>
          <td class="diff" data-label="比較">{diff_html(s.get('price'), comp.get('price'))}</td>
        </tr>""")

        # このペアの価格推移データ（自社 + 各競合の実額ライン）
        ds = [{
            "label": "自社", "color": self_color,
            "data": [r.get("pairs", {}).get(label, {}).get("self", {}).get("price") for r in history],
        }]
        for j, comp_cfg in enumerate(pair_cfg.get("competitors", [])):
            casin = comp_cfg["asin"]
            ds.append({
                "label": comp_cfg.get("name", casin),
                "color": comp_colors[j % len(comp_colors)],
                "data": [r.get("pairs", {}).get(label, {}).get("competitors", {}).get(casin, {}).get("price")
                         for r in history],
            })
        # 縦軸を50円刻みに固定（全グラフで目盛り間隔を統一）
        vals = [v for d in ds for v in d["data"] if v is not None]
        if vals:
            ymin = (min(vals) // 50) * 50 - 50
            ymax = ((max(vals) + 49) // 50) * 50 + 50
        else:
            ymin = ymax = None
        charts.append({"id": f"chart-{i}", "datasets": ds, "ymin": ymin, "ymax": ymax})

        pair_blocks.append(f"""<div class="card">
        <h2>ペア: {label}</h2>
        <div class="selfbox">
          <span class="badge" style="background:{self_color}">自社</span>
          {s.get('name','')}{s_grams}
          <span class="asin">{s.get('asin','')}</span>
          <span class="bigprice">{fmt_yen(s.get('price'))}{s_stale}</span>
        </div>
        <table>
          <thead><tr><th>競合</th><th>価格</th><th>自社との比較</th></tr></thead>
          <tbody>{''.join(comp_rows) if comp_rows else '<tr><td colspan=3>競合未設定</td></tr>'}</tbody>
        </table>
        <div class="charttitle">価格推移</div>
        <canvas id="chart-{i}" height="220"></canvas>
      </div>""")

    chart_data = json.dumps({"labels": labels, "charts": charts}, ensure_ascii=False)

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
  .wrap {{ max-width: 920px; margin: 0 auto; padding: 24px 16px 64px; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  h2 {{ font-size: 1.1rem; margin: 0 0 14px; color: #374151; }}
  .updated {{ color: #6b7280; font-size: .85rem; margin-bottom: 20px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .selfbox {{ background: #eff6ff; border-radius: 8px; padding: 12px 14px; margin-bottom: 14px;
             display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .bigprice {{ margin-left: auto; font-size: 1.3rem; font-weight: 800; white-space: nowrap; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 10px 8px; text-align: left; border-bottom: 1px solid #eef0f3; font-size: .92rem; }}
  th {{ font-size: .78rem; color: #6b7280; font-weight: 600; }}
  td.price {{ font-weight: 700; white-space: nowrap; }}
  td.diff {{ white-space: nowrap; }}
  .asin {{ color: #9ca3af; font-size: .75rem; }}
  .badge {{ color: #fff; padding: 2px 10px; border-radius: 999px; font-size: .8rem; font-weight: 600; }}
  .up {{ color: #dc2626; }} .down {{ color: #059669; }} .flat {{ color: #6b7280; }}
  .stale {{ color: #d97706; font-size: .72rem; }}
  .charttitle {{ font-size: .85rem; color: #6b7280; font-weight: 600; margin: 16px 0 6px; }}
  td.name, .name, .selfbox {{ overflow-wrap: anywhere; }}
  footer {{ color: #9ca3af; font-size: .75rem; text-align: center; margin-top: 24px; line-height: 1.6; }}

  /* スマホ（狭幅）対応 */
  @media (max-width: 600px) {{
    .wrap {{ padding: 16px 10px 48px; }}
    h1 {{ font-size: 1.2rem; }}
    h2 {{ font-size: 1rem; }}
    .card {{ padding: 14px; }}
    .selfbox {{ flex-direction: column; align-items: flex-start; gap: 4px; }}
    .bigprice {{ margin-left: 0; font-size: 1.25rem; }}
    table thead {{ display: none; }}
    table, tbody, tr, td {{ display: block; width: 100%; }}
    tr {{ border: 1px solid #eef0f3; border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; }}
    td {{ border: none; padding: 4px 0; display: flex; justify-content: space-between;
         align-items: baseline; gap: 12px; }}
    td::before {{ content: attr(data-label); color: #6b7280; font-size: .72rem;
                 font-weight: 600; flex: 0 0 auto; }}
    td.price, td.diff {{ white-space: normal; text-align: right; }}
    td[data-label="競合"] {{ flex-direction: column; align-items: flex-start; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <p class="updated">最終更新: {updated}（1日1回自動更新）</p>

  {''.join(pair_blocks) if pair_blocks else '<div class="card"><p>データ未取得</p></div>'}

  <footer>
    価格は Amazon (amazon.co.jp) から自動取得した参考値です。<br>
    実際の販売価格は各商品ページをご確認ください。
  </footer>
</div>

<script>
const C = {chart_data};
C.charts.forEach(ch => {{
  new Chart(document.getElementById(ch.id), {{
    type: 'line',
    data: {{
      labels: C.labels,
      datasets: ch.datasets.map(d => ({{
        label: d.label, data: d.data,
        borderColor: d.color, backgroundColor: d.color + '22',
        spanGaps: true, tension: 0.2, pointRadius: 3,
      }})),
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ position: 'bottom' }} }},
      scales: {{ y: {{
        min: ch.ymin, max: ch.ymax,
        ticks: {{ stepSize: 50, callback: v => '￥' + v.toLocaleString() }}
      }} }}
    }}
  }});
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
        notify_discord(history)

    html = render_html(config, history)
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"HTML 生成完了: {HTML_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
