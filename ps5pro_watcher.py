#!/usr/bin/env python3
"""
PS5 Pro availability watcher (Austria)

Checks shop pages every ~60s and sends an instant push notification with the
purchase link as soon as a console looks buyable.

Setup:
  1. pip install requests
  2. Install the free "ntfy" app on your phone, subscribe to a secret topic
     name of your choice, and put that name in NTFY_TOPIC below.
  3. python ps5pro_watcher.py
"""
import json
import re
import time
import random
import subprocess
import sys
from datetime import datetime

import requests

# ----------------------------- CONFIG ---------------------------------
import os
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "change-me-to-a-long-random-string")  # your private ntfy topic
CHECK_INTERVAL = 60          # seconds between rounds (don't go much lower)
MAX_PRICE_EUR = 1000         # ignore scalper offers above this (set None to disable)
RENOTIFY_AFTER = 15 * 60     # re-alert every 15 min while still available

STORES = {
    "Sony PlayStation Direct AT (1 TB)":
        "https://direct.playstation.com/de-at/buy-consoles/playstation5-pro-console",
    "Sony PlayStation Direct AT (2 TB)":
        "https://direct.playstation.com/de-at/buy-consoles/playstation5-pro-console-2-tb",
    "Sony Direct AT refurbished (2 TB)":
        "https://direct.playstation.com/de-at/buy-consoles/certified-refurbished-playstation5-pro-console",
    "MediaMarkt AT (1 TB, Sony)":
        "https://www.mediamarkt.at/de/product/_sony-playstationr5-pro-2007357.html",
    "MediaMarkt AT (2 TB, Sony)":
        "https://www.mediamarkt.at/de/product/_sony-playstationr5-pro-2-tb-2049935.html",
    "Alza AT (2 TB)":
        "https://www.alza.at/gaming/playstation-5-pro-2tb-d13109460.htm",
    # ---- Add more shops here: open the PS5 Pro PRODUCT page in your browser,
    # ---- copy the address bar URL, and add a line (remove the leading #):
    # "Saturn AT":         "https://www.saturn.at/de/product/...",
    # "Amazon.de (-> AT)": "https://www.amazon.de/dp/ASIN",
    # "Cyberport AT":      "https://www.cyberport.at/...",
    # "Alternate AT":      "https://www.alternate.at/...",
    # "GameStop AT":       "https://www.gamestop.at/...",
    # "Otto AT":           "https://www.otto.at/...",
    # "Expert AT":         "https://www.expert.at/...",
    # "Hartlauer":         "https://www.hartlauer.at/...",
}

# --- Sony Direct "Generalüberholt und Zertifiziert" (refurbished) --------
REFURB_ENABLED = True
REFURB_URL = "https://direct.playstation.com/de-at/certified-refurbished"
# ---------------------------------------------------------------------

# --- Geizhals.at price comparison (alerts when an offer <= max price) ---
GEIZHALS_ENABLED = True
GEIZHALS_PAGES = {
    "Geizhals AT PS5 Pro 2TB": "https://geizhals.at/3298140",
    # add more Geizhals product pages here
}
GEIZHALS_MAX_PRICE = 900    # official price is 899.99 -> alert at/below this
GEIZHALS_MIN_PRICE = 300
# ---------------------------------------------------------------------

# --- Willhaben.at classified ads (new listings, not stock status) -------
WILLHABEN_ENABLED = True
WILLHABEN_KEYWORDS = ["ps5 pro", "playstation5 pro", "playstation 5 pro", "ps 5 pro"]
WILLHABEN_URL = ("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz"
                 "?keyword={kw}&sort=1&rows=30")   # sort=1 -> newest first
WILLHABEN_MAX_PRICE = 900     # only ads at or below this price (EUR)
WILLHABEN_MIN_PRICE = 300     # ignore "1 EUR" bait / accessory ads
WILLHABEN_MAX_AGE_MIN = 8     # alert only for ads published in the last N minutes
WILLHABEN_BAD_WORDS = ["suche", "defekt", "bastler", "ersatzteil", "tausch",
                       "hülle", "huelle", "standfuß", "standfuss", "controller",
                       "laufwerk", "spiel", "ankauf", "kaufe", "reparatur"]
# ---------------------------------------------------------------------

DEADLINE = None   # set in main() when --duration is used

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.5",
}

OUT_PATTERNS = [
    r"nicht zum kauf erhältlich", r"nicht zum kauf erh&auml;ltlich",
    r"zurzeit nicht verf", r"derzeit nicht verf", r"nicht verfügbar",
    r"ausverkauft", r"nicht vorrätig", r"currently unavailable",
    r"out of stock", r"sold out", r"derzeit nicht lieferbar",
    r"keine lieferung möglich", r"bald wieder verfügbar", r"derzeit nicht erhältlich",
]
IN_PATTERNS = [r"in den warenkorb", r"add to cart", r"jetzt kaufen", r"in den einkaufswagen"]


def parse_jsonld(html):
    """Return (availability, price) from schema.org JSON-LD if present."""
    avail, price = None, None
    for block in re.findall(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "availability" in node:
                    avail = str(node["availability"])
                    price = node.get("price", price)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return avail, price


def check(url):
    """Return (status, detail) where status in IN_STOCK / OUT / UNKNOWN."""
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code in (403, 429, 503):
        return "UNKNOWN", f"blocked/rate-limited (HTTP {r.status_code})"
    if r.status_code != 200:
        return "UNKNOWN", f"HTTP {r.status_code}"
    html = r.text
    low = html.lower()

    avail, price = parse_jsonld(html)
    notice = bool(re.search(r"nicht zum kauf erh(ä|&auml;|&#228;)ltlich", low))
    if avail:
        note = " | page says 'Nicht zum Kauf erhältlich'" if notice else ""
        if "instock" in avail.lower() or "limitedavailability" in avail.lower():
            if MAX_PRICE_EUR and price:
                try:
                    if float(str(price).replace(",", ".")) > MAX_PRICE_EUR:
                        return "OUT", f"in stock but price {price} > limit"
                except ValueError:
                    pass
            conflict = " - CONFLICT, verify by hand!" if notice else ""
            return "IN_STOCK", f"schema.org: {avail}, price {price}{note}{conflict}"
        return "OUT", f"schema.org: {avail}{note}"

    # Fallback: text heuristics (less reliable)
    out_hit = next((p for p in OUT_PATTERNS if re.search(p, low)), None)
    in_hit = next((p for p in IN_PATTERNS if re.search(p, low)), None)
    if out_hit:
        extra = f"; buy-button text ALSO present ('{in_hit}')" if in_hit else ""
        return "OUT", f"out-of-stock text found ('{out_hit}'){extra}"
    if in_hit:
        return "IN_STOCK", "buy button text found (verify manually!)"
    return "UNKNOWN", "could not determine (page may load content via JavaScript)"


def _find_key(obj, key):
    """Recursively find the first value stored under `key`."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def willhaben_ads(keyword):
    """Return list of dicts {id,title,price,url,age_min} or raise."""
    from urllib.parse import quote
    r = requests.get(WILLHABEN_URL.format(kw=quote(keyword)), headers=HEADERS, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Willhaben HTTP {r.status_code}")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("Willhaben page format not recognised")
    summaries = _find_key(json.loads(m.group(1)), "advertSummary") or []
    ads = []
    now = time.time()
    for ad in summaries:
        try:
            attrs = {a["name"]: a.get("values", []) for a in ad["attributes"]["attribute"]}
            first = lambda k: (attrs.get(k) or [None])[0]
            title = ad.get("description") or first("HEADING") or ""
            price = float(str(first("PRICE") or "0").replace(",", "."))
            seo = first("SEO_URL") or ""
            url = "https://www.willhaben.at/iad/" + seo.lstrip("/")
            published = first("PUBLISHED")           # epoch milliseconds
            age_min = (now - int(published) / 1000) / 60 if published else None
            ads.append({"id": str(ad.get("id")), "title": title, "price": price,
                        "url": url, "age_min": age_min})
        except Exception:
            continue
    return ads


def check_willhaben(seen):
    ts = datetime.now().strftime("%H:%M:%S")
    ads, errors = {}, []
    for kw in WILLHABEN_KEYWORDS:
        if DEADLINE and time.time() > DEADLINE:
            break
        try:
            for ad in willhaben_ads(kw):
                ads[ad["id"]] = ad          # de-duplicate across keywords
        except Exception as e:
            errors.append(f"'{kw}': {e}")
        time.sleep(random.uniform(0.3, 1.0))
    if errors:
        print(f"[{ts}] Willhaben: UNKNOWN - {'; '.join(errors)}", flush=True)
    if not ads:
        return
    ads = list(ads.values())
    new = 0
    for ad in ads:
        t = ad["title"].lower()
        compact = re.sub(r"[\s\-_.]", "", t)     # "PS 5 Pro" -> "ps5pro"
        if "ps5pro" not in compact and "playstation5pro" not in compact:
            continue
        if any(w in t for w in WILLHABEN_BAD_WORDS):
            continue
        if not (WILLHABEN_MIN_PRICE <= ad["price"] <= WILLHABEN_MAX_PRICE):
            continue
        if ad["age_min"] is not None and ad["age_min"] > WILLHABEN_MAX_AGE_MIN:
            continue
        if ad["id"] in seen:
            continue
        seen.add(ad["id"])
        new += 1
        notify(f"New PS5 Pro ad: EUR {ad['price']:.0f}",
               f"{ad['title']}\n{ad['url']}", ad["url"])
    print(f"[{ts}] Willhaben: {len(ads)} ads read, {new} new match(es)", flush=True)


def check_refurb(last_alert):
    """Watch Sony's refurbished page for a PS5 Pro listing and check its stock."""
    from urllib.parse import urljoin
    import html as htmllib
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        r = requests.get(REFURB_URL, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            print(f"[{ts}] Sony refurbished: UNKNOWN - HTTP {r.status_code}", flush=True)
            return
        page = r.text
    except Exception as e:
        print(f"[{ts}] Sony refurbished: UNKNOWN - {e}", flush=True)
        return

    # 1) product links on the page that look like a PS5 Pro refurbished product
    links = []
    for href in re.findall(r'href="([^"]+)"', page):
        if "certified-refurbished" in href and re.search(r"[-/]pro(?:[-/?#]|$)", href):
            full = urljoin(REFURB_URL, htmllib.unescape(href))
            if full.rstrip("/") != REFURB_URL.rstrip("/") and full not in links:
                links.append(full)
    # 2) fallback: product title text mentioning Pro
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", page))
    title_hit = re.search(
        r"Generalüberholt\w*\s+und\s+Zertifiziert\s+(?:PlayStation\s*®?\s*5|PS5\s*®?)\s*Pro",
        text, re.I)

    if not links and not title_hit:
        print(f"[{ts}] Sony refurbished: no PS5 Pro listed", flush=True)
        return
    if not links:
        links = [REFURB_URL]

    for url in links:
        name = "Sony refurbished PS5 Pro"
        try:
            status, detail = check(url) if url != REFURB_URL else ("UNKNOWN", "listed on page, open it to check")
        except Exception as e:
            status, detail = "UNKNOWN", f"error: {e}"
        print(f"[{ts}] {name}: {status} - {detail} ({url})", flush=True)
        if status in ("IN_STOCK", "UNKNOWN") and time.time() - last_alert.get(url, 0) > RENOTIFY_AFTER:
            last_alert[url] = time.time()
            note = "AVAILABLE" if status == "IN_STOCK" else "LISTED (verify stock)"
            notify(f"PS5 Pro refurbished {note}", f"{detail}\n{url}", url)


def geizhals_lowest(html):
    """Lowest offer price on a Geizhals product page (JSON-LD, else text)."""
    for block in re.findall(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            low = _find_key(json.loads(block), "lowPrice")
        except Exception:
            continue
        if low is not None:
            try:
                return float(str(low).replace(",", ".")), "structured data"
            except ValueError:
                pass
    m = re.search(r"ab\s*(?:&nbsp;|\s)*€\s*(?:&nbsp;|\s)*([\d.]+,\d{2})", html)
    if m:
        return float(m.group(1).replace(".", "").replace(",", ".")), "page text"
    return None, None


def check_geizhals(last_alert):
    for name, url in GEIZHALS_PAGES.items():
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                print(f"[{ts}] {name}: UNKNOWN - HTTP {r.status_code}", flush=True)
                continue
            low, src = geizhals_lowest(r.text)
        except Exception as e:
            print(f"[{ts}] {name}: UNKNOWN - {e}", flush=True)
            continue
        if low is None:
            print(f"[{ts}] {name}: UNKNOWN - no price found on page", flush=True)
            continue
        hit = GEIZHALS_MIN_PRICE <= low <= GEIZHALS_MAX_PRICE
        print(f"[{ts}] {name}: lowest offer EUR {low:.2f} ({src}) - "
              f"{'MATCH' if hit else 'above limit'}", flush=True)
        if hit and time.time() - last_alert.get(url, 0) > RENOTIFY_AFTER:
            last_alert[url] = time.time()
            notify(f"Geizhals: PS5 Pro from EUR {low:.0f}",
                   f"{name}: lowest offer EUR {low:.2f}\n{url}", url)


def notify(title, message, url):
    # Push to phone via ntfy.sh (tapping opens the shop page)
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": "urgent",
                     "Tags": "video_game,rotating_light", "Click": url},
            timeout=10,
        )
    except Exception as e:
        print("  ntfy failed:", e)
    # Local desktop notification + beep (best effort)
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{message}" with title "{title}" sound name "Glass"'])
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", title, f"{message}\n{url}"])
        elif sys.platform == "win32":
            import winsound
            for _ in range(5):
                winsound.Beep(1200, 300)
    except Exception:
        pass
    print("\a")


def main():
    """
    python ps5pro_watcher.py                 -> loop forever (home PC / Pi)
    python ps5pro_watcher.py --once          -> one round, then exit
    python ps5pro_watcher.py --duration 280  -> loop for 280 s, then exit
                                                (used by GitHub Actions)
    """
    once = "--once" in sys.argv
    duration = None
    if "--duration" in sys.argv:
        duration = int(sys.argv[sys.argv.index("--duration") + 1])
    global DEADLINE
    deadline = time.time() + duration if duration else None
    DEADLINE = deadline
    interval = 30 if duration else CHECK_INTERVAL
    scripted = once or duration is not None   # no "check needed" spam on cron runs

    last_alert = {}
    warned_unknown = set()
    wh_seen = set()
    print(f"Watching {len(STORES)} pages every ~{interval}s.")
    while True:
        round_start = time.time()
        for name, url in STORES.items():
            if deadline and time.time() > deadline:
                return
            ts = datetime.now().strftime("%H:%M:%S")
            try:
                status, detail = check(url)
            except Exception as e:
                status, detail = "UNKNOWN", f"error: {e}"
            print(f"[{ts}] {name}: {status} - {detail}", flush=True)

            if status == "IN_STOCK":
                if time.time() - last_alert.get(name, 0) > RENOTIFY_AFTER:
                    last_alert[name] = time.time()
                    notify(f"PS5 Pro available: {name}", f"{detail}\n{url}", url)
            elif status == "UNKNOWN" and not scripted and name not in warned_unknown:
                warned_unknown.add(name)
                notify("PS5 watcher: check needed", f"{name}: {detail}", url)
            time.sleep(random.uniform(0.3, 1.0))
        if REFURB_ENABLED and not (deadline and time.time() > deadline):
            check_refurb(last_alert)
        if GEIZHALS_ENABLED and not (deadline and time.time() > deadline):
            check_geizhals(last_alert)
        if WILLHABEN_ENABLED and not (deadline and time.time() > deadline):
            check_willhaben(wh_seen)
        if once:
            return
        wait = max(1, interval - (time.time() - round_start))
        if deadline and time.time() + wait >= deadline:
            return
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
