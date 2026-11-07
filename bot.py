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
from gnews_decoder import decode_many

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
BOT_NAME = "K-Prep's Hackathon Bot"
STATE_FILE = "seen.json"
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "15"))
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "45"))
GOOGLE_NEWS_SOCS_COOKIE = os.getenv("GOOGLE_NEWS_SOCS_COOKIE", "").strip()

TRUSTED_DOMAINS = {
    "devfolio.co", "unstop.com", "hackerearth.com", "hack2skill.com",
    "gdg.community.dev", "developers.google.com", "sih.gov.in",
    "mygov.in", "aws.amazon.com", "devpost.com"
}

QUERIES = [
    "hackathon India registration",
    "hackathon India students online offline",
    "site:devfolio.co hackathon India",
    "site:unstop.com hackathon India",
    "site:hackerearth.com hackathon India",
    "site:hack2skill.com hackathon India",
    "site:gdg.community.dev hackathon India",
    "site:mygov.in hackathon India OR innovation challenge"
]

INDIA_SIGNALS = {
    "india", "indian", "bengaluru", "bangalore", "mumbai", "pune",
    "hyderabad", "chennai", "delhi", "new delhi", "kolkata", "bhubaneswar",
    "gurugram", "gurgaon", "noida", "jaipur", "ahmedabad", "surat",
    "lucknow", "kanpur", "indore", "bhopal", "kochi", "thiruvananthapuram",
    "visakhapatnam", "vizag", "nagpur", "patna", "ranchi", "guwahati",
    "chandigarh", "dehradun", "srinagar", "shimla", "goa", "west bengal",
    "odisha", "maharashtra", "karnataka", "telangana", "tamil nadu",
    "kerala", "rajasthan", "gujarat", "uttar pradesh", "madhya pradesh",
    "bihar", "jharkhand", "assam", "punjab", "haryana", "andhra pradesh",
    "uttarakhand", "himachal pradesh"
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; KPrepHackathonBot/1.0)"
})


class EventPageParser:
    """Small metadata parser for HTML event pages."""
    def __init__(self):
        from html.parser import HTMLParser

        class Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.meta = {}
                self.text_parts = []
                self.in_hidden = False

            def handle_starttag(inner_self, tag, attrs):
                attrs = dict(attrs)
                tag = tag.lower()
                if tag == "meta":
                    key = (attrs.get("property") or attrs.get("name") or "").lower()
                    value = (attrs.get("content") or "").strip()
                    if key and value:
                        inner_self.meta[key] = value
                elif tag in {"script", "style", "noscript"}:
                    inner_self.in_hidden = True

            def handle_endtag(inner_self, tag):
                if tag.lower() in {"script", "style", "noscript"}:
                    inner_self.in_hidden = False

            def handle_data(inner_self, data):
                if not inner_self.in_hidden:
                    value = re.sub(r"\s+", " ", html.unescape(data or "")).strip()
                    if value:
                        inner_self.text_parts.append(value)

        self.parser = Parser()

    def feed(self, source):
        self.parser.feed(source)
        return {
            "meta": self.parser.meta,
            "text": " ".join(self.parser.text_parts)
        }


def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def domain_of(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def trusted_domain(host):
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)


def is_specific_event_url(url):
    parsed = urlparse(url or "")
    path = parsed.path.rstrip("/").lower()
    if parsed.scheme not in {"http", "https"} or not trusted_domain(domain_of(url)):
        return False
    if not path or path in {
        "/", "/hackathons", "/events", "/challenges", "/competitions",
        "/search", "/discover", "/explore", "/login", "/signup"
    }:
        return False
    return True


def fetch_event_page(url):
    try:
        response = SESSION.get(url, timeout=20, allow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type:
            return response.url, None
        parsed = EventPageParser().feed(response.text[:2_500_000])
        return response.url, parsed
    except requests.RequestException as exc:
        print(f"Could not fetch event page ({type(exc).__name__}).")
        return url, None


def load_seen():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            value = json.load(file)
            return set(value if isinstance(value, list) else [])
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen)[-5000:], file, indent=2)


def published_datetime(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)


def is_india_event(text):
    low = (text or "").lower()
    return any(signal in low for signal in INDIA_SIGNALS)


def is_hackathon(text):
    low = (text or "").lower()
    return any(term in low for term in (
        "hackathon", "hack fest", "coding challenge", "innovation challenge"
    ))


def first_match(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;|-")
            if value and len(value) <= 140:
                return value
    return None


def extract_details(title, page, event_url):
    meta = page["meta"] if page else {}
    text = page["text"] if page else ""
    low = f"{title} {meta.get('og:description', '')} {meta.get('description', '')} {text}".lower()

    deadline = first_match([
        r"(?:registration|application)\s+deadline\s*[:\-–]?\s*([^.;\n]{3,90})",
        r"(?:registration|application)\s+(?:closes|ends|last date)\s*[:\-–]?\s*([^.;\n]{3,90})",
        r"(?:last date to register|register before|apply before)\s*[:\-–]?\s*([^.;\n]{3,90})"
    ], text)

    # Several event pages show a date first and then label it "Registration Deadline".
    if not deadline:
        deadline = first_match([
            r"(\d{1,2}\s+[A-Za-z]{3,9}(?:\s*['’]?\s*\d{2,4})?(?:,\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*(?:IST)?)?)\s+registration deadline"
        ], text)

    location = first_match([
        r"(?:venue|location|city)\s*[:\-–]\s*([^.;\n]{3,90})"
    ], text)

    if re.search(r"\bcompletely online\b|\bonline only\b|\bvirtual\b", low):
        mode = "Online"
    elif "hybrid" in low:
        mode = "Online + Offline"
    elif re.search(r"\boffline\b|\bin person\b|\bon-campus\b", low):
        mode = "Offline"
    else:
        mode = None

    category_map = [
        ("AI/ML", ("artificial intelligence", "machine learning", "ai/ml", "agentic ai")),
        ("Web3", ("web3", "blockchain")),
        ("Web Development", ("web development", "frontend", "backend", "full stack")),
        ("Cybersecurity", ("cybersecurity", "cyber security", "infosec")),
        ("IoT", ("internet of things", "iot")),
        ("FinTech", ("fintech", "financial technology")),
        ("HealthTech", ("healthtech", "health tech")),
        ("Sustainability", ("sustainability", "climate tech", "green tech"))
    ]
    categories = [
        label for label, terms in category_map
        if any(term in low for term in terms)
    ]

    organizer = first_match([
        r"(?:organizer|organiser|hosted by|organized by|organised by)\s*[:\-]\s*([^.;\n]{3,90})"
    ], text)

    image_url = (
        meta.get("og:image")
        or meta.get("twitter:image")
        or meta.get("twitter:image:src")
    )
    if image_url:
        image_url = urljoin(event_url, image_url)
        if urlparse(image_url).scheme not in {"http", "https"}:
            image_url = None
        # Avoid known generic platform/Google branding, not event imagery.
        image_low = image_url.lower()
        if any(word in image_low for word in ("google-news", "google_logo", "unstop_logo", "site-logo")):
            image_url = None

    description = (
        meta.get("og:description")
        or meta.get("description")
        or ""
    )
    description = clean_text(description)
    generic_descriptions = (
        "comprehensive up-to-date news coverage",
        "aggregated from sources all over the world by google news",
        "latest news"
    )
    if any(generic in description.lower() for generic in generic_descriptions):
        description = ""

    # Never use Google News RSS snippets as the event description.
    if not description:
        description = "Open the official event listing to read the full hackathon details."

    return {
        "deadline": deadline,
        "location": location,
        "mode": mode,
        "categories": ", ".join(categories[:4]) if categories else None,
        "organizer": organizer,
        "image_url": image_url,
        "description": description[:700]
    }


def make_payload(title, source, event_url, published, page):
    details = extract_details(title, page, event_url)
    published_text = published.strftime("%d %b %Y") if published else "Not available"

    embed = {
        "title": f"🇮🇳 {clean_text(title)[:230]}",
        "url": event_url,
        "description": f"*{details['description'][:500]}* 🚀",
        "color": 0x2ECC71,
        "fields": [
            {"name": "📅 Registration Deadline", "value": details["deadline"] or "See event page", "inline": True},
            {"name": "📍 Location", "value": details["location"] or "See event page", "inline": True},
            {"name": "💻 Mode", "value": details["mode"] or "See event page", "inline": True},
            {"name": "🏆 Categories", "value": details["categories"] or "See event page", "inline": True},
            {"name": "🏢 Organizer", "value": details["organizer"] or "See event page", "inline": True},
            {"name": "🔗 Registration Link", "value": f"[Register / View Event]({event_url})", "inline": False},
            {
                "name": "🛡️ Source Status",
                "value": f"✅ Individual event page resolved from **{source}**. Organizer identity is not independently audited.",
                "inline": False
            },
            {"name": "💡 About the Hackathon", "value": details["description"][:700], "inline": False}
        ],
        "footer": {
            "text": f"Powered by {BOT_NAME} • India Hackathon Updates • Listing published {published_text}"
        }
    }

    # The reference layout uses a poster at the top-right. Use the real event
    # page's Open Graph/Twitter poster only; never use the Google News logo.
    if details["image_url"]:
        embed["thumbnail"] = {"url": details["image_url"]}

    return {
        "username": BOT_NAME,
        "content": "📢 **New Hackathon Alert!** 🇮🇳",
        "embeds": [embed],
        "allowed_mentions": {"parse": []}
    }


def decode_google_news_links(urls):
    kwargs = {"concurrency": 3, "timeout": 20.0}
    if GOOGLE_NEWS_SOCS_COOKIE:
        kwargs["consent_cookie"] = GOOGLE_NEWS_SOCS_COOKIE
    try:
        return decode_many(urls, **kwargs)
    except Exception as exc:
        raise RuntimeError(
            f"Google News URL resolution failed ({type(exc).__name__}). "
            "No Google News wrapper links will be posted."
        ) from exc


def main():
    # Check webhook target and name; never print the webhook token.
    webhook_response = SESSION.get(WEBHOOK, timeout=20)
    webhook_response.raise_for_status()
    webhook_info = webhook_response.json()
    print(
        "Discord webhook destination: "
        f"name={webhook_info.get('name')!r}, "
        f"channel_id={webhook_info.get('channel_id')!r}"
    )

    seen = load_seen()
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    candidates = {}

    for query in QUERIES:
        feed_url = (
            "https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            title = clean_text(entry.get("title", ""))
            source_obj = entry.get("source", {})
            source_name = clean_text(source_obj.get("title", "Unknown source"))
            source_url = source_obj.get("href", "")
            combined = f"{title} {source_name}"

            if not title or not is_hackathon(combined):
                continue
            if not trusted_domain(domain_of(source_url)):
                continue
            if not is_india_event(combined):
                continue

            published = published_datetime(entry)
            if published and published < cutoff:
                continue

            google_url = entry.get("link", "").strip()
            if not google_url:
                continue

            candidates[google_url] = {
                "title": title,
                "source": source_name,
                "published": published
            }

    print(f"Discovery candidates before URL resolution: {len(candidates)}")
    decoded = decode_google_news_links(list(candidates.keys()))

    ready = []
    unresolved = 0
    for google_url, item in candidates.items():
        event_url = decoded.get(google_url)
        if not event_url:
            unresolved += 1
            continue

        if not is_specific_event_url(event_url):
            unresolved += 1
            continue

        final_url, page = fetch_event_page(event_url)
        if not is_specific_event_url(final_url):
            unresolved += 1
            continue

        # Use the direct, resolved event URL and real event page metadata only.
        clean_title = item["title"]
        suffix = f" - {item['source']}"
        if clean_title.lower().endswith(suffix.lower()):
            clean_title = clean_title[:-len(suffix)].strip(" -")

        key = hashlib.sha256(
            (clean_title.lower() + "|" + final_url).encode("utf-8")
        ).hexdigest()
        if key in seen:
            continue

        ready.append((
            key, clean_title, item["source"], final_url,
            item["published"], page
        ))

    print(f"Resolved individual event pages: {len(ready)}")
    print(f"Skipped unresolved/non-event links: {unresolved}")

    ready.sort(
        key=lambda item: item[4] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )

    posted = 0
    for key, title, source, event_url, published, page in ready[:MAX_POSTS_PER_RUN]:
        payload = make_payload(title, source, event_url, published, page)
        separator = "&" if "?" in WEBHOOK else "?"
        response = SESSION.post(
            f"{WEBHOOK}{separator}wait=true",
            json=payload,
            timeout=25
        )
        response.raise_for_status()
        message = response.json()

        message_id = message.get("id")
        channel_id = message.get("channel_id")
        if not message_id or not channel_id:
            raise RuntimeError("Discord did not confirm a saved message ID and channel ID.")

        poster_attached = bool(payload["embeds"][0].get("thumbnail"))
        print(
            f"Discord confirmed event: {title[:90]!r}; "
            f"channel_id={channel_id}; poster_attached={poster_attached}"
        )
        seen.add(key)
        posted += 1
        time.sleep(1.2)

    save_seen(seen)
    print(
        f"Discord-confirmed announcements: {posted}; "
        f"resolved new event pages: {len(ready)}; "
        f"unresolved links skipped: {unresolved}"
    )


if __name__ == "__main__":
    main()
