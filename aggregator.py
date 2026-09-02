#!/usr/bin/env python3
import re
import time
import json
import random
from dataclasses import dataclass, asdict
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------
# KONFIGURATION
# ------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    )
}

# Deine Themen + Links
TOPICS = [
    {
        "name": "3D - Thüringen",
        "urls": [
            "https://www.kleinanzeigen.de/s-thueringen/preis::250/4k-3d/k0l3548",
        ],
    },
    {
        "name": "3D - Berlin",
        "urls": [
            "https://www.kleinanzeigen.de/s-berlin/preis::250/4k-3d/k0l3331",
        ],
    },
    {
        "name": "3D - Deutschland",
        "urls": [
            "https://www.kleinanzeigen.de/s-preis::250/4K%203D/k0",
        ],
    },
]

# Ausgabe-Dateien
OUTPUT_JSON = "results.json"
OUTPUT_HTML = "results.html"

# ------------------------------------------------------------
# DATENSTRUKTUR
# ------------------------------------------------------------

@dataclass
class Ad:
    topic: str
    title: str
    price: Optional[int]
    location: str
    url: str

# ------------------------------------------------------------
# HILFSFUNKTIONEN
# ------------------------------------------------------------

def parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)", text.replace(".", ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def parse_listing_page(html: str) -> List[Ad]:
    soup = BeautifulSoup(html, "html.parser")
    ads: List[Ad] = []

    for article in soup.find_all("article"):
        a_title = article.find("a", attrs={"class": re.compile("aditem.*")})
        if not a_title:
            continue

        title = a_title.get_text(strip=True)
        url = a_title.get("href", "")
        if url.startswith("/"):
            url = "https://www.kleinanzeigen.de" + url

        price_el = article.find("p", attrs={"class": re.compile("aditem-main--price")})
        price_text = price_el.get_text(strip=True) if price_el else ""
        price = parse_price(price_text)

        loc_el = article.find("div", attrs={"class": re.compile("aditem-main--top")})
        location = loc_el.get_text(" ", strip=True) if loc_el else ""

        ads.append(
            Ad(
                topic="",
                title=title,
                price=price,
                location=location,
                url=url,
            )
        )

    return ads


def fetch_all_pages(session: requests.Session, base_url: str) -> List[Ad]:
    all_ads: List[Ad] = []

    # Seite 1
    print(f"[INFO] Hole Seite 1: {base_url}")
    resp = session.get(base_url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    ads = parse_listing_page(resp.text)
    print(f"[INFO] Seite 1: {len(ads)} Anzeigen")
    all_ads.extend(ads)

    # Seiten 2–20
    for page in range(2, 21):
        base_clean = re.sub(r"/seite:\d+/?", "/", base_url).rstrip("/")
        url = f"{base_clean}/seite:{page}/"

        print(f"[INFO] Probiere Seite {page}: {url}")
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        final_url = resp.url
        m = re.search(r"/seite:(\d+)", final_url)
        real_page = int(m.group(1)) if m else 1

        print(f"[INFO] Tatsächliche Seite laut Kleinanzeigen: {real_page}")

        if real_page < page:
            print(f"[INFO] → letzte Seite erreicht ({real_page})")
            break

        ads = parse_listing_page(resp.text)
        print(f"[INFO] Seite {real_page}: {len(ads)} Anzeigen")

        if not ads:
            break

        all_ads.extend(ads)
        time.sleep(random.uniform(1.0, 2.0))

    return all_ads


def save_json(ads: List[Ad], path: str) -> None:
    data = [asdict(ad) for ad in ads]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[INFO] JSON gespeichert: {path}")


def save_html(ads: List[Ad], path: str) -> None:
    html = []

    html.append("""
<!DOCTYPE html>
<html lang='de'>
<head>
<meta charset='utf-8'>
<title>Kleinanzeigen Übersicht</title>
<style>
body { font-family: sans-serif; margin: 20px; }
input { padding: 10px; margin-bottom: 8px; width: 100%; }
.card { border: 1px solid #ccc; padding: 10px; margin-bottom: 10px; border-radius: 6px; }
.card-title { font-weight: bold; }
.card-price { color: green; font-weight: bold; }
</style>
</head>
<body>
<h1>Kleinanzeigen Übersicht</h1>

<div style='max-width:900px;margin:0 auto 20px auto;'>
    <input id="filter-title" oninput="applyFilters()" placeholder="Titel enthält…">
    <input id="filter-location" oninput="applyFilters()" placeholder="Ort enthält…">
    <div style="display:flex;gap:10px;">
        <input id="filter-price-min" oninput="applyFilters()" placeholder="Preis min" style="flex:1;">
        <input id="filter-price-max" oninput="applyFilters()" placeholder="Preis max" style="flex:1;">
    </div>
</div>

<div id="results">
""")

    for ad in ads:
        price_str = f"{ad.price} €" if ad.price is not None else "-"
        html.append(f"""
<div class="card">
    <div class="card-title">{ad.title}</div>
    <div class="card-price">{price_str}</div>
    <div class="card-meta">{ad.location}</div>
    <a href="{ad.url}" target="_blank">Anzeigenlink</a>
</div>
""")

    html.append("""
</div>

<script>
function applyFilters() {
    const titleFilter = document.getElementById("filter-title").value.toLowerCase();
    const locationFilter = document.getElementById("filter-location").value.toLowerCase();
    const priceMin = parseFloat(document.getElementById("filter-price-min").value) || 0;
    const priceMax = parseFloat(document.getElementById("filter-price-max").value) || Infinity;

    document.querySelectorAll(".card").forEach(card => {
        const title = card.querySelector(".card-title").innerText.toLowerCase();
        const location = card.querySelector(".card-meta").innerText.toLowerCase();
        const priceText = card.querySelector(".card-price").innerText.replace(/[^0-9]/g, "");
        const price = parseFloat(priceText) || 0;

        const matchesTitle = title.includes(titleFilter);
        const matchesLocation = location.includes(locationFilter);
        const matchesPrice = price >= priceMin && price <= priceMax;

        card.style.display = (matchesTitle && matchesLocation && matchesPrice) ? "block" : "none";
    });
}
</script>

</body>
</html>
""")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(html))

    print(f"[INFO] HTML gespeichert: {path}")


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main() -> None:
    session = requests.Session()
    all_ads: List[Ad] = []

    print(f"[INFO] Starte Scraping…")

    for topic in TOPICS:
        topic_name = topic["name"]
        for url in topic["urls"]:
            print(f"\n[TOPIC] {topic_name} -> {url}")
            ads = fetch_all_pages(session, url)
            for ad in ads:
                ad.topic = topic_name
            all_ads.extend(ads)

    print(f"\n[INFO] Insgesamt {len(all_ads)} Anzeigen gesammelt.")

    save_json(all_ads, OUTPUT_JSON)
    save_html(all_ads, OUTPUT_HTML)

    print("[INFO] Fertig.")


if __name__ == "__main__":
    main()
