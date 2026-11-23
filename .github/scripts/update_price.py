#!/usr/bin/env python3
"""
Fetches today's gold prices (karat 18 / 21 / 24, buy + sell) from four Egyptian
market sources and writes the combined result to price.json for the Al-Quds
Jewelry calculator:

  - goldbullioneg.com   (table scrape, same logic as before)
  - gold-era.eg         (JS-rendered page -> fetched with Playwright/Chromium)
  - market.isagha.com   (static table with explicit data-unit="18"/"21"/"24" rows)
  - bulliontradingcenter.com  (static page, "21K Gold" / "24K/ Gold" blocks; no 18K)

IMPORTANT - "buy" / "sell" mean different things on different sites:
  * goldbullioneg.com and gold-era.eg label the price the CUSTOMER pays to buy
    from the shop as "شراء" (the higher number), and the price the customer
    gets when selling to the shop as "بيع" (the lower number).
  * market.isagha.com and bulliontradingcenter.com use the opposite, dealer-
    centric convention: their "شراء"/"تشتري" is what THEY pay to buy from the
    customer (lower), and "بيع"/"تبيع" is what they charge to sell (higher).
  price.json's karat*_buy field has always held the HIGHER ("customer buys /
  shop sells") number (that's what goldbullioneg.com's "شراء" was), so every
  fetcher below normalizes to that meaning before returning its numbers.

Runs inside GitHub Actions every 2 hours. For each karat independently, it
takes the median of whichever sources agree with each other (within
OUTLIER_TOLERANCE) and drops any source that disagrees. It never publishes a
number it is not sure about:
  * each source's own 18/21/24 numbers must agree with each other (ratio checks)
  * buy and sell must be close to each other
  * if, after dropping outliers, a karat has no source left (or the sources
    that are left don't agree with each other), NONE of the three karats are
    updated this run (they are kept in sync with each other)
  * the new 21k price must not jump more than 7% from the last price (if the
    last price is less than 24h old)
If any check fails, price.json is left untouched, a debug snapshot is saved
under debug/, and the workflow run is marked as failed (GitHub emails the
repo owner).
"""

import datetime
import html
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICE_FILE = os.path.join(ROOT, "price.json")
DEBUG_DIR = os.path.join(ROOT, "debug")
CAIRO = ZoneInfo("Africa/Cairo")

KARATS = (24, 21, 18)
PURITY_VS_24 = {21: 21 / 24, 18: 18 / 24}
RATIO_TOLERANCE = 0.02      # 21/24 and 18/24 must be within 2% of the theoretical ratio
MAX_SPREAD = 0.04           # buy and sell (within one source) may differ by at most 4%
MAX_JUMP = 0.07             # 21k may not move more than 7% vs a price younger than 24h
PLAUSIBLE_21 = (2000, 30000)
OUTLIER_TOLERANCE = 0.03    # a source's number must be within 3% of the others to count

MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def http_get(url, headers=None, timeout=40, attempts=3):
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
    }
    hdrs.update(headers or {})
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read().decode("utf-8", "replace"), None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            last_error = f"HTTP {exc.code}: {body[:200]}"
        except Exception as exc:  # network error, timeout...
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(10 * attempt)
    return None, last_error


def to_text(raw):
    s = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|section|ul|ol)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.translate(DIGITS)
    s = s.replace("٬", ",").replace("٫", ".")
    s = re.sub(r"[‎‏‪-‮⁦-⁩ ]", " ", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return s


def num(text):
    """Parse a price string like '6,480.50' / '6480' into a float, or None."""
    if text is None:
        return None
    t = text.translate(DIGITS).replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def source_consistent(buy, sell, b24=None, b21=None, b18=None):
    """Sanity checks shared by every source: buy>=sell (small spread) and, when
    more than one karat is available from the same source, the 18/21/24 ratios."""
    if buy is None or sell is None:
        return False
    hi, lo = max(buy, sell), min(buy, sell)
    if lo <= 0 or hi - lo > hi * MAX_SPREAD:
        return False
    return True


def karats_consistent(values):
    """values: dict karat -> (buy, sell), only for karats a single source provided.
    Checks the 21/24 and 18/24 ratios when 24 is among them."""
    if 24 not in values:
        return True
    b24, s24 = values[24]
    for k in (21, 18):
        if k not in values:
            continue
        bk, sk = values[k]
        for base, val in ((b24, bk), (s24, sk)):
            if base <= 0 or abs(val / base - PURITY_VS_24[k]) > RATIO_TOLERANCE:
                return False
    return True


# --------------------------------------------------------------------------
# source 1: goldbullioneg.com
#   "شراء" = customer buys (higher) -> our buy; "بيع" = customer sells (lower) -> our sell.
#   Same fuzzy table scrape as the original single-source script.
# --------------------------------------------------------------------------

GBE_URL = "https://goldbullioneg.com/"
KARAT_RE = re.compile(r"عيار\s*(24|22|21|18|14|12)(?!\d)|(?<!\d)(24|22|21|18|14|12)\s*عيار")
BOUNDARY_RE = re.compile(r"عيار|قيراط|الجنيه الذهب|جنيه ذهب|الجنيه الدهب|أونصة|اونصة|الأوقية|أوقية|اوقية|الدولار")
NUMBER_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d{4,6})(?:\.\d+)?(?!\d)")
WINDOW = 260


def _numbers_in(segment):
    vals = []
    for m in NUMBER_RE.finditer(segment):
        v = int(m.group(1).replace(",", ""))
        if 1000 <= v <= 100000:
            vals.append(v)
    return vals


def _gbe_collect_candidates(text):
    cands = {k: [] for k in KARATS}
    for m in KARAT_RE.finditer(text):
        karat = int(m.group(1) or m.group(2))
        if karat not in cands:
            continue
        after = text[m.end(): m.end() + WINDOW]
        b = BOUNDARY_RE.search(after)
        if b:
            after = after[: b.start()]
        before = text[max(0, m.start() - WINDOW): m.start()]
        bs = list(BOUNDARY_RE.finditer(before))
        if bs:
            before = before[bs[-1].end():]
        for direction, seg in (("after", after), ("before", before)):
            vals = _numbers_in(seg)
            if len(vals) < 2:
                continue
            a, c = vals[0], vals[1]
            hi, lo = max(a, c), min(a, c)
            if hi - lo > hi * MAX_SPREAD:
                continue
            snippet = text[max(0, m.start() - 60): m.end() + 160].replace("\n", " ")
            cands[karat].append({"buy": hi, "sell": lo, "dir": direction,
                                 "pos": m.start(), "snippet": snippet.strip()})
    for k in cands:
        cands[k].sort(key=lambda c: (c["dir"] != "after", c["pos"]))
    return cands


def _gbe_consistent(c24, c21, c18):
    for field in ("buy", "sell"):
        base = c24[field]
        if abs(c21[field] / base - PURITY_VS_24[21]) > RATIO_TOLERANCE:
            return False
        if abs(c18[field] / base - PURITY_VS_24[18]) > RATIO_TOLERANCE:
            return False
    return PLAUSIBLE_21[0] <= c21["buy"] <= PLAUSIBLE_21[1]


def fetch_goldbullioneg(debug):
    raw, error = http_get(GBE_URL)
    if error:
        debug["error"] = f"could not load {GBE_URL} ({error})"
        return {}
    text = to_text(raw)
    cands = _gbe_collect_candidates(text)
    for c24 in cands[24][:15]:
        for c21 in cands[21][:15]:
            for c18 in cands[18][:15]:
                if _gbe_consistent(c24, c21, c18):
                    debug["chosen"] = {24: c24, 21: c21, 18: c18}
                    return {24: (c24["buy"], c24["sell"]),
                            21: (c21["buy"], c21["sell"]),
                            18: (c18["buy"], c18["sell"])}
    debug["error"] = f"no consistent 18/21/24 set (candidates: {{k: len(v) for k, v in cands.items()}})"
    debug["candidates"] = {str(k): v[:5] for k, v in cands.items()}
    return {}


# --------------------------------------------------------------------------
# source 2: market.isagha.com
#   Dealer-centric: "بيع" (السعر الذي يبيع به التاجر للعميل) is the higher number
#   -> maps to our "buy". "شراء" (السعر الذي يشتري به التاجر من العميل) is the
#   lower number -> maps to our "sell". The table has a stable, explicit markup:
#   <tr data-testid="prices-row" data-unit="18">...
#     <span data-testid="price-figure">BIE</span> ... <span data-testid="price-figure">GAP</span>
#     <span data-testid="price-figure">SHERAA</span> ... <span data-testid="price-figure">GAP</span>
#   (بيع, فجوة بيع, شراء, فجوة شراء, in that order)
# --------------------------------------------------------------------------

ISAGHA_URL = "https://market.isagha.com/prices"
ISAGHA_ROW_RE = re.compile(r'data-unit="(14|18|21|22|24)"(.*?)</tr>', re.S)
ISAGHA_FIGURE_RE = re.compile(r'data-testid="price-figure"[^>]*>([\d.,]+)</span>')


def fetch_isagha(debug):
    raw, error = http_get(ISAGHA_URL)
    if error:
        debug["error"] = f"could not load {ISAGHA_URL} ({error})"
        return {}
    out = {}
    raw_values = {}
    for m in ISAGHA_ROW_RE.finditer(raw):
        karat = int(m.group(1))
        if karat not in KARATS:
            continue
        figures = [num(x) for x in ISAGHA_FIGURE_RE.findall(m.group(2))]
        if len(figures) < 3 or figures[0] is None or figures[2] is None:
            continue
        sell_to_customer = figures[0]   # "بيع" column -> our buy
        buy_from_customer = figures[2]  # "شراء" column -> our sell
        if not source_consistent(sell_to_customer, buy_from_customer):
            continue
        raw_values[karat] = (sell_to_customer, buy_from_customer)
    if not karats_consistent(raw_values):
        debug["error"] = f"18/21/24 ratios don't line up ({raw_values})"
        debug["raw"] = raw_values
        return {}
    for k, v in raw_values.items():
        out[k] = v
    debug["chosen"] = out
    return out


# --------------------------------------------------------------------------
# source 3: bulliontradingcenter.com (BTC) - no 18K on the homepage.
#   Dealer-centric: "تبيع BTC" (higher) -> our buy; "تشتري BTC" (lower) -> our sell.
# --------------------------------------------------------------------------

BTC_URL = "https://bulliontradingcenter.com/ar/"
BTC_BLOCKS = {21: "21K Gold", 24: "24K/ Gold"}


def fetch_btc(debug):
    raw, error = http_get(BTC_URL)
    if error:
        debug["error"] = f"could not load {BTC_URL} ({error})"
        return {}
    raw_values = {}
    for karat, heading in BTC_BLOCKS.items():
        pat = re.compile(
            re.escape(f"<h2>{heading}</h2>") + r".{0,400}?>([\d,]+\.\d+)\s*EGP.{0,400}?>([\d,]+\.\d+)\s*EGP",
            re.S,
        )
        m = pat.search(raw)
        if not m:
            continue
        sell_to_customer = num(m.group(1))  # "تبيع BTC" -> our buy
        buy_from_customer = num(m.group(2))  # "تشتري BTC" -> our sell
        if not source_consistent(sell_to_customer, buy_from_customer):
            continue
        raw_values[karat] = (sell_to_customer, buy_from_customer)
    if not karats_consistent(raw_values):  # only checks when 24 is present
        debug["error"] = f"21/24 ratio doesn't line up ({raw_values})"
        debug["raw"] = raw_values
        return {}
    debug["chosen"] = raw_values
    return raw_values


# --------------------------------------------------------------------------
# source 4: gold-era.eg - the price table is filled in by client-side JS, so a
#   plain urllib fetch only sees "جارٍ تحميل الأسعار...". We render it with a
#   headless Chromium (Playwright) instead. Customer-centric, same convention
#   as goldbullioneg.com: "سعر الشراء" (higher) -> our buy; "سعر البيع" (lower)
#   -> our sell. If Playwright/Chromium isn't available this run, this source
#   is simply skipped (the other three are enough to keep going).
# --------------------------------------------------------------------------

GOLDERA_URL = "https://gold-era.eg/ar/%D8%B3%D8%B9%D8%B1-%D8%A7%D9%84%D8%B0%D9%87%D8%A8/"
GOLDERA_KARAT_LABEL = {24: "عيار 24", 21: "عيار 21", 18: "عيار 18"}
GOLDERA_ROW_RE = re.compile(
    r"عيار\s*(24|21|18)\s*\n+\s*([\d.,]+)\s*\n+\s*([\d.,]+)"
)


def _goldera_render_text():
    """Runs a tiny Playwright script in a subprocess and returns the rendered
    page's visible text, or None if Playwright/Chromium isn't usable here."""
    script = r"""
import sys
try:
    from playwright.sync_api import sync_playwright
except Exception as exc:
    print("PWMISSING:" + str(exc), file=sys.stderr)
    sys.exit(2)
url = sys.argv[1]
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        print(page.inner_text("body"))
        browser.close()
except Exception as exc:
    print("PWFAIL:" + str(exc), file=sys.stderr)
    sys.exit(3)
"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, GOLDERA_URL],
            capture_output=True, text=True, timeout=70,
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr or "unknown Playwright failure")[:300]
    return proc.stdout, None


def fetch_goldera(debug):
    text, error = _goldera_render_text()
    if error or not text:
        debug["error"] = f"could not render {GOLDERA_URL} ({error})"
        return {}
    text = text.translate(DIGITS).replace("٬", ",").replace("٫", ".")
    raw_values = {}
    for m in GOLDERA_ROW_RE.finditer(text):
        karat = int(m.group(1))
        if karat not in KARATS:
            continue
        buy = num(m.group(2))   # "سعر الشراء" -> our buy (already the higher number)
        sell = num(m.group(3))  # "سعر البيع" -> our sell
        if not source_consistent(buy, sell):
            continue
        raw_values[karat] = (buy, sell)
    if not karats_consistent(raw_values):
        debug["error"] = f"18/21/24 ratios don't line up ({raw_values})"
        debug["raw"] = raw_values
        return {}
    debug["chosen"] = raw_values
    return raw_values


SOURCES = [
    ("goldbullioneg.com", fetch_goldbullioneg),
    ("gold-era.eg", fetch_goldera),
    ("isagha.com", fetch_isagha),
    ("bulliontradingcenter.com", fetch_btc),
]


# --------------------------------------------------------------------------
# combining the sources
# --------------------------------------------------------------------------

def combine_karat(karat, per_source, debug):
    """per_source: {source_name: (buy, sell)} for sources that returned this karat.
    Returns (buy, sell, used_sources) or None if no agreeing cluster exists."""
    if not per_source:
        debug[karat] = {"status": "no source provided this karat"}
        return None

    names = list(per_source.keys())
    if len(names) == 1:
        buy, sell = per_source[names[0]]
        debug[karat] = {"status": "single source", "used": names}
        return buy, sell, names

    # drop whichever source disagrees most with the group, one at a time,
    # until everyone left is within OUTLIER_TOLERANCE of the median (buy side
    # decides membership; sell is pulled from the same sources for consistency)
    remaining = dict(per_source)
    while len(remaining) >= 2:
        buys = {n: v[0] for n, v in remaining.items()}
        median_buy = statistics.median(buys.values())
        deviations = {n: abs(b - median_buy) / median_buy for n, b in buys.items()}
        worst_name = max(deviations, key=deviations.get)
        if deviations[worst_name] <= OUTLIER_TOLERANCE:
            break
        del remaining[worst_name]

    if len(remaining) == 0:
        debug[karat] = {"status": "all sources disagree", "candidates": per_source}
        return None
    if len(remaining) == 1:
        name = next(iter(remaining))
        buy, sell = remaining[name]
        debug[karat] = {"status": "only one source survived outlier check",
                         "used": [name], "dropped": [n for n in names if n != name],
                         "candidates": per_source}
        return buy, sell, [name]

    buy = statistics.median(v[0] for v in remaining.values())
    sell = statistics.median(v[1] for v in remaining.values())
    debug[karat] = {"status": "median of agreeing sources", "used": list(remaining),
                     "dropped": [n for n in names if n not in remaining],
                     "candidates": per_source}
    return buy, sell, list(remaining)


def load_previous():
    try:
        with open(PRICE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def arabic_display(dt):
    hour12 = dt.hour % 12 or 12
    suffix = "ص" if dt.hour < 12 else "م"
    return f"{dt.day} {MONTHS_AR[dt.month - 1]} {dt.year} - {hour12}:{dt.minute:02d} {suffix}"


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")
    print(f"{key}={value}")


def fail(reason, now, source_debug, combine_debug):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    write_json(os.path.join(DEBUG_DIR, "status.json"), {
        "status": "failed",
        "reason": reason,
        "checked_at": now.isoformat(timespec="seconds"),
        "sources": source_debug,
        "combine": {str(k): v for k, v in combine_debug.items()},
    })
    set_output("status", "failed")
    set_output("message", f"Gold price update skipped: {reason}")
    print(f"FAILED: {reason}", file=sys.stderr)


def main():
    now = datetime.datetime.now(CAIRO).replace(microsecond=0)

    per_karat_sources = {k: {} for k in KARATS}
    source_debug = {}
    sources_used_overall = set()

    for name, fetcher in SOURCES:
        dbg = {}
        try:
            result = fetcher(dbg)
        except Exception as exc:
            dbg["error"] = f"unexpected error: {type(exc).__name__}: {exc}"
            result = {}
        source_debug[name] = dbg
        for karat, (buy, sell) in result.items():
            per_karat_sources[karat][name] = (buy, sell)

    combine_debug = {}
    combined = {}
    for karat in KARATS:
        result = combine_karat(karat, per_karat_sources[karat], combine_debug)
        if result is None:
            return fail(f"karat {karat}: {combine_debug[karat]['status']}",
                        now, source_debug, combine_debug)
        buy, sell, used = result
        combined[karat] = (buy, sell)
        sources_used_overall.update(used)

    previous = load_previous()
    prev21 = previous.get("karat21_buy")
    prev_time = previous.get("updated_at")
    if prev21 and prev_time:
        try:
            age = now - datetime.datetime.fromisoformat(prev_time)
        except ValueError:
            age = datetime.timedelta(days=999)
        jump = abs(combined[21][0] - prev21) / prev21
        if age < datetime.timedelta(hours=24) and jump > MAX_JUMP:
            return fail(f"21k buy moved {jump:.1%} ({prev21} -> {combined[21][0]}) in {age}",
                        now, source_debug, combine_debug)

    source_label = " + ".join(sorted(sources_used_overall))
    data = {
        "karat18_buy": round(combined[18][0], 2),
        "karat21_buy": round(combined[21][0], 2),
        "karat24_buy": round(combined[24][0], 2),
        "karat18_sell": round(combined[18][1], 2),
        "karat21_sell": round(combined[21][1], 2),
        "karat24_sell": round(combined[24][1], 2),
        "updated_at": now.isoformat(timespec="seconds"),
        "updated_at_display": arabic_display(now),
        "source": source_label,
    }

    # GitHub runs this job every 30 minutes (its scheduler often skips runs), but we only
    # commit when the price changed or the last saved update is ~2 hours old.
    keys = [k for k in data if k.startswith("karat")]
    same_prices = all(previous.get(k) == data[k] for k in keys)
    if same_prices and prev_time:
        try:
            since = now - datetime.datetime.fromisoformat(prev_time)
        except ValueError:
            since = datetime.timedelta(days=999)
        if since < datetime.timedelta(minutes=110):
            set_output("status", "ok")
            set_output("message", "Prices unchanged; nothing to commit")
            return

    write_json(PRICE_FILE, data)

    os.makedirs(DEBUG_DIR, exist_ok=True)
    write_json(os.path.join(DEBUG_DIR, "status.json"), {
        "status": "ok",
        "checked_at": data["updated_at"],
        "sources": source_debug,
        "combine": {str(k): v for k, v in combine_debug.items()},
    })
    set_output("status", "ok")
    set_output("message", (f"Update reference gold prices: 18={data['karat18_buy']} "
                           f"21={data['karat21_buy']} 24={data['karat24_buy']} (buy) / "
                           f"18={data['karat18_sell']} 21={data['karat21_sell']} "
                           f"24={data['karat24_sell']} (sell) [{source_label}]"))


if __name__ == "__main__":
    main()
