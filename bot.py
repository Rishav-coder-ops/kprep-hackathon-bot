import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
STATE_FILE = "seen.json"
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "15"))
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "45"))
MAX_CANDIDATES_PER_RUN = int(os.getenv("MAX_CANDIDATES_PER_RUN", "60"))

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; K-PrepsHackathonBot/2.0; "
        "+https://github.com/)"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

# Search results are discovery leads, not proof of organizer identity.
TRUSTED_DOMAINS = {
    "devfolio.co", "unstop.com", "hackerearth.com", "hack2skill.com",
    "gdg.community.dev", "developers.google.com", "sih.gov.in",
    "mygov.in", "aws.amazon.com", "devpost.com"
}

QUERIES = [
    'hackathon India registration',
    'hackathon India students online offline',
    'site:devfolio.co hackathon India',
    'site:unstop.com hackathon India',
    'site:hackerearth.com hackathon India',
    'site:hack2skill.com hackathon India',
    'site:gdg.community.dev hackathon India',
    'site:mygov.in hackathon India OR innovation challenge'
]

INDIA_SIGNALS = {
    "india", "indian", "bengaluru", "bangalore", "mumbai", "pune",
    "hyderabad", "chennai", "delhi", "new delhi", "kolkata", "bhubaneswar",
    "gurugram", "gurgaon", "noida", "jaipur", "ahmedabad", "surat",
    "lucknow", "kanpur", "indore", "bhopal", "kochi", "thiruvananthapuram",
    "thiruvananthapuram", "visakhapatnam", "vizag", "nagpur", "patna",
    "ranchi", "guwahati", "chandigarh", "dehradun", "srinagar", "shimla",
    "goa", "west bengal", "odisha", "maharashtra", "karnataka", "telangana",
    "tamil nadu", "kerala", "rajasthan", "gujarat", "uttar pradesh",
    "madhya pradesh", "bihar", "jharkhand", "assam", "punjab", "haryana",
    "andhra pradesh", "uttarakhand", "himachal pradesh"
}

def clean_html(value):
    """Decode RSS/HTML text before placing it in a Discord message."""
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()

def clean_url(value, base_url=""):
    """Return an absolute HTTP(S) URL, or an empty string for unsafe values."""
    raw = html.unescape(str(value or "")).strip()
    if not raw:
        return ""
    try:
        url = urljoin(base_url, raw)
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return ""
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""

def clip(value, limit):
    value = clean_html(value)
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"

def domain_of(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""

def trusted_domain(host):
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)

def load_seen():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(seen):
    # Keep a bounded history so the repository state file stays small.
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-5000:], f, indent=2)

def resolve_article_url(url):
    try:
        r = requests.get(url, timeout=20, allow_redirects=True,
                         headers=REQUEST_HEADERS)
        r.raise_for_status()
        return r.url
    except requests.RequestException:
        return url

def fetch_event_page(url):
    """Fetch an approved event page and return its HTML plus final URL."""
    if not url:
        return "", url
    try:
        response = requests.get(url, timeout=20, allow_redirects=True,
                                headers=REQUEST_HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        return "", url

    final_url = clean_url(response.url) or url
    content_type = response.headers.get("Content-Type", "").lower()
    if not trusted_domain(domain_of(final_url)):
        return "", final_url
    if content_type and "html" not in content_type:
        return "", final_url
    return response.text, final_url

def published_datetime(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)

def is_india_event(text):
    low = text.lower()
    return any(signal in low for signal in INDIA_SIGNALS)

def is_hackathon(text):
    low = text.lower()
    return any(word in low for word in ("hackathon", "hack fest", "coding challenge", "innovation challenge"))

def strip_source_suffix(value, source):
    """Remove a publisher suffix such as ' - Unstop' from an RSS headline."""
    value = clean_html(value)
    source = clean_html(source)
    if not source:
        return value
    return re.sub(
        rf"\s*(?:[-–—|]\s*)?{re.escape(source)}\s*$",
        "", value, flags=re.IGNORECASE
    ).strip(" -–—|")

def meta_content(soup, *keys):
    wanted = {key.lower() for key in keys}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        if key in wanted and tag.get("content"):
            return clean_html(tag["content"])
    return ""

def meta_url(soup, base_url, *keys):
    wanted = {key.lower() for key in keys}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        if key in wanted and tag.get("content"):
            url = clean_url(tag["content"], base_url)
            if url:
                return url
    return ""

def first_url(value, base_url):
    if isinstance(value, str):
        return clean_url(value, base_url)
    if isinstance(value, list):
        for item in value:
            url = first_url(item, base_url)
            if url:
                return url
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "@id", "target"):
            url = first_url(value.get(key), base_url)
            if url:
                return url
    return ""

def json_ld_events(soup):
    """Return Event objects exposed by the listing's JSON-LD, if any."""
    def walk(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                if isinstance(child, (dict, list)):
                    yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    events = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        for node in walk(data):
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if any(str(item).lower().endswith("event") for item in types):
                events.append(node)
    return events

def display_name(value):
    if isinstance(value, str):
        return clean_html(value)
    if isinstance(value, list):
        return ", ".join(filter(None, (display_name(item) for item in value)))
    if isinstance(value, dict):
        return clean_html(value.get("name") or "")
    return ""

def display_location(value):
    if isinstance(value, str):
        return clean_html(value)
    if not isinstance(value, dict):
        return ""
    parts = [clean_html(value.get("name") or "")]
    address = value.get("address")
    if isinstance(address, str):
        parts.append(clean_html(address))
    elif isinstance(address, dict):
        parts.extend(clean_html(address.get(key) or "") for key in (
            "addressLocality", "addressRegion", "addressCountry"
        ))
    return ", ".join(dict.fromkeys(part for part in parts if part))

def display_date(value):
    value = clean_html(value)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%d %b %Y")
    except ValueError:
        return value

def normal_label(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

def labeled_value(soup, labels):
    """Read explicit label/value pairs only; never infer facts from prose."""
    lines = [
        clean_html(line)
        for line in soup.get_text("\n", strip=True).splitlines()
    ]
    lines = [line for line in lines if line]
    normalized_labels = {normal_label(label) for label in labels}
    for index, line in enumerate(lines):
        for label in labels:
            match = re.match(
                rf"^{re.escape(label)}\s*[:\-–—]\s*(.+)$",
                line, flags=re.IGNORECASE
            )
            if match:
                return clean_html(match.group(1))
            if normal_label(line) == normal_label(label):
                for candidate in lines[index + 1:index + 3]:
                    if normal_label(candidate) not in normalized_labels:
                        return candidate
    return ""

def attendance_mode(value):
    value = clean_html(value)
    low = value.lower()
    if "mixed" in low or ("online" in low and "offline" in low):
        return "Online + Offline"
    if "online" in low or "virtual" in low:
        return "Online"
    if "offline" in low or "physical" in low:
        return "Offline"
    return value

def registration_url(soup, event, base_url):
    for key in ("offers", "potentialAction", "registrationUrl"):
        url = first_url(event.get(key), base_url)
        if url:
            return url

    action_words = ("register", "registration", "apply", "join", "participate")
    rejected_words = ("login", "sign in", "privacy", "terms", "cookie")
    for anchor in soup.find_all("a", href=True):
        label = clean_html(
            " ".join(str(value) for value in (
                anchor.get_text(" ", strip=True),
                anchor.get("aria-label"),
                anchor.get("title"),
            ) if value)
        ).lower()
        if any(word in label for word in action_words) and not any(
            word in label for word in rejected_words
        ):
            url = clean_url(anchor["href"], base_url)
            if url:
                return url
    return ""

def cleaned_description(value, title, source):
    value = strip_source_suffix(value, source)
    clean_title = strip_source_suffix(title, source)
    if normal_label(value) == normal_label(clean_title):
        return ""
    if normal_label(clean_title) in normal_label(value) and len(value) < len(clean_title) + 60:
        return ""
    return clip(value, 850)

def extract_event_details(page_html, page_url, fallback_title, fallback_summary,
                          source_name, source_url, published):
    """Extract details which the official listing actually exposes."""
    soup = BeautifulSoup(page_html, "html.parser")
    events = json_ld_events(soup)
    event = events[0] if events else {}

    og_title = meta_content(soup, "og:title", "twitter:title")
    og_description = meta_content(
        soup, "og:description", "twitter:description", "description"
    )
    document_title = clean_html(soup.title.string) if soup.title and soup.title.string else ""
    title = clean_html(event.get("name") or og_title or document_title or fallback_title)
    title = strip_source_suffix(title, source_name)
    description = cleaned_description(
        event.get("description") or og_description or fallback_summary,
        title, source_name
    )

    canonical = ""
    canonical_tag = soup.find(
        "link", rel=lambda rel: rel and "canonical" in rel
    )
    if canonical_tag:
        canonical = clean_url(canonical_tag.get("href"), page_url)
    canonical = canonical or meta_url(soup, page_url, "og:url")
    event_url = canonical if trusted_domain(domain_of(canonical)) else page_url

    keywords = event.get("keywords", "")
    if isinstance(keywords, list):
        keywords = ", ".join(clean_html(item) for item in keywords)
    details = {
        "title": title or "India hackathon announcement",
        "description": description,
        "source": clean_html(source_name) or domain_of(source_url) or "Event listing",
        "source_url": clean_url(source_url),
        "url": clean_url(event_url) or clean_url(source_url),
        "image": first_url(event.get("image"), page_url)
                 or meta_url(soup, page_url, "og:image", "twitter:image"),
        "deadline": labeled_value(soup, (
            "Registration Deadline", "Registration closes", "Registration end date",
            "Last date to register", "Apply by", "Application deadline", "Deadline"
        )),
        "event_date": display_date(event.get("startDate")),
        "location": display_location(event.get("location"))
                    or labeled_value(soup, ("Location", "Venue", "City")),
        "mode": attendance_mode(event.get("eventAttendanceMode"))
                or labeled_value(soup, ("Event mode", "Participation mode", "Mode", "Format")),
        "categories": clean_html(keywords)
                      or labeled_value(soup, ("Categories", "Category", "Themes", "Tracks", "Domains")),
        "organizer": display_name(event.get("organizer"))
                     or labeled_value(soup, ("Organizer", "Organised by", "Organized by", "Hosted by")),
        "registration_url": registration_url(soup, event, event_url),
        "published": published,
        "page_parsed": True,
    }
    return details

def fallback_event(title, summary, source, source_url, published):
    title = strip_source_suffix(title, source)
    return {
        "title": title or "India hackathon announcement",
        "description": cleaned_description(summary, title, source),
        "source": clean_html(source) or domain_of(source_url) or "Event listing",
        "source_url": clean_url(source_url),
        "url": clean_url(source_url),
        "image": "",
        "deadline": "",
        "event_date": "",
        "location": "",
        "mode": "",
        "categories": "",
        "organizer": "",
        "registration_url": "",
        "published": published,
        "page_parsed": False,
    }

def discord_link(label, url):
    # Do not use angle brackets here: clean_html() deliberately strips HTML-like tags
    # while enforcing Discord's field length limits.
    return f"[{label}]({url.replace(')', '%29')})"

def make_embed(event):
    """Build a Discord-native rich alert without inventing event facts."""
    title = clip(event.get("title"), 250) or "India hackathon announcement"
    event_url = clean_url(event.get("url")) or clean_url(event.get("source_url"))
    description = clip(event.get("description"), 850)
    description = (
        f"*{description}*" if description
        else "*Open the listing below to review the latest event details.*"
    )
    fields = []

    def add_field(name, value, inline=True):
        value = clip(value, 1024)
        if value:
            fields.append({"name": name, "value": value, "inline": inline})

    add_field("📅 Registration deadline", event.get("deadline"))
    add_field("🗓️ Event date", event.get("event_date"))
    add_field("📍 Location", event.get("location"))
    add_field("💻 Mode", event.get("mode"))
    add_field("🏆 Categories", event.get("categories"))
    add_field("🏢 Organizer", event.get("organizer"))

    registration = clean_url(event.get("registration_url"))
    if registration:
        add_field("🔗 Registration link", discord_link("Open registration", registration), False)
    elif event_url:
        add_field("🔗 Event page", discord_link("Open event details", event_url), False)

    source = clip(event.get("source"), 100) or "Event listing"
    source_link = clean_url(event.get("source_url")) or event_url
    if event.get("page_parsed"):
        review = f"Listed on **{source}** (trusted platform)."
    else:
        review = (
            f"Discovery result from **{source}**. "
            "The listing did not expose structured event details."
        )
    if source_link:
        review += "\n" + discord_link("Open source listing", source_link)
    add_field("✅ Source & review", review, False)

    embed = {
        "author": {"name": "India Hackathon Alerts"},
        "title": f"🇮🇳 {title}",
        "description": description,
        "color": 0x57F287,
        "fields": fields[:25],
        "footer": {
            "text": (
                "K-Prep's Hackathon Bot · Verify eligibility, dates, "
                "and organizer before registering"
            )
        },
    }
    if event_url:
        embed["url"] = event_url
    image = clean_url(event.get("image"))
    if image:
        embed["thumbnail"] = {"url": image}
    published = event.get("published")
    if isinstance(published, datetime):
        embed["timestamp"] = published.astimezone(timezone.utc).isoformat()

    return {
        "username": "K-Prep's Hackathon Bot",
        "content": "📣 **New Hackathon Alert!**\n🔥 Don't miss this opportunity!",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

def main():
    if not WEBHOOK:
        raise RuntimeError("DISCORD_WEBHOOK_URL is required.")

    seen = load_seen()
    found = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    for query in QUERIES:
        rss_url = (
            "https://news.google.com/rss/search?q=" + quote_plus(query) +
            "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        feed = feedparser.parse(rss_url)
        for entry in feed.entries:
            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", ""))
            source_obj = entry.get("source", {})
            source_name = clean_html(source_obj.get("title", "Unknown source"))
            source_url = clean_url(source_obj.get("href", ""))
            source_domain = domain_of(source_url)
            combined = f"{title} {summary} {source_name}"

            if not title or not is_hackathon(combined):
                continue
            if not trusted_domain(source_domain):
                continue
            if not is_india_event(combined):
                continue

            published = published_datetime(entry)
            if published and published < cutoff:
                continue

            article_url = resolve_article_url(entry.get("link", ""))
            final_domain = domain_of(article_url)
            # Require the resolved article to remain on an approved publisher domain.
            if final_domain and not trusted_domain(final_domain):
                # Google News may not expose a direct publisher URL; keep the original
                # source listing link only if its declared publisher is trusted.
                article_url = source_url

            key = hashlib.sha256((title.lower() + "|" + article_url).encode()).hexdigest()
            if key in seen:
                continue
            found.append((key, title, summary, source_name, article_url, published))

    # Deduplicate across overlapping search queries.
    unique = {}
    for item in found:
        unique[item[0]] = item
    items = sorted(unique.values(), key=lambda x: x[5] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    posted = 0
    inspected = 0
    for key, title, summary, source, url, published in items:
        if posted >= MAX_POSTS_PER_RUN or inspected >= MAX_CANDIDATES_PER_RUN:
            break
        inspected += 1
        # Google News supplies only a short teaser. Fetch the approved event page
        # so dates, location, registration links, and images can be displayed when
        # the platform exposes them.
        page_html = ""
        page_url = url
        if urlparse(url).path.strip("/"):
            page_html, page_url = fetch_event_page(url)

        if page_html:
            event = extract_event_details(
                page_html, page_url, title, summary, source, url, published
            )
        else:
            event = fallback_event(title, summary, source, url, published)

        payload = make_embed(event)
        response = requests.post(WEBHOOK, json=payload, timeout=20)
        response.raise_for_status()
        seen.add(key)
        posted += 1
        time.sleep(1.2)  # Respect Discord webhook rate limits.

    save_seen(seen)
    print(
        f"New announcements posted: {posted}; new candidates: {len(items)}; "
        f"inspected: {inspected}"
    )

if __name__ == "__main__":
    main()
