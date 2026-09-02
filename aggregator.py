import re
import json
import random
import time
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape

import requests
from bs4 import BeautifulSoup

# ---------- Konfiguration ----------
BASE_DIR = Path(__file__).resolve().parent

LINKS_FILE = BASE_DIR / "links.txt"
CACHE_FILE = BASE_DIR / "cache.json"
OUTPUT_HTML = BASE_DIR / "index.html"
THEMEN_DIR = BASE_DIR / "themen"
FEEDS_DIR = BASE_DIR / "feeds"

SITE_BASE_URL = "https://github.com/Golb69/kleinanzeigen-uebersicht"

MIN_DELAY = 6
MAX_DELAY = 14
CACHE_HOURS = 6
MAX_ADS_PER_TOPIC = 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

AD_URL_RE = re.compile(r"/s-anzeige/[^/]+/(\d+)-")
PRICE_RE = re.compile(r"[\d.,]+\s*€(?:\s*VB)?|Zu verschenken|VB")
PLZ_ORT_RE = re.compile(r"\b\d{5}\s+[A-ZÄÖÜ][\wÄÖÜäöüß\-\s/]+")
DATUM_RE = re.compile(r"(Heute|Gestern),\s*\d{2}:\d{2}|\d{2}\.\d{2}\.\d{4}")

INVALID_FILENAME_CHARS = '\\/:*?"<>|'

EXCLUDE_KEYWORDS = [
    "defekt", "kaputt", "bastler", "nur teile", "ohne funktion",
    "funktioniert nicht", "schrott", "als ersatzteil"
]


def contains_excluded_words(text: str) -> bool:
    text_lower = text.lower()
    return any(word in text_lower for word in EXCLUDE_KEYWORDS)


def load_links(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"'{path}' wurde nicht gefunden. Bist du im richtigen Ordner? "
            f"Aktueller Ordner: {Path.cwd()}"
        )
    topics: dict[str, list[str]] = {}
    current = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            topics.setdefault(current, [])
        elif current is not None and line.startswith("http"):
            topics[current].append(line)
    return topics


def load_cache(path: Path) -> dict:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        # Migration alter Cache-Dateien
        if "fetched_urls" not in data:
            data["fetched_urls"] = {}
        if "ads" not in data:
            data["ads"] = {}
        return data
    return {"fetched_urls": {}, "ads": {}}



def save_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def needs_refetch(cache: dict, url: str) -> bool:
    last = cache["fetched_urls"].get(url)
    if last is None:
        return True
    last_dt = datetime.fromisoformat(last)
    age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return age_hours >= CACHE_HOURS


def fetch_detail_page(session: requests.Session, url: str) -> dict:
    resp = session.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title_tag = soup.find("h1", id="viewad-title")
    title = title_tag.get_text(strip=True) if title_tag else None

    price_tag = soup.find("h2", id="viewad-price")
    price = price_tag.get_text(strip=True) if price_tag else None

    locality_tag = soup.find("span", id="viewad-locality")
    location = locality_tag.get_text(strip=True) if locality_tag else None

    return {
        "title": title,
        "price": price,
        "location": location
    }


def parse_listing_page(html: str, session: requests.Session) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_ids = set()

    for a in soup.find_all("a", href=True):
        m = AD_URL_RE.search(a["href"])
        if not m:
            continue
        ad_id = m.group(1)
        if ad_id in seen_ids:
            continue
        seen_ids.add(ad_id)

        container = a.find_parent(["article", "li", "div"]) or a

        title = a.get_text(strip=True)
        if not title:
            text_for_title = container.get_text(" ", strip=True)
            parts = text_for_title.split()
            title = " ".join(parts[:10]) if parts else "Anzeige"

        text = container.get_text(" ", strip=True)

        if contains_excluded_words(text):
            continue

        price_match = PRICE_RE.search(text)
        price = price_match.group(0) if price_match else None

        location_match = PLZ_ORT_RE.search(text)
        location = location_match.group(0) if location_match else None

        date_match = DATUM_RE.search(text)
        date = date_match.group(0) if date_match else None

        img_tag = container.find("img")
        image = None
        if img_tag:
            image = img_tag.get("src") or img_tag.get("data-src") or img_tag.get("data-imgsrc")

        href = a["href"]
        if href.startswith("/"):
            href = "https://www.kleinanzeigen.de" + href

        # Detailseite holen und Daten überschreiben/ergänzen
        try:
            details = fetch_detail_page(session, href)
            title = details["title"] or title
            price = details["price"] or price
            location = details["location"] or location
        except requests.RequestException:
            pass

        results.append({
            "id": ad_id,
            "title": title,
            "price": price,
            "location": location,
            "date": date,
            "image": image,
            "url": href,
        })

    return results


def fetch_search_page(session: requests.Session, url: str) -> list[dict]:
    resp = session.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return parse_listing_page(resp.text, session)


def update_cache_with_ads(cache: dict, topic: str, ads: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    topic_ads = cache["ads"].setdefault(topic, {})
    for ad in ads:
        if ad["id"] not in topic_ads:
            ad["first_seen"] = now
            topic_ads[ad["id"]] = ad
        else:
            first_seen = topic_ads[ad["id"]]["first_seen"]
            ad["first_seen"] = first_seen
            topic_ads[ad["id"]] = ad


def safe_filename(topic: str) -> str:
    result = topic.strip()
    for ch in INVALID_FILENAME_CHARS:
        result = result.replace(ch, "_")
    return result


CARD_CSS = """
body{font-family:sans-serif;background:#f5f5f5;margin:0;padding:16px;}
a{text-decoration:none;color:inherit;}
h1{margin-top:0;text-align:center;}
.meta-top{color:#777;font-size:.85em;margin-bottom:16px;text-align:center;}

.topic-list{
  list-style:none;
  padding:0;
  margin:0 auto 24px auto;
  max-width:900px;
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
  gap:12px;
}
.topic-list li{margin:0;}
.topic-list a{
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  background:#fff;
  border-radius:8px;
  padding:14px 18px;
  box-shadow:0 1px 3px rgba(0,0,0,.15);
  font-weight:600;
  font-size:1.0em;
  text-align:center;
}
.topic-list .count{
  color:#777;
  font-weight:400;
  font-size:.85em;
  margin-top:4px;
}

.back-link{
  display:inline-block;
  margin-bottom:16px;
  color:#0a7d3c;
  font-weight:600;
}

.grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
  gap:14px;
  max-width:1100px;
  margin:0 auto 24px auto;
}
.card{
  background:#fff;
  border-radius:8px;
  overflow:hidden;
  box-shadow:0 1px 3px rgba(0,0,0,.15);
  display:flex;
  flex-direction:column;
}
.card img{
  width:100%;
  height:160px;
  object-fit:cover;
  background:#eee;
}
.card-body{padding:10px 12px;}
.card-title{
  font-weight:700;
  font-size:.95em;
  color:#222;
  line-height:1.3;
  margin-bottom:6px;
}
.card-price{
  color:#0a7d3c;
  font-weight:700;
  font-size:.95em;
  margin-bottom:4px;
}
.card-meta{
  color:#777;
  font-size:.8em;
  margin-top:2px;
}
"""


def build_index_html(cache: dict, output: Path, themen_dir: Path) -> None:
    parts = [
        "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>",
        "<title>Kleinanzeigen Übersicht</title>",
        f"<style>{CARD_CSS}</style></head><body>",
        "<h1>Themen</h1>",
        f"<div class='meta-top'>Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}</div>",
        "<ul class='topic-list'>",
    ]

    for topic, ads in cache["ads"].items():
        link = f"{themen_dir.name}/{safe_filename(topic)}.html"
        parts.append(
            f"<li><a href='{link}'>{topic}"
            f"<span class='count'>{len(ads)} Anzeigen</span></a></li>"
        )

    parts.append("</ul></body></html>")
    output.write_text("\n".join(parts), encoding="utf-8")


def build_topic_page(topic: str, ads: dict, themen_dir: Path) -> None:
    themen_dir.mkdir(exist_ok=True)
    ad_list = sorted(ads.values(), key=lambda a: a.get("first_seen", ""), reverse=True)

    parts = [
        "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>",
        f"<title>{topic} – Kleinanzeigen</title>",
        f"<style>{CARD_CSS}</style></head><body>",
        "<a class='back-link' href='../index.html'>&larr; Zurück zur Übersicht</a>",
        f"<h1>{topic}</h1>",
        f"<div class='meta-top'>{len(ad_list)} Anzeigen</div>",
        "<div class='grid'>",
    ]

    for ad in ad_list[:MAX_ADS_PER_TOPIC]:
        img = ad.get("image") or ""
        img_html = f"<img src='{img}' loading='lazy'>" if img else ""
        price = ad.get("price") or ""
        location = ad.get("location") or ""
        date = ad.get("date") or ""
        parts.append(
            f"<a class='card' href='{ad['url']}' target='_blank'>"
            f"{img_html}"
            f"<div class='card-body'>"
            f"<div class='card-title'>{ad['title']}</div>"
            f"<div class='card-price'>💰 {price}</div>"
            f"<div class='card-meta'>📍 {location}</div>"
            f"<div class='card-meta'>🗓️ {date}</div>"
            f"</div></a>"
        )

    parts.append("</div></body></html>")
    (themen_dir / f"{safe_filename(topic)}.html").write_text(
        "\n".join(parts), encoding="utf-8"
    )


def build_all_html(cache: dict, output: Path, themen_dir: Path) -> None:
    build_index_html(cache, output, themen_dir)
    for topic, ads in cache["ads"].items():
        build_topic_page(topic, ads, themen_dir)


def build_rss_feeds(cache: dict, feeds_dir: Path, site_base_url: str) -> None:
    feeds_dir.mkdir(exist_ok=True)

    for topic, ads in cache["ads"].items():
        ad_list = sorted(ads.values(), key=lambda a: a.get("first_seen", ""), reverse=True)
        filename = feeds_dir / f"{safe_filename(topic)}.xml"

        items = []
        for ad in ad_list[:MAX_ADS_PER_TOPIC]:
            try:
                pub_dt = datetime.fromisoformat(ad.get("first_seen", ""))
            except ValueError:
                pub_dt = datetime.now(timezone.utc)

            title = xml_escape(ad["title"])
            link = xml_escape(ad["url"])
            price = xml_escape(ad.get("price") or "")
            location = xml_escape(ad.get("location") or "")
            desc_parts = [p for p in [price, location] if p]
            description = " · ".join(desc_parts)
            image = ad.get("image")
            image_html = f"<img src='{xml_escape(image)}'/><br/>" if image else ""

            items.append(f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="false">{ad['id']}</guid>
      <pubDate>{format_datetime(pub_dt)}</pubDate>
      <description><![CDATA[{image_html}{description}]]></description>
    </item>""")

        feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Kleinanzeigen: {xml_escape(topic)}</title>
    <link>{xml_escape(site_base_url)}</link>
    <description>Automatisch aktualisierte Kleinanzeigen-Ergebnisse für {xml_escape(topic)}</description>
    <language>de-de</language>
    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>
{''.join(items)}
  </channel>
</rss>"""
        filename.write_text(feed_xml, encoding="utf-8")


def main() -> None:
    topics = load_links(LINKS_FILE)
    cache = load_cache(CACHE_FILE)

    total_links = sum(len(urls) for urls in topics.values())
    print(f"{len(topics)} Themen, {total_links} Links insgesamt.")

    session = requests.Session()

    for topic, urls in topics.items():
        for url in urls:
            if not needs_refetch(cache, url):
                continue
            try:
                ads = fetch_search_page(session, url)
                update_cache_with_ads(cache, topic, ads)
                cache["fetched_urls"][url] = datetime.now(timezone.utc).isoformat()
                print(f"[OK] {topic}: {url} -> {len(ads)} Anzeigen")
            except requests.RequestException as e:
                print(f"[FEHLER] {url}: {e}")

            save_cache(CACHE_FILE, cache)
            build_all_html(cache, OUTPUT_HTML, THEMEN_DIR)
            build_rss_feeds(cache, FEEDS_DIR, SITE_BASE_URL)
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    print("Fertig.")


if __name__ == "__main__":
    main()
