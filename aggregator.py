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
# None = keine Begrenzung. Vorher war das hart auf 60 gesetzt, wodurch nur
# 60 der z.B. 478 gefundenen Anzeigen angezeigt/im RSS-Feed gelistet wurden.
# Falls die Seiten dadurch zu groß/langsam werden, hier z.B. 300 eintragen.
MAX_ADS_PER_TOPIC = None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

AD_URL_RE = re.compile(r"/s-anzeige/[^/]+/(\d+)-")
PRICE_RE = re.compile(r"([\d.,]+)\s*€(?:\s*VB)?|Zu verschenken|VB")
PLZ_ORT_RE = re.compile(r"\b\d{5}\s+[A-ZÄÖÜ][\wÄÖÜäöüß\-\s/]+")
DATUM_RE = re.compile(r"(Heute|Gestern),\s*\d{2}:\d{2}|\d{2}\.\d{2}\.\d{4}")

INVALID_FILENAME_CHARS = '\\/:*?"<>|'

EXCLUDE_KEYWORDS = [
    "defekt", "kaputt", "bastler", "nur teile", "ohne funktion",
    "funktioniert nicht", "schrott", "als ersatzteil"
]

# ---------- Filter-Konfiguration ----------
TITLE_CONTAINS = []          
LOCATION_CONTAINS = []       
MIN_PRICE = None             
MAX_PRICE = 250              


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
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("fetched_urls", {})
    data.setdefault("ads", {})
    return data


def save_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def needs_refetch(cache: dict, url: str) -> bool:
    fetched = cache.get("fetched_urls", {})
    last = fetched.get(url)
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return age_hours >= CACHE_HOURS


def parse_price_to_int(price_str: str | None) -> int | None:
    if not price_str:
        return None
    price_str = price_str.strip()
    if "Zu verschenken" in price_str:
        return 0
    m = re.search(r"([\d.,]+)", price_str)
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try:
        return int(float(num))
    except ValueError:
        return None


def passes_filters(ad: dict) -> bool:
    title = (ad.get("title") or "").lower()
    location = (ad.get("location") or "").lower()
    price_val = parse_price_to_int(ad.get("price"))

    if TITLE_CONTAINS:
        if not any(k.lower() in title for k in TITLE_CONTAINS):
            return False

    if LOCATION_CONTAINS:
        if not any(k.lower() in location for k in LOCATION_CONTAINS):
            return False

    if MIN_PRICE is not None and price_val is not None:
        if price_val < MIN_PRICE:
            return False

    if MAX_PRICE is not None and price_val is not None:
        if price_val > MAX_PRICE:
            return False

    return True


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


def parse_listing_page(html: str, session: requests.Session) -> tuple[list[dict], str | None]:
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

        try:
            details = fetch_detail_page(session, href)
            title = details["title"] or title
            price = details["price"] or price
            location = details["location"] or location
        except requests.RequestException:
            pass

        ad = {
            "id": ad_id,
            "title": title,
            "price": price,
            "location": location,
            "date": date,
            "image": image,
            "url": href,
        }

        if passes_filters(ad):
            results.append(ad)

    next_url = None
    return results, next_url


def build_page_url(base_url: str, page: int) -> str:
    """
    Baut die korrekte Kleinanzeigen-Pagination-URL.

    Kleinanzeigen erwartet die Seitenzahl als EIGENES Pfadsegment direkt
    nach '/s-', nicht als Anhängsel am Ende der URL:

        richtig: https://www.kleinanzeigen.de/s-seite:2/thueringen/preis::250/k0l3547
        falsch:  https://www.kleinanzeigen.de/s-thueringen/preis::250/k0l3547/seite:2/

    Die alte Implementierung hängte '/seite:N/' ans Ende an. Das ergab eine
    URL, die Kleinanzeigen nicht kennt -> die Seite hat dann automatisch auf
    eine generische/ungefilterte Suche umgeleitet (daher die falschen
    Ergebnisse, die du beobachtet hast).
    """
    # Falls die Basis-URL schon ein seite:-Segment enthält, erst entfernen.
    base = re.sub(r"/s-seite:\d+/", "/s-", base_url)

    if page <= 1:
        return base

    # 'seite:N/' direkt hinter dem ersten '/s-' einfügen.
    return base.replace("/s-", f"/s-seite:{page}/", 1)


def fetch_all_pages(session: requests.Session, base_url: str) -> list[dict]:
    all_ads: list[dict] = []
    seen_ids: set[str] = set()

    # Seite 1
    resp = session.get(base_url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    ads, _ = parse_listing_page(resp.text, session)
    for ad in ads:
        if ad["id"] not in seen_ids:
            seen_ids.add(ad["id"])
            all_ads.append(ad)
    print("Seite 1:", len(ads))

    # Seiten 2–20
    for page in range(2, 21):
        url = build_page_url(base_url, page)
        print(f"Probiere Seite {page}: {url}")

        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        # Redirect-Erkennung: Kleinanzeigen leitet auf eine andere Seite
        # (meist zurück auf Seite 1) um, wenn die angeforderte Seite nicht
        # existiert. Das erkennen wir daran, dass die tatsächlich
        # ausgelieferte Seite in der finalen URL nicht mit der angefragten
        # übereinstimmt.
        final_url = resp.url
        m = re.search(r"/s-seite:(\d+)/", final_url)
        real_page = int(m.group(1)) if m else 1

        if real_page != page:
            print(f"→ Kleinanzeigen hat auf Seite {real_page} umgeleitet "
                  f"(Seite {page} existiert nicht). Pagination gestoppt.")
            break

        ads, _ = parse_listing_page(resp.text, session)
        new_ads = [ad for ad in ads if ad["id"] not in seen_ids]
        print(f"Seite {real_page}: {len(ads)} Anzeigen ({len(new_ads)} neu)")

        if not new_ads:
            print("→ Keine neuen Anzeigen mehr. Pagination gestoppt.")
            break

        for ad in new_ads:
            seen_ids.add(ad["id"])
        all_ads.extend(new_ads)

        time.sleep(random.uniform(1, 2))

    return all_ads



def update_cache_with_ads(cache: dict, topic: str, ads: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    ads_root = cache.setdefault("ads", {})
    topic_ads = ads_root.setdefault(topic, {})
    for ad in ads:
        if ad["id"] not in topic_ads:
            ad["first_seen"] = now
            topic_ads[ad["id"]] = ad
        else:
            first_seen = topic_ads[ad["id"]].get("first_seen", now)
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

.search-box{
  display:block;
  width:100%;
  max-width:420px;
  margin:0 auto 20px auto;
  padding:10px 14px;
  font-size:1em;
  border:1px solid #ccc;
  border-radius:8px;
  box-sizing:border-box;
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
        "<input type='text' id='topicSearch' class='search-box' "
        "placeholder='Thema suchen…' oninput='filterTopics()' autocomplete='off'>",
        "<ul class='topic-list' id='topicList'>",
    ]

    for topic, ads in cache.get("ads", {}).items():
        link = f"{themen_dir.name}/{safe_filename(topic)}.html"
        parts.append(
            f"<li><a href='{link}'>{topic}"
            f"<span class='count'>{len(ads)} Anzeigen</span></a></li>"
        )

    parts.append("</ul>")
    parts.append("""
<script>
function filterTopics() {
  var q = document.getElementById('topicSearch').value.trim().toLowerCase();
  var terms = q.split(/\\s+/).filter(Boolean);
  document.querySelectorAll('#topicList li').forEach(function (li) {
    var text = li.textContent.toLowerCase();
    var match = terms.every(function (t) { return text.indexOf(t) !== -1; });
    li.style.display = match ? '' : 'none';
  });
}
</script>
""")
    parts.append("</body></html>")
    output.write_text("\n".join(parts), encoding="utf-8")


def build_topic_page(topic: str, ads: dict, themen_dir: Path) -> None:
    themen_dir.mkdir(exist_ok=True)
    ad_list = sorted(ads.values(), key=lambda a: a.get("first_seen", ""), reverse=True)
    display_list = ad_list if MAX_ADS_PER_TOPIC is None else ad_list[:MAX_ADS_PER_TOPIC]

    parts = [
        "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>",
        f"<title>{topic} – Kleinanzeigen</title>",
        f"<style>{CARD_CSS}</style></head><body>",
        "<a class='back-link' href='../index.html'>&larr; Zurück zur Übersicht</a>",
        f"<h1>{topic}</h1>",
        f"<div class='meta-top' id='adCount'>{len(display_list)} Anzeigen</div>",
        "<input type='text' id='adSearch' class='search-box' "
        "placeholder='Anzeigen durchsuchen…' oninput='filterAds()' autocomplete='off'>",
        "<div class='grid' id='adGrid'>",
    ]

    for ad in display_list:
        img = ad.get("image") or ""
        img_html = f"<img src='{img}' loading='lazy'>" if img else ""
        price = ad.get("price") or ""
        location = ad.get("location") or ""
        date = ad.get("date") or ""
        title = ad.get("title") or ""
        title_attr = title.replace("'", "&#39;").lower()
        parts.append(
            f"<a class='card' data-title='{title_attr}' href='{ad['url']}' target='_blank'>"
            f"{img_html}"
            f"<div class='card-body'>"
            f"<div class='card-title'>{title}</div>"
            f"<div class='card-price'>💰 {price}</div>"
            f"<div class='card-meta'>📍 {location}</div>"
            f"<div class='card-meta'>🗓️ {date}</div>"
            f"</div></a>"
        )

    parts.append("</div>")
    parts.append("""
<script>
function filterAds() {
  var q = document.getElementById('adSearch').value.trim().toLowerCase();
  var terms = q.split(/\\s+/).filter(Boolean);
  var visible = 0;
  document.querySelectorAll('#adGrid .card').forEach(function (card) {
    var title = card.dataset.title || '';
    var match = terms.every(function (t) { return title.indexOf(t) !== -1; });
    card.style.display = match ? '' : 'none';
    if (match) visible++;
  });
  document.getElementById('adCount').textContent = visible + ' Anzeigen';
}
</script>
""")
    parts.append("</body></html>")
    (themen_dir / f"{safe_filename(topic)}.html").write_text(
        "\n".join(parts), encoding="utf-8"
    )


def build_all_html(cache: dict, output: Path, themen_dir: Path) -> None:
    build_index_html(cache, output, themen_dir)
    for topic, ads in cache.get("ads", {}).items():
        build_topic_page(topic, ads, themen_dir)


def build_rss_feeds(cache: dict, feeds_dir: Path, site_base_url: str) -> None:
    feeds_dir.mkdir(exist_ok=True)

    for topic, ads in cache.get("ads", {}).items():
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
                print(f"[INFO] Hole {topic}: {url}")
                ads = fetch_all_pages(session, url)   # ⭐ neue Pagination
                print(f"[OK] {topic}: {url} -> {len(ads)} gefilterte Anzeigen gesamt")
                update_cache_with_ads(cache, topic, ads)
                cache["fetched_urls"][url] = datetime.now(timezone.utc).isoformat()
            except requests.RequestException as e:
                print(f"[FEHLER] {url}: {e}")

            save_cache(CACHE_FILE, cache)
            build_all_html(cache, OUTPUT_HTML, THEMEN_DIR)
            build_rss_feeds(cache, FEEDS_DIR, SITE_BASE_URL)
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    print("Fertig.")


if __name__ == "__main__":
    main()
