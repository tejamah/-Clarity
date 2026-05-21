from collections import Counter
from datetime import datetime, timezone
from html import unescape
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import requests


BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend" / "frontend-react" / "public"
FEED_ITEM_LIMIT = 35
CATEGORY_ARTICLE_LIMIT = 100
GOOGLE_CUSTOM_SEARCH_REFRESH_SECONDS = 60 * 60 * 4
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "")

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
CORS(app)

CATEGORIES = {
    "top": [
        "https://feeds.npr.org/1001/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    ],
    "technology": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
        "https://www.wired.com/feed/rss",
        "https://techcrunch.com/feed/",
        "https://news.google.com/rss/search?q=technology&hl=en-US&gl=US&ceid=US:en",
    ],
    "business": [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.npr.org/rss/rss.php?id=1006",
        "https://news.google.com/rss/search?q=business&hl=en-US&gl=US&ceid=US:en",
    ],
    "sports": [
        "https://www.espn.com/espn/rss/news",
        "https://rss.nytimes.com/services/xml/rss/nyt/Sports.xml",
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://www.cbssports.com/rss/headlines/",
        "https://news.google.com/rss/search?q=sports&hl=en-US&gl=US&ceid=US:en",
    ],
    "entertainment": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Arts.xml",
        "https://www.npr.org/rss/rss.php?id=1048",
        "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
        "https://www.billboard.com/feed/",
        "https://news.google.com/rss/search?q=entertainment&hl=en-US&gl=US&ceid=US:en",
    ],
}

STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "because",
    "been",
    "before",
    "but",
    "can",
    "could",
    "for",
    "from",
    "has",
    "have",
    "her",
    "his",
    "how",
    "into",
    "its",
    "more",
    "new",
    "not",
    "our",
    "out",
    "over",
    "said",
    "she",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "they",
    "this",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "with",
    "you",
    "your",
}

news_cache = {
    "categories": list(CATEGORIES),
    "articles": {},
    "updated_at": None,
    "sources": {"google_custom_search": bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)},
}
google_search_cache = {}
scheduler = None


def strip_html(value):
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def child_text(item, tag_name):
    for child in item:
        if child.tag.split("}")[-1].lower() == tag_name.lower():
            return child.text or ""
    return ""


def child_attr(item, tag_name, attr_name):
    for child in item:
        if child.tag.split("}")[-1].lower() == tag_name.lower():
            return child.attrib.get(attr_name, "")
    return ""


def feed_nodes(root):
    rss_items = root.findall(".//item")
    if rss_items:
        return rss_items
    return [node for node in root.iter() if node.tag.split("}")[-1].lower() == "entry"]


def summarize_text(text, title=""):
    clean_text = strip_html(text)
    if not clean_text:
        return title

    sentences = re.split(r"(?<=[.!?])\s+", clean_text)
    sentences = [sentence.strip() for sentence in sentences if len(sentence.strip()) > 30]
    if len(sentences) <= 2:
        return clean_text[:280]

    words = re.findall(r"[a-zA-Z]{3,}", f"{title} {clean_text}".lower())
    keywords = Counter(word for word in words if word not in STOP_WORDS)

    ranked = []
    for index, sentence in enumerate(sentences):
        sentence_words = re.findall(r"[a-zA-Z]{3,}", sentence.lower())
        score = sum(keywords[word] for word in sentence_words) / max(len(sentence_words), 1)
        ranked.append((score, index, sentence))

    chosen = sorted(sorted(ranked, reverse=True)[:2], key=lambda item: item[1])
    summary = " ".join(sentence for _, _, sentence in chosen)
    return summary[:360]


def parse_rss_feed(url, limit=FEED_ITEM_LIMIT):
    response = requests.get(
        url,
        timeout=10,
        headers={"User-Agent": "LiveSummarizedNews/1.0 (+free-rss-demo)"},
    )
    response.raise_for_status()

    root = ElementTree.fromstring(response.content)
    items = feed_nodes(root)
    articles = []

    for item in items[:limit]:
        title = strip_html(child_text(item, "title"))
        link = child_text(item, "link").strip() or child_attr(item, "link", "href")
        description = strip_html(
            child_text(item, "description") or child_text(item, "summary") or child_text(item, "content")
        )
        published = child_text(item, "pubDate").strip() or child_text(item, "updated").strip()
        image = (
            child_attr(item, "content", "url")
            or child_attr(item, "thumbnail", "url")
            or child_attr(item, "enclosure", "url")
        )

        if not title or not link:
            continue

        articles.append(
            {
                "title": title,
                "summary": summarize_text(description, title),
                "url": link,
                "image": image,
                "source": re.sub(r"^www\.", "", urlparse(url).netloc),
                "published": published,
            }
        )

    return articles


def google_query_for_category(category):
    if category == "top":
        return "latest news"
    return f"latest {category} news"


def google_article_image(item):
    pagemap = item.get("pagemap", {})
    cse_images = pagemap.get("cse_image", [])
    if cse_images:
        return cse_images[0].get("src", "")

    metatags = pagemap.get("metatags", [])
    if metatags:
        return metatags[0].get("og:image", "")

    return ""


def fetch_google_custom_search(category):
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return []

    cached = google_search_cache.get(category)
    now = datetime.now(timezone.utc)
    if cached and (now - cached["fetched_at"]).total_seconds() < GOOGLE_CUSTOM_SEARCH_REFRESH_SECONDS:
        return cached["articles"]

    response = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        timeout=10,
        params={
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CSE_ID,
            "q": google_query_for_category(category),
            "num": 10,
            "dateRestrict": "d1",
            "safe": "active",
        },
    )
    response.raise_for_status()

    articles = []
    for item in response.json().get("items", []):
        title = strip_html(item.get("title", ""))
        link = item.get("link", "").strip()
        if not title or not link:
            continue

        articles.append(
            {
                "title": title,
                "summary": summarize_text(item.get("snippet", ""), title),
                "url": link,
                "image": google_article_image(item),
                "source": re.sub(r"^www\.", "", urlparse(link).netloc),
                "published": "Google API",
            }
        )

    google_search_cache[category] = {"articles": articles, "fetched_at": now}
    return articles


def fetch_and_summarize_news():
    next_articles = {}

    for category, feeds in CATEGORIES.items():
        seen_urls = set()
        articles = []

        try:
            for article in fetch_google_custom_search(category):
                seen_urls.add(article["url"])
                articles.append(article)
        except Exception as error:
            print(f"Error fetching Google Custom Search for {category}: {error}")

        for feed_url in feeds:
            try:
                for article in parse_rss_feed(feed_url):
                    if article["url"] in seen_urls:
                        continue
                    seen_urls.add(article["url"])
                    articles.append(article)
            except Exception as error:
                print(f"Error fetching {feed_url}: {error}")

        next_articles[category] = articles[:CATEGORY_ARTICLE_LIMIT]

    news_cache["articles"] = next_articles
    news_cache["updated_at"] = datetime.now(timezone.utc).isoformat()


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


def ensure_news_loaded():
    if not news_cache["articles"]:
        fetch_and_summarize_news()


def start_news_scheduler():
    global scheduler
    if scheduler and scheduler.running:
        return

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        fetch_and_summarize_news,
        "interval",
        minutes=2,
        id="refresh_news",
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()


@app.route("/api/news")
def get_all_news():
    ensure_news_loaded()
    return jsonify(news_cache)


@app.route("/api/news/<category>")
def get_news(category):
    ensure_news_loaded()
    return jsonify(news_cache["articles"].get(category, []))


if __name__ == "__main__":
    start_news_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
