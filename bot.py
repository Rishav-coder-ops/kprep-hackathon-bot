import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
BOT_NAME = "K-Prep's Hackathon Bot"
STATE_FILE = "seen.json"
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "15"))
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "45"))

# Publisher allowlist for discovery. This does not independently certify events.
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

GENERIC_PATHS = {
    "", "/", "/hackathons", "/events", "/challenges", "/competitions",
    "/search", "/discover", "/explore", "/login", "/signup"
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "KPrepHackathonBot/1.0 (Discord hackathon announcements)"
})


class PageParser(HTMLParser):
    """Collect Open Graph metadata, links and readable text from an event page."""

    def __init__(self):
        super().__init__()
        self.meta = {}
        self.links = []
        self.text_parts = []
        self.in_hidden = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            value = (attrs.get("content") or "").strip()
            if key and value:
                self.meta[key] = value
        elif tag == "a":
            href = attrs.get("href")
            if href:
                self.links.append(href)
        elif tag in {"script", "style", "noscript"}:
            self.in_hidden = True

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript"}:
            self.in_hidden = False

    def handle_data(self, data):
        if not self.in_hidden:
            text = re.sub(r"\s+", " ", html.unescape(data or "")).strip()
            if text:
                self.text_parts.append(text)


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


def is_specific_event_page(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    if parsed.scheme not in {"http", "https"} or not trusted_domain(domain_of(url)):
        return False
    if path.lower() in GENERIC_PATHS:
        return False
    parts = [part for part in path.split("/") if part]
    return bool(parts and not (len(parts) == 1 and parts[0].lower() in {
        "hackathons", "events", "challenges", "competitions", "community"
    }))


def fetch_page(url):
    try:
        response = SESSION.get(url, timeout=18, allow_redirects=True)
        response.raise_for_status()
        if "html" not in response.headers.get("content-type", "").lower():
            return response.url, None
        parser = PageParser()
        parser.feed(response.text[:2_000_000])
        return response.url, {
            "meta": parser.meta,
            "links": parser.links,
            "text": " ".join(parser.text_parts)
        }
    except requests.RequestException as exc:
        print(f"Page fetch failed ({type(exc).__name__})")
        return url, None


def resolve_event_page(rss_url):
    """Resolve to a specific event page; never replace it with a platform homepage."""
    final_url, page = fetch_page(rss_url)

    if is_specific_event_page(final_url):
        return final_url, page, True

    if page:
        article_links = []
        for href in page["links"]:
            candidate = urljoin(final_url, href)
            if is_specific_event_page(candidate):
                path = urlparse(candidate).path.lower()
                score = 1
                if any(word in path for word in ("hackathon", "event", "challenge", "competition")):
                    score += 4
                if any(word in path for word in ("register", "registration")):
                    score += 2
                article_links.append((score, candidate))
        if article_links:
            article_links.sort(key=lambda item: item[0], reverse=True)
            event_url, event_page = fetch_page(article_links[0][1])
            if is_specific_event_page(event_url):
                return event_url, event_page, True
            return article_links[0][1], event_page, True

    # If we only have a publisher/news article, use that as an explicitly
    # unconfirmed source result, never the publisher homepage.
    if final_url and domain_of(final_url) != "news.google.com":
        return final_url, page, False
    return rss_url, page, False


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
    lower = (text or "").lower()
    return any(signal in lower for signal in INDIA_SIGNALS)


def is_hackathon(text):
    lower = (text or "").lower()
    return any(term in lower for term in (
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


def event_details(page, title, summary, event_url):
    text = page["text"] if page else ""
    meta = page["meta"] if page else {}
    combined = f"{title} {summary} {text}".lower()

    deadline = first_match([
        r"(?:registration|application)\s+(?:deadline|closes|ends|last date)\s*[:\-–]?\s*([^.;\n]{4,80})",
        r"(?:last date to register|register before|apply before)\s*[:\-–]?\s*([^.;\n]{4,80})"
    ], text)

    location = first_match([
        r"(?:venue|location|city)\s*[:\-–]\s*([^.;\n]{3,80})"
    ], text)

    if "hybrid" in combined:
        mode = "Online + Offline"
    elif re.search(r"\bonline\b|\bvirtual\b", combined):
        mode = "Online"
    elif re.search(r"\boffline\b|\bin person\b|\bon-campus\b", combined):
        mode = "Offline"
    else:
        mode = None

    category_map = [
        ("AI/ML", ("artificial intelligence", "machine learning", "ai/ml")),
        ("Web3", ("web3", "blockchain")),
        ("Web Development", ("web development", "frontend", "backend")),
        ("Cybersecurity", ("cybersecurity", "cyber security", "infosec")),
        ("IoT", ("internet of things", "iot")),
        ("FinTech", ("fintech", "financial technology")),
        ("HealthTech", ("healthtech", "health tech")),
        ("Sustainability", ("sustainability", "climate tech", "green tech"))
    ]
    categories = [
        label for label, terms in category_map
        if any(term in combined for term in terms)
    ]

    organizer = first_match([
        r"(?:organizer|organiser|hosted by|organized by|organised by)\s*[:\-]\s*([^.;\n]{3,80})"
    ], text)

    # Use the event page's real poster if it exposes an Open Graph/Twitter image.
    image_url = (
        meta.get("og:image")
        or meta.get("twitter:image")
        or meta.get("twitter:image:src")
    )
    if image_url:
        image_url = urljoin(event_url, image_url)
        if urlparse(image_url).scheme not in {"http", "https"}:
            image_url = None

    description = (
        meta.get("og:description")
        or meta.get("description")
        or summary
        or "Open the event page to review the full hackathon details."
    )

    return {
        "deadline": deadline,
        "location": location,
        "mode": mode,
        "categories": ", ".join(categories[:4]) if categories else None,
        "organizer": organizer,
        "image_url": image_url,
        "description": clean_text(description)[:700]
    }


def make_payload(title, summary, source, event_url, published, page, event_confirmed):
    details = event_details(page, title, summary, event_url)
    published_text = published.strftime("%d %b %Y") if published else "Not available"

    if event_confirmed:
        status = (
            f"✅ Individual event page found on **{source}**. "
            "Organizer identity has not been independently audited."
        )
        link_label = "Open individual event page"
    else:
        status = (
            f"⚠️ Source result from **{source}**. A direct event page was not confirmed; "
            "please check the destination before registering."
        )
        link_label = "Open original source result"

    def field_value(value):
        return value if value else "Check event page"

    embed = {
        "title": f"🇮🇳 {clean_text(title)[:230]}",
        "url": event_url,
        "description": (
            f"*{details['description'][:500]}*"
            if details["description"] else
            "*Explore this hackathon opportunity!* 🚀"
        ),
        "color": 0x2ECC71,
        "fields": [
            {
                "name": "📅 Registration Deadline",
                "value": field_value(details["deadline"]),
                "inline": True
            },
            {
                "name": "📍 Location",
                "value": field_value(details["location"]),
                "inline": True
            },
            {
                "name": "💻 Mode",
                "value": field_value(details["mode"]),
                "inline": True
            },
            {
                "name": "🏆 Categories",
                "value": field_value(details["categories"]),
                "inline": True
            },
            {
                "name": "🏢 Organizer",
                "value": field_value(details["organizer"]),
                "inline": True
            },
            {
                "name": "🔗 Registration Link",
                "value": f"[{link_label}]({event_url})",
                "inline": False
            },
            {
                "name": "🛡️ Source Status",
                "value": status,
                "inline": False
            },
            {
                "name": "💡 About the Hackathon",
                "value": details["description"][:700] or "Open the event page for more information.",
                "inline": False
            }
        ],
        "footer": {
            "text": (
                f"Powered by {BOT_NAME}  •  India Hackathon Updates  •  "
                f"Listing published {published_text}"
            )
        }
    }

    # Discord's thumbnail renders on the right side, matching the reference card.
    if details["image_url"]:
        embed["thumbnail"] = {"url": details["image_url"]}

    return {
        "username": BOT_NAME,
        "content": "📢 **New Hackathon Alert!** 🇮🇳",
        "embeds": [embed],
        "allowed_mentions": {"parse": []}
    }


def main():
    # Confirm webhook destination in logs without exposing the token.
    webhook_response = SESSION.get(WEBHOOK, timeout=20)
    webhook_response.raise_for_status()
    webhook_info = webhook_response.json()
    print(
        "Discord webhook destination: "
        f"name={webhook_info.get('name')!r}, "
        f"channel_id={webhook_info.get('channel_id')!r}"
    )

    seen = load_seen()
    found = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    for query in QUERIES:
        rss_url = (
            "https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        feed = feedparser.parse(rss_url)

        for entry in feed.entries:
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", ""))
            source_obj = entry.get("source", {})
            source_name = clean_text(source_obj.get("title", "Unknown source"))
            source_url = source_obj.get("href", "")
            combined = f"{title} {summary} {source_name}"

            if not title or not is_hackathon(combined):
                continue
            if not trusted_domain(domain_of(source_url)):
                continue
            if not is_india_event(combined):
                continue

            published = published_datetime(entry)
            if published and published < cutoff:
                continue

            rss_link = entry.get("link", "").strip()
            if not rss_link:
                continue

            event_url, page, event_confirmed = resolve_event_page(rss_link)
            if not event_url:
                continue

            key = hashlib.sha256(
                (title.lower() + "|" + event_url).encode("utf-8")
            ).hexdigest()
            if key in seen:
                continue

            found.append((
                key, title, summary, source_name, event_url,
                published, page, event_confirmed
            ))

    unique = {item[0]: item for item in found}
    items = sorted(
        unique.values(),
        key=lambda item: item[5] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )

    posted = 0
    for item in items:
        if posted >= MAX_POSTS_PER_RUN:
            break

        key, title, summary, source, event_url, published, page, confirmed = item
        payload = make_payload(
            title, summary, source, event_url, published, page, confirmed
        )

        sep = "&" if "?" in WEBHOOK else "?"
        response = SESSION.post(
            f"{WEBHOOK}{sep}wait=true",
            json=payload,
            timeout=25
        )
        response.raise_for_status()
        message = response.json()

        message_id = message.get("id")
        channel_id = message.get("channel_id")
        if not message_id or not channel_id:
            raise RuntimeError(
                "Discord did not return a saved message ID and channel ID."
            )

        print(
            "Discord confirmed saved message: "
            f"id={message_id}, channel_id={channel_id}, "
            f"event_page_confirmed={confirmed}, "
            f"poster_attached={bool(payload['embeds'][0].get('thumbnail'))}"
        )
        seen.add(key)
        posted += 1
        time.sleep(1.2)

    save_seen(seen)
    print(
        f"Discord-confirmed announcements: {posted}; "
        f"new candidates: {len(items)}"
    )


if __name__ == "__main__":
    main()
