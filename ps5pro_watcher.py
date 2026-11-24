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
# ---------------------------------------------------------------------

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.5",
}

OUT_PATTERNS = [
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
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code in (403, 429, 503):
        return "UNKNOWN", f"blocked/rate-limited (HTTP {r.status_code})"
    if r.status_code != 200:
        return "UNKNOWN", f"HTTP {r.status_code}"
    html = r.text
    low = html.lower()

    avail, price = parse_jsonld(html)
    if avail:
        if "instock" in avail.lower() or "limitedavailability" in avail.lower():
            if MAX_PRICE_EUR and price:
                try:
                    if float(str(price).replace(",", ".")) > MAX_PRICE_EUR:
                        return "OUT", f"in stock but price {price} > limit"
                except ValueError:
                    pass
            return "IN_STOCK", f"schema.org: {avail}, price {price}"
        return "OUT", f"schema.org: {avail}"

    # Fallback: text heuristics (less reliable)
    if any(re.search(p, low) for p in OUT_PATTERNS):
        return "OUT", "out-of-stock text found"
    if any(re.search(p, low) for p in IN_PATTERNS):
        return "IN_STOCK", "buy button text found (verify manually!)"
    return "UNKNOWN", "could not determine (page may load content via JavaScript)"


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
    deadline = time.time() + duration if duration else None
    interval = 30 if duration else CHECK_INTERVAL
    scripted = once or duration is not None   # no "check needed" spam on cron runs

    last_alert = {}
    warned_unknown = set()
    print(f"Watching {len(STORES)} pages every ~{interval}s.")
    while True:
        round_start = time.time()
        for name, url in STORES.items():
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
