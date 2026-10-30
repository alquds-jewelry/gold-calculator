#!/usr/bin/env python3
"""
Fetches today's gold prices (karat 18 / 21 / 24, buy + sell) from goldbullioneg.com
and writes them to price.json for the Al-Quds Jewelry calculator.

Runs inside GitHub Actions every 2 hours. It never publishes a number it is not
sure about:
  * the three karats must agree with each other (21 = 87.5% of 24, 18 = 75% of 24)
  * buy and sell must be close to each other
  * the new 21k price must not jump more than 7% from the last price (if the last
    price is less than 24h old)
If any check fails, price.json is left untouched, a debug snapshot is saved under
debug/, and the workflow run is marked as failed (GitHub emails the repo owner).
"""

import datetime
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

SOURCE_URL = "https://goldbullioneg.com/"
SOURCE_NAME = "goldbullioneg.com"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICE_FILE = os.path.join(ROOT, "price.json")
DEBUG_DIR = os.path.join(ROOT, "debug")
CAIRO = ZoneInfo("Africa/Cairo")

KARATS = (24, 21, 18)
PURITY_VS_24 = {21: 21 / 24, 18: 18 / 24}
RATIO_TOLERANCE = 0.02      # 21/24 and 18/24 must be within 2% of the theoretical ratio
MAX_SPREAD = 0.04           # buy and sell may differ by at most 4%
MAX_JUMP = 0.07             # 21k may not move more than 7% vs a price younger than 24h
PLAUSIBLE_21 = (2000, 30000)

MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
KARAT_RE = re.compile(r"عيار\s*(24|22|21|18|14|12)(?!\d)|(?<!\d)(24|22|21|18|14|12)\s*عيار")
BOUNDARY_RE = re.compile(r"عيار|قيراط|الجنيه الذهب|جنيه ذهب|الجنيه الدهب|أونصة|اونصة|الأوقية|أوقية|اوقية|الدولار")
NUMBER_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d{4,6})(?:\.\d+)?(?!\d)")
WINDOW = 260


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")
    print(f"{key}={value}")


def fetch_html():
    headers = {
        "User-Agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
    }
    last_error = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(SOURCE_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=40) as res:
                return res.read().decode("utf-8", "replace"), None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            last_error = (f"HTTP {exc.code}", body)
        except Exception as exc:  # network error, timeout...
            last_error = (f"{type(exc).__name__}: {exc}", "")
        time.sleep(15 * attempt)
    return last_error[1], last_error[0]


def to_text(raw):
    s = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|section|ul|ol)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.translate(DIGITS)
    s = s.replace("٬", ",").replace("٫", ".")
    s = re.sub(r"[‎‏‪-‮⁦-⁩ ]", " ", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return s


def numbers_in(segment):
    vals = []
    for m in NUMBER_RE.finditer(segment):
        v = int(m.group(1).replace(",", ""))
        if 1000 <= v <= 100000:
            vals.append(v)
    return vals


def collect_candidates(text):
    cands = {k: [] for k in KARATS}
    for m in KARAT_RE.finditer(text):
        karat = int(m.group(1) or m.group(2))
        if karat not in cands:
            continue
        # look AFTER the label, stopping at the next label
        after = text[m.end(): m.end() + WINDOW]
        b = BOUNDARY_RE.search(after)
        if b:
            after = after[: b.start()]
        # and BEFORE the label (for tables where the name column comes last)
        before = text[max(0, m.start() - WINDOW): m.start()]
        bs = list(BOUNDARY_RE.finditer(before))
        if bs:
            before = before[bs[-1].end():]
        for direction, seg in (("after", after), ("before", before)):
            vals = numbers_in(seg)
            if len(vals) < 2:
                continue
            a, c = vals[0], vals[1]
            hi, lo = max(a, c), min(a, c)
            if hi - lo > hi * MAX_SPREAD:
                continue
            snippet = text[max(0, m.start() - 60): m.end() + 160].replace("\n", " ")
            cands[karat].append({"buy": hi, "sell": lo, "dir": direction,
                                 "pos": m.start(), "snippet": snippet.strip()})
    for k in cands:  # prefer label-then-numbers, then document order
        cands[k].sort(key=lambda c: (c["dir"] != "after", c["pos"]))
    return cands


def consistent(c24, c21, c18):
    for field in ("buy", "sell"):
        base = c24[field]
        if abs(c21[field] / base - PURITY_VS_24[21]) > RATIO_TOLERANCE:
            return False
        if abs(c18[field] / base - PURITY_VS_24[18]) > RATIO_TOLERANCE:
            return False
    return PLAUSIBLE_21[0] <= c21["buy"] <= PLAUSIBLE_21[1]


def choose(cands):
    for c24 in cands[24][:15]:
        for c21 in cands[21][:15]:
            for c18 in cands[18][:15]:
                if consistent(c24, c21, c18):
                    return {24: c24, 21: c21, 18: c18}
    return None


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


def fail(reason, now, raw=None, text=None, cands=None):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    if raw:
        with open(os.path.join(DEBUG_DIR, "last_fetch.html"), "w", encoding="utf-8") as fh:
            fh.write(raw[:1_500_000])
    if text:
        with open(os.path.join(DEBUG_DIR, "last_text.txt"), "w", encoding="utf-8") as fh:
            fh.write(text[:400_000])
    write_json(os.path.join(DEBUG_DIR, "status.json"), {
        "status": "failed",
        "reason": reason,
        "checked_at": now.isoformat(timespec="seconds"),
        "candidates": {str(k): v[:5] for k, v in (cands or {}).items()},
    })
    set_output("status", "failed")
    set_output("message", f"Gold price update skipped: {reason}")
    print(f"FAILED: {reason}", file=sys.stderr)


def main():
    now = datetime.datetime.now(CAIRO).replace(microsecond=0)
    raw, error = fetch_html()
    if error:
        return fail(f"could not load {SOURCE_URL} ({error})", now, raw=raw)

    text = to_text(raw)
    cands = collect_candidates(text)
    chosen = choose(cands)
    if not chosen:
        counts = {k: len(v) for k, v in cands.items()}
        return fail(f"no consistent 18/21/24 price set found (candidates: {counts})",
                    now, raw=raw, text=text, cands=cands)

    previous = load_previous()
    prev21 = previous.get("karat21_buy")
    prev_time = previous.get("updated_at")
    if prev21 and prev_time:
        try:
            age = now - datetime.datetime.fromisoformat(prev_time)
        except ValueError:
            age = datetime.timedelta(days=999)
        jump = abs(chosen[21]["buy"] - prev21) / prev21
        if age < datetime.timedelta(hours=24) and jump > MAX_JUMP:
            return fail(f"21k buy moved {jump:.1%} ({prev21} -> {chosen[21]['buy']}) in {age}",
                        now, raw=raw, text=text, cands=cands)

    data = {
        "karat18_buy": chosen[18]["buy"],
        "karat21_buy": chosen[21]["buy"],
        "karat24_buy": chosen[24]["buy"],
        "karat18_sell": chosen[18]["sell"],
        "karat21_sell": chosen[21]["sell"],
        "karat24_sell": chosen[24]["sell"],
        "updated_at": now.isoformat(timespec="seconds"),
        "updated_at_display": arabic_display(now),
        "source": SOURCE_NAME,
    }
    write_json(PRICE_FILE, data)

    os.makedirs(DEBUG_DIR, exist_ok=True)
    for name in ("last_fetch.html", "last_text.txt"):
        p = os.path.join(DEBUG_DIR, name)
        if os.path.exists(p):
            os.remove(p)
    write_json(os.path.join(DEBUG_DIR, "status.json"), {
        "status": "ok",
        "checked_at": data["updated_at"],
        "chosen": {str(k): {"buy": v["buy"], "sell": v["sell"], "snippet": v["snippet"]}
                   for k, v in chosen.items()},
    })
    set_output("status", "ok")
    set_output("message", (f"Update reference gold prices: 18={data['karat18_buy']} "
                           f"21={data['karat21_buy']} 24={data['karat24_buy']} (buy) / "
                           f"18={data['karat18_sell']} 21={data['karat21_sell']} "
                           f"24={data['karat24_sell']} (sell)"))


if __name__ == "__main__":
    main()
