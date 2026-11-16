#!/usr/bin/env python3
"""
EGX Sector Liquidity Flow — data collector
==========================================
يسحب بيانات كل أسهم البورصة المصرية (EGX) من TradingView Scanner (مجاني وبدون مفتاح)،
ويربط كل سهم بقطاعه، ويحفظ:
  data/latest.json   -> آخر لقطة لكل الأسهم (الداشبورد بيحسب منها كل حاجة)
  data/history.json  -> ملخص يومي لكل قطاع (لرسم دوران السيولة عبر الأيام)

الاستخدام:
  python collector.py               # سحب بيانات حقيقية
  python collector.py --notify      # + إرسال ملخص على تليجرام (لو ضابط المتغيرات)
  python collector.py --demo        # بيانات تجريبية للتجربة بدون إنترنت
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CAIRO = ZoneInfo("Africa/Cairo")
SCAN_URL = "https://scanner.tradingview.com/egypt/scan"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://www.tradingview.com",
    "referer": "https://www.tradingview.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

# column on TradingView -> short key in our JSON
COLUMNS = {
    "name": "t",
    "description": "n",
    "sector": "tvs",
    "close": "c",
    "change": "ch",
    "open": "o",
    "high": "h",
    "low": "l",
    "volume": "v",
    "Value.Traded": "vt",
    "average_volume_10d_calc": "av10",
    "average_volume_30d_calc": "av30",
    "average_volume_60d_calc": "av60",
    "relative_volume_10d_calc": "rv",
    "market_cap_basic": "mc",
    "Perf.W": "pw",
    "Perf.1M": "pm",
    "Perf.3M": "p3",
    "SMA50": "sma50",
    "RSI": "rsi",
}

# fallback: TradingView sector (English) -> Arabic, used when ticker not in sectors_map.csv
TV_SECTOR_AR = {
    "Finance": "الخدمات المالية غير المصرفية",
    "Non-Energy Minerals": "مواد البناء",
    "Process Industries": "الموارد الأساسية",
    "Consumer Non-Durables": "الأغذية والمشروبات والتبغ",
    "Health Technology": "الرعاية الصحية والأدوية",
    "Health Services": "الرعاية الصحية والأدوية",
    "Producer Manufacturing": "خدمات ومنتجات صناعية وسيارات",
    "Industrial Services": "المقاولات والإنشاءات الهندسية",
    "Consumer Durables": "المنسوجات والسلع المعمرة",
    "Consumer Services": "السياحة والترفيه",
    "Communications": "الاتصالات والإعلام وتكنولوجيا المعلومات",
    "Technology Services": "الاتصالات والإعلام وتكنولوجيا المعلومات",
    "Electronic Technology": "الاتصالات والإعلام وتكنولوجيا المعلومات",
    "Energy Minerals": "الطاقة والخدمات المساندة",
    "Utilities": "الطاقة والخدمات المساندة",
    "Transportation": "النقل والشحن",
    "Retail Trade": "الموزعون وتجارة التجزئة",
    "Distribution Services": "الموزعون وتجارة التجزئة",
    "Commercial Services": "خدمات أخرى",
    "Miscellaneous": "أخرى",
}

INDEX_TICKERS = ["EGX:EGX30", "EGX:EGX70EWI", "EGX:EGX100EWI"]


# ----------------------------------------------------------------- fetching
def _post(body: dict) -> dict:
    req = urllib.request.Request(SCAN_URL, data=json.dumps(body).encode(), headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_stocks() -> list[dict]:
    cols = list(COLUMNS)
    body = {
        "filter": [{"left": "type", "operation": "equal", "right": "stock"}],
        "options": {"lang": "en"},
        "markets": ["egypt"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": cols,
        "sort": {"sortBy": "Value.Traded", "sortOrder": "desc"},
        "range": [0, 1000],
    }
    res = _post(body)
    out = []
    for row in res.get("data", []):
        d = dict(zip(cols, row["d"]))
        out.append({COLUMNS[k]: d.get(k) for k in cols})
    return out


def fetch_indices() -> list[dict]:
    try:
        res = _post({"symbols": {"tickers": INDEX_TICKERS, "query": {"types": []}},
                     "columns": ["description", "close", "change"]})
        return [{"t": r["s"].split(":")[1], "n": r["d"][0], "c": r["d"][1], "ch": r["d"][2]}
                for r in res.get("data", [])]
    except Exception as e:  # indices are nice-to-have
        print("! indices:", e, file=sys.stderr)
        return []


# ----------------------------------------------------------------- sectors
def load_sector_map() -> dict[str, str]:
    p = ROOT / "sectors_map.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return {r["ticker"].strip().upper(): r["sector"].strip() for r in csv.DictReader(f) if r.get("ticker")}


def assign_sectors(stocks: list[dict]) -> None:
    smap = load_sector_map()
    for s in stocks:
        t = (s.get("t") or "").upper()
        s["s"] = smap.get(t) or TV_SECTOR_AR.get(s.get("tvs") or "", "أخرى")
        s["mapped"] = t in smap


# ----------------------------------------------------------------- aggregates (for history)
def num(x, d=0.0):
    try:
        x = float(x)
        return d if math.isnan(x) else x
    except (TypeError, ValueError):
        return d


def sector_summary(stocks: list[dict]) -> dict:
    total = sum(num(s.get("vt")) for s in stocks) or 1.0
    agg: dict[str, dict] = {}
    for s in stocks:
        a = agg.setdefault(s["s"], {"vt": 0.0, "avg30": 0.0, "mcw": 0.0, "mc": 0.0, "mf": 0.0, "up": 0, "dn": 0})
        vt = num(s.get("vt"))
        a["vt"] += vt
        a["avg30"] += num(s.get("av30")) * num(s.get("c"))
        mc = num(s.get("mc"))
        a["mc"] += mc
        a["mcw"] += mc * num(s.get("ch"))
        h, l, c = num(s.get("h")), num(s.get("l")), num(s.get("c"))
        if h > l:
            a["mf"] += ((c - l) - (h - c)) / (h - l) * vt
        if num(s.get("ch")) > 0:
            a["up"] += 1
        elif num(s.get("ch")) < 0:
            a["dn"] += 1
    out = {}
    for k, a in agg.items():
        out[k] = {
            "vt": round(a["vt"]),
            "share": round(a["vt"] / total * 100, 3),
            "liq": round((a["vt"] / a["avg30"] - 1) * 100, 2) if a["avg30"] else None,
            "chg": round(a["mcw"] / a["mc"], 3) if a["mc"] else 0,
            "mf": round(a["mf"] / a["vt"] * 100, 2) if a["vt"] else 0,
            "up": a["up"], "dn": a["dn"],
        }
    return out


def update_history(stocks: list[dict], day: str, keep: int = 180) -> dict:
    p = DATA / "history.json"
    hist = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"days": {}}
    hist["days"][day] = {"total": round(sum(num(s.get("vt")) for s in stocks)), "sectors": sector_summary(stocks)}
    for d in sorted(hist["days"])[:-keep]:
        del hist["days"][d]
    p.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return hist


# ----------------------------------------------------------------- telegram
def notify(stocks: list[dict]) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("! Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return
    sm = sector_summary(stocks)
    ranked = sorted(sm.items(), key=lambda kv: kv[1]["liq"] if kv[1]["liq"] is not None else -999, reverse=True)
    lines = ["📊 سيولة قطاعات البورصة المصرية", ""]
    lines.append("🟢 سيولة أعلى من المعتاد:")
    lines += [f"• {k}: سيولة {v['liq']:+.0f}% · سعر {v['chg']:+.2f}%" for k, v in ranked[:4] if (v["liq"] or 0) > 0]
    lines.append("")
    lines.append("🔴 سيولة أقل من المعتاد:")
    lines += [f"• {k}: سيولة {v['liq']:+.0f}% · سعر {v['chg']:+.2f}%" for k, v in ranked[-4:] if (v["liq"] or 0) < 0]
    hot = sorted([s for s in stocks if num(s.get("rv")) >= 2 and num(s.get("ch")) > 0],
                 key=lambda s: num(s.get("vt")), reverse=True)[:5]
    if hot:
        lines += ["", "⚡ أسهم عليها سيولة غير عادية:"]
        lines += [f"• {s['t']} ({s['s']}): حجم ×{num(s['rv']):.1f} · {num(s['ch']):+.2f}%" for s in hot]
    body = json.dumps({"chat_id": chat, "text": "\n".join(lines)}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()
    print("✓ Telegram sent")


# ----------------------------------------------------------------- demo data
def demo_stocks(seed: int | None = None) -> list[dict]:
    """Synthetic but realistic-looking data — for testing only."""
    rnd = random.Random(seed)
    smap = load_sector_map()
    # sector "mood" for the day: liquidity multiplier and price drift
    moods = {sec: (rnd.uniform(0.55, 1.8), rnd.uniform(-2.5, 3.5)) for sec in set(smap.values())}
    out = []
    for t, sec in smap.items():
        liq_mult, drift = moods[sec]
        price = round(math.exp(rnd.uniform(0, 5)), 2)
        avg_vol = math.exp(rnd.uniform(11, 16)) / max(price, 0.3)
        rv = max(0.15, rnd.lognormvariate(math.log(liq_mult), 0.35))
        vol = avg_vol * rv
        ch = drift + rnd.gauss(0, 1.6)
        c = price
        o = c / (1 + ch / 100) * (1 + rnd.gauss(0, 0.004))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.012)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.012)))
        out.append({
            "t": t, "n": t, "tvs": "", "c": round(c, 3), "ch": round(ch, 2), "o": round(o, 3),
            "h": round(h, 3), "l": round(l, 3), "v": round(vol), "vt": round(vol * c),
            "av10": round(avg_vol * rnd.uniform(0.85, 1.15)), "av30": round(avg_vol),
            "av60": round(avg_vol * rnd.uniform(0.8, 1.2)), "rv": round(rv, 2),
            "mc": round(price * avg_vol * rnd.uniform(150, 900)),
            "pw": round(drift * 1.5 + rnd.gauss(0, 3), 2), "pm": round(drift * 3 + rnd.gauss(0, 7), 2),
            "p3": round(drift * 5 + rnd.gauss(0, 12), 2), "sma50": round(c * rnd.uniform(0.85, 1.12), 3),
            "rsi": round(min(90, max(10, 50 + drift * 6 + rnd.gauss(0, 10))), 1),
        })
    return out


# ----------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="generate synthetic data (no internet)")
    ap.add_argument("--notify", action="store_true", help="send Telegram summary")
    ap.add_argument("--no-history", action="store_true")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    now = datetime.now(CAIRO)

    if args.demo:
        # build ~30 trading days of demo history, then today's snapshot
        stocks = []
        d = now - timedelta(days=45)
        i = 0
        while d <= now:
            if d.weekday() not in (4, 5):  # EGX: Sun–Thu (Fri=4, Sat=5)
                stocks = demo_stocks(seed=i)
                assign_sectors(stocks)
                if not args.no_history:
                    update_history(stocks, d.strftime("%Y-%m-%d"))
                i += 1
            d += timedelta(days=1)
        indices = [{"t": "EGX30", "n": "EGX 30", "c": 41250.3, "ch": 1.12},
                   {"t": "EGX70EWI", "n": "EGX 70 EWI", "c": 10480.6, "ch": 0.64},
                   {"t": "EGX100EWI", "n": "EGX 100 EWI", "c": 14320.1, "ch": 0.71}]
        source = "DEMO — بيانات تجريبية"
    else:
        try:
            stocks = fetch_stocks()
        except Exception as e:
            sys.exit(f"✗ تعذر سحب البيانات من TradingView: {e}")
        if not stocks:
            sys.exit("✗ TradingView رجّع نتيجة فاضية")
        assign_sectors(stocks)
        indices = fetch_indices()
        source = "TradingView scanner (egypt)"
        if not args.no_history and stocks:
            update_history(stocks, now.strftime("%Y-%m-%d"))

    latest = {
        "generated_at": now.isoformat(timespec="seconds"),
        "source": source,
        "demo": bool(args.demo),
        "indices": indices,
        "stocks": stocks,
    }
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    unmapped = sorted(s["t"] for s in stocks if not s.get("mapped") and num(s.get("vt")) > 0)
    print(f"✓ {len(stocks)} stocks · {now:%Y-%m-%d %H:%M} Cairo · source: {source}")
    if unmapped:
        print(f"ℹ {len(unmapped)} tickers not in sectors_map.csv (used TradingView sector): {', '.join(unmapped[:40])}")
    if args.notify:
        notify(stocks)


if __name__ == "__main__":
    main()
