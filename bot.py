import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = "seen.json"
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "15"))
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "45"))

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
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()

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
        r = requests.get(url, timeout=15, allow_redirects=True,
                         headers={"User-Agent": "IndiaHackathonAlertBot/1.0"})
        return r.url
    except requests.RequestException:
        return url

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

def make_embed(title, summary, source, url, published):
    # Discord webhook embed payload; do not claim organizer verification.
    fields = [
        {"name": "Source", "value": source[:100], "inline": True},
        {"name": "Review status", "value": "Source checked · Organizer not independently verified", "inline": False},
    ]
    if published:
        fields.append({"name": "Published", "value": published.strftime("%d %b %Y"), "inline": True})
    return {
        "embeds": [{
            "title": title[:250] or "India hackathon announcement",
            "url": url,
            "description": (summary or "Open the source listing to review event details.")[:900],
            "color": 0x2ECC71,
            "fields": fields,
            "footer": {"text": "K-Prep's Hackathon Bot · Check eligibility, dates and organizer before registering"}
        }],
        "allowed_mentions": {"parse": []}
    }

def main():
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
            source_url = source_obj.get("href", "")
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

            

            original_article_url = entry.get("link", "").strip()

            if not original_article_url:
                continue

            resolved_article_url = resolve_article_url(original_article_url)
            resolved_domain = domain_of(resolved_article_url)
            resolved_path = urlparse(resolved_article_url).path.strip("/")

            if (
                resolved_domain
                and trusted_domain(resolved_domain)
                and resolved_path
            ):
                article_url = resolved_article_url
            else:
                article_url = original_article_url

            key = hashlib.sha256(
                (title.lower() + "|" + article_url).encode()
            ).hexdigest()

            if key in seen:
                continue

            found.append(
                (key, title, summary, source_name, article_url, published)
            )


    # Deduplicate across overlapping search queries.
    unique = {}
    for item in found:
        unique[item[0]] = item
    items = sorted(unique.values(), key=lambda x: x[5] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    posted = 0
    for key, title, summary, source, url, published in items:
        if posted >= MAX_POSTS_PER_RUN:
            break
        payload = make_embed(title, summary, source, url, published)
        response = requests.post(WEBHOOK, json=payload, timeout=20)
        response.raise_for_status()
        seen.add(key)
        posted += 1
        time.sleep(1.2)  # Respect Discord webhook rate limits.

    save_seen(seen)
    print(f"New announcements posted: {posted}; new candidates: {len(items)}")

if __name__ == "__main__":
    main()
