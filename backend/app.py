from collections import Counter
from datetime import datetime, timezone
from html import unescape
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree
import hashlib

from dotenv import load_dotenv
import logging
from functools import lru_cache

try:
    import sentry_sdk
except Exception:
    sentry_sdk = None

try:
    from textblob import TextBlob
except Exception:
    TextBlob = None

try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
except Exception:
    nlp = None

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
import requests
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from flask_mail import Mail
except Exception:
    Mail = None


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend" / "frontend-react" / "public"
DATABASE_PATH = BASE_DIR / "live_news.db"
FEED_ITEM_LIMIT = 35
CATEGORY_ARTICLE_LIMIT = 100
GOOGLE_CUSTOM_SEARCH_REFRESH_SECONDS = 60 * 60 * 4
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "")
AUTH_RATE_LIMIT_WINDOW_SECONDS = 60
AUTH_RATE_LIMIT_MAX_ATTEMPTS = 10

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "").lower() == "true",
    MAIL_SERVER=os.getenv("MAIL_SERVER", ""),
    MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
    MAIL_USE_TLS=os.getenv("MAIL_USE_TLS", "true").lower() == "true",
    MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
)
mail = Mail(app) if Mail else None

# Configure CORS only when a separate frontend origin is used.
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
if FRONTEND_ORIGIN:
    CORS(app, origins=[FRONTEND_ORIGIN], supports_credentials=True)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Optional Sentry
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN and sentry_sdk:
    sentry_sdk.init(dsn=SENTRY_DSN)

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
auth_attempts = {}
scheduler = None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https: data:; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'",
    )
    return response


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf():
    token = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    return bool(token and expected and secrets.compare_digest(token, expected))


def rate_limit_key(action, email=""):
    ip_address = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    ip_address = ip_address.split(",")[0].strip()
    return f"{action}:{ip_address}:{normalize_email(email)}"


def rate_limited(action, email=""):
    now = time.monotonic()
    key = rate_limit_key(action, email)
    attempts = [
        timestamp
        for timestamp in auth_attempts.get(key, [])
        if now - timestamp < AUTH_RATE_LIMIT_WINDOW_SECONDS
    ]

    if len(attempts) >= AUTH_RATE_LIMIT_MAX_ATTEMPTS:
        auth_attempts[key] = attempts
        return True

    attempts.append(now)
    auth_attempts[key] = attempts
    return False


def get_db():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS summaries (
                url TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS article_cache (
                category TEXT PRIMARY KEY,
                articles_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                collection_id INTEGER,
                saved_at TEXT NOT NULL,
                UNIQUE(user_id, url),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(collection_id) REFERENCES collections(id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, name),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS article_annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                annotation TEXT NOT NULL,
                annotation_type TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS article_sentiment (
                url TEXT PRIMARY KEY,
                sentiment_score REAL,
                subjectivity REAL,
                tone TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS article_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                entity_text TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                extracted_at TEXT NOT NULL,
                FOREIGN KEY(url) REFERENCES summaries(url)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS article_trends (
                url TEXT PRIMARY KEY,
                trend_score REAL,
                appearance_count INTEGER,
                last_seen TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS article_clusters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster_hash TEXT UNIQUE,
                topic TEXT NOT NULL,
                urls_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS newsletters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                unsubscribe_token TEXT NOT NULL UNIQUE,
                subscribed_at TEXT NOT NULL,
                frequency TEXT DEFAULT 'daily'
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS digest_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                newsletter_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                articles_json TEXT NOT NULL,
                FOREIGN KEY(newsletter_id) REFERENCES newsletters(id)
            )
            """
        )


def public_user(row):
    if not row:
        return None

    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None

    with get_db() as db:
        row = db.execute(
            "SELECT id, name, email FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    return public_user(row)


def normalize_email(email):
    return (email or "").strip().lower()


def strip_html(value):
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def get_cached_summary(url):
    with get_db() as db:
        row = db.execute("SELECT summary FROM summaries WHERE url = ?", (url,)).fetchone()
        return row["summary"] if row else None


def set_cached_summary(url, summary_text):
    with get_db() as db:
        db.execute(
            "REPLACE INTO summaries (url, summary, updated_at) VALUES (?, ?, ?)",
            (url, summary_text, datetime.now(timezone.utc).isoformat()),
        )


def get_cached_articles(category):
    with get_db() as db:
        row = db.execute(
            "SELECT articles_json FROM article_cache WHERE category = ?",
            (category,),
        ).fetchone()

    if not row:
        return []

    try:
        return json.loads(row["articles_json"])
    except json.JSONDecodeError:
        return []


def set_cached_articles(category, articles):
    with get_db() as db:
        db.execute(
            "REPLACE INTO article_cache (category, articles_json, updated_at) VALUES (?, ?, ?)",
            (
                category,
                json.dumps(articles),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


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


def analyze_sentiment(text):
    """Analyze sentiment and subjectivity using TextBlob."""
    if not TextBlob or not text:
        return {"sentiment_score": 0, "subjectivity": 0.5, "tone": "neutral"}
    try:
        blob = TextBlob(text)
        polarity = blob.sentiment.polarity
        subjectivity = blob.sentiment.subjectivity
        if polarity > 0.1:
            tone = "positive"
        elif polarity < -0.1:
            tone = "negative"
        else:
            tone = "neutral"
        return {"sentiment_score": polarity, "subjectivity": subjectivity, "tone": tone}
    except Exception:
        return {"sentiment_score": 0, "subjectivity": 0.5, "tone": "neutral"}


def extract_entities(text):
    """Extract named entities (people, places, orgs) using spaCy."""
    if not nlp or not text:
        return []
    try:
        doc = nlp(text[:5000])  # limit to first 5000 chars
        entities = []
        for ent in doc.ents:
            if ent.label_ in ("PERSON", "GPE", "ORG"):
                entities.append({"text": ent.text, "type": ent.label_})
        return entities
    except Exception:
        return []


def calculate_reading_time(text):
    """Estimate reading time in minutes (avg 200 words per minute)."""
    if not text:
        return 1
    words = len(text.split())
    minutes = max(1, round(words / 200))
    return minutes


def calculate_trend_score(url):
    """Calculate trend score based on appearance count."""
    with get_db() as db:
        row = db.execute(
            "SELECT appearance_count FROM article_trends WHERE url = ?", (url,)
        ).fetchone()
    if not row:
        return 0
    return row["appearance_count"] / 100  # Normalize to 0-1 range


def topic_hash(title, summary):
    """Generate a hash for article clustering based on keywords."""
    keywords = re.findall(r"\b\w{4,}\b", (title + " " + summary).lower())
    keywords = [w for w in keywords if w not in STOP_WORDS][:5]  # Top 5 keywords
    return hashlib.md5(" ".join(sorted(keywords)).encode()).hexdigest()[:8]


def cluster_articles(articles):
    """Group articles by similar content (topic similarity)."""
    clusters = {}
    for article in articles:
        h = topic_hash(article.get("title", ""), article.get("summary", ""))
        if h not in clusters:
            clusters[h] = {
                "topic": article.get("title", "").split()[0],
                "urls": [],
            }
        clusters[h]["urls"].append(article["url"])
    return clusters


@lru_cache(maxsize=2048)
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

        # Use persistent cache for summaries when available to avoid recomputing
        cached = get_cached_summary(link)
        if cached:
            summary_text = cached
        else:
            summary_text = summarize_text(description, title)
            try:
                set_cached_summary(link, summary_text)
            except Exception:
                logger.exception("Failed to cache summary for %s", link)

        # Analyze sentiment
        sentiment_data = analyze_sentiment(description or title)
        
        # Extract entities
        entities = extract_entities(description or title)
        
        # Calculate reading time
        reading_time = calculate_reading_time(description)
        
        # Get trend score
        trend_score = calculate_trend_score(link)

        article = {
            "title": title,
            "summary": summary_text,
            "url": link,
            "image": image,
            "source": re.sub(r"^www\.", "", urlparse(url).netloc),
            "published": published,
            "reading_time": reading_time,
            "sentiment": sentiment_data.get("tone", "neutral"),
            "sentiment_score": sentiment_data.get("sentiment_score", 0),
            "entities": entities,
            "trending": trend_score > 0.3,
            "trend_score": trend_score,
        }
        
        articles.append(article)

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
    fetched_any_articles = False

    for category, feeds in CATEGORIES.items():
        seen_urls = set()
        articles = []

        try:
            for article in fetch_google_custom_search(category):
                seen_urls.add(article["url"])
                articles.append(article)
        except Exception as error:
            logger.warning("Error fetching Google Custom Search for %s: %s", category, error)

        for feed_url in feeds:
            try:
                for article in parse_rss_feed(feed_url):
                    if article["url"] in seen_urls:
                        continue
                    seen_urls.add(article["url"])
                    articles.append(article)
            except Exception as error:
                logger.warning("Error fetching %s: %s", feed_url, error)

        if articles:
            fetched_any_articles = True
            next_articles[category] = articles[:CATEGORY_ARTICLE_LIMIT]
            set_cached_articles(category, next_articles[category])
            continue

        cached_articles = news_cache["articles"].get(category) or get_cached_articles(category)
        next_articles[category] = cached_articles[:CATEGORY_ARTICLE_LIMIT]

    if fetched_any_articles or any(next_articles.values()):
        news_cache["articles"] = next_articles
        news_cache["updated_at"] = datetime.now(timezone.utc).isoformat()
        return

    logger.warning("No news articles loaded. Check network/DNS access to RSS hosts.")


@app.route("/")
def index():
    response = send_from_directory(FRONTEND_DIR, "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/index.html")
def index_html():
    response = send_from_directory(FRONTEND_DIR, "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(FRONTEND_DIR, "service-worker.js")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/manifest.json")
def manifest():
    return send_from_directory(FRONTEND_DIR, "manifest.json")


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


@app.route("/api/auth/me")
def auth_me():
    return jsonify({"user": current_user()})


@app.route("/api/auth/csrf")
def auth_csrf():
    return jsonify({"csrfToken": csrf_token()})


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    if not validate_csrf():
        return jsonify({"error": "Security token expired. Refresh and try again."}), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""

    if rate_limited("register", email):
        return jsonify({"error": "Too many attempts. Please wait a minute and try again."}), 429
    if len(name) < 2:
        return jsonify({"error": "Name must be at least 2 characters."}), 400
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO users (name, email, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    name,
                    email,
                    generate_password_hash(password),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({"error": "An account with that email already exists."}), 409

    session.clear()
    session["csrf_token"] = secrets.token_urlsafe(32)
    session["user_id"] = user_id
    return jsonify({"csrfToken": csrf_token(), "user": current_user()}), 201


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if not validate_csrf():
        return jsonify({"error": "Security token expired. Refresh and try again."}), 403

    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""

    if rate_limited("login", email):
        return jsonify({"error": "Too many attempts. Please wait a minute and try again."}), 429

    with get_db() as db:
        row = db.execute(
            "SELECT id, name, email, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid email or password."}), 401

    session.clear()
    session["csrf_token"] = secrets.token_urlsafe(32)
    session["user_id"] = row["id"]
    return jsonify({"csrfToken": csrf_token(), "user": public_user(row)})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    if not validate_csrf():
        return jsonify({"error": "Security token expired. Refresh and try again."}), 403

    session.clear()
    return jsonify({"csrfToken": csrf_token(), "user": None})


# Saved Articles & Collections
@app.route("/api/saved", methods=["GET"])
def get_saved_articles():
    user = current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    with get_db() as db:
        rows = db.execute(
            "SELECT url, title, saved_at, collection_id FROM saved_articles WHERE user_id = ? ORDER BY saved_at DESC",
            (user["id"],)
        ).fetchall()
    
    articles = [dict(row) for row in rows]
    return jsonify({"articles": articles})


@app.route("/api/saved", methods=["POST"])
def save_article():
    if not validate_csrf():
        return jsonify({"error": "Security token expired. Refresh and try again."}), 403

    user = current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    title = data.get("title", "").strip()
    collection_id = data.get("collection_id")
    
    if not url or not title:
        return jsonify({"error": "URL and title required"}), 400
    
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO saved_articles (user_id, url, title, collection_id, saved_at) VALUES (?, ?, ?, ?, ?)",
                (user["id"], url, title, collection_id, datetime.now(timezone.utc).isoformat())
            )
        return jsonify({"success": True}), 201
    except sqlite3.IntegrityError:
        return jsonify({"alreadySaved": True, "success": True}), 200


@app.route("/api/saved/<path:url>", methods=["DELETE"])
def delete_saved_article(url):
    if not validate_csrf():
        return jsonify({"error": "Security token expired. Refresh and try again."}), 403

    user = current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    with get_db() as db:
        db.execute("DELETE FROM saved_articles WHERE user_id = ? AND url = ?", (user["id"], url))
    
    return jsonify({"success": True})


# Collections
@app.route("/api/collections", methods=["GET"])
def get_collections():
    user = current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, created_at FROM collections WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],)
        ).fetchall()
    
    collections = [dict(row) for row in rows]
    return jsonify({"collections": collections})


@app.route("/api/collections", methods=["POST"])
def create_collection():
    user = current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    
    if not name or len(name) < 2:
        return jsonify({"error": "Collection name must be at least 2 characters"}), 400
    
    try:
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO collections (user_id, name, created_at) VALUES (?, ?, ?)",
                (user["id"], name, datetime.now(timezone.utc).isoformat())
            )
            collection_id = cursor.lastrowid
        return jsonify({"id": collection_id, "name": name}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Collection with that name already exists"}), 409


@app.route("/api/collections/<int:cid>", methods=["DELETE"])
def delete_collection(cid):
    user = current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    with get_db() as db:
        db.execute("DELETE FROM collections WHERE id = ? AND user_id = ?", (cid, user["id"]))
    
    return jsonify({"success": True})


# Article Annotations
@app.route("/api/annotations/<path:url>", methods=["GET"])
def get_annotations(url):
    user = current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    with get_db() as db:
        rows = db.execute(
            "SELECT annotation, annotation_type, created_at FROM article_annotations WHERE user_id = ? AND url = ?",
            (user["id"], url)
        ).fetchall()
    
    annotations = [dict(row) for row in rows]
    return jsonify({"annotations": annotations})


@app.route("/api/annotations/<path:url>", methods=["POST"])
def add_annotation(url):
    user = current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    annotation = data.get("annotation", "").strip()
    annotation_type = data.get("type", "note")
    
    if not annotation:
        return jsonify({"error": "Annotation text required"}), 400
    
    with get_db() as db:
        db.execute(
            "INSERT INTO article_annotations (user_id, url, annotation, annotation_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], url, annotation, annotation_type, datetime.now(timezone.utc).isoformat())
        )
    
    return jsonify({"success": True}), 201


# Related Articles
@app.route("/api/related/<path:url>", methods=["GET"])
def get_related_articles(url):
    url = url.strip()
    
    with get_db() as db:
        # Get the article
        article = db.execute(
            "SELECT summary FROM summaries WHERE url = ?", (url,)
        ).fetchone()
        
        if not article:
            return jsonify({"articles": []})
        
        # Find articles with similar summaries (simple keyword overlap)
        all_articles = db.execute(
            "SELECT url, summary FROM summaries LIMIT 1000"
        ).fetchall()
    
    keywords = set(re.findall(r"\b\w{4,}\b", article["summary"].lower()))
    keywords = {w for w in keywords if w not in STOP_WORDS}
    
    scored = []
    for row in all_articles:
        if row["url"] == url:
            continue
        other_keywords = set(re.findall(r"\b\w{4,}\b", row["summary"].lower()))
        overlap = len(keywords & other_keywords)
        if overlap > 0:
            scored.append({"url": row["url"], "score": overlap})
    
    related = sorted(scored, key=lambda x: x["score"], reverse=True)[:5]
    return jsonify({"related": related})


# Newsletter Signup
@app.route("/api/newsletter/subscribe", methods=["POST"])
def newsletter_subscribe():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email"))
    frequency = data.get("frequency", "daily")
    
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "Valid email required"}), 400
    
    unsubscribe_token = secrets.token_urlsafe(32)
    
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO newsletters (email, unsubscribe_token, subscribed_at, frequency) VALUES (?, ?, ?, ?)",
                (email, unsubscribe_token, datetime.now(timezone.utc).isoformat(), frequency)
            )
        return jsonify({"success": True, "message": "Subscribed to newsletter"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already subscribed"}), 409


@app.route("/api/newsletter/unsubscribe/<token>", methods=["POST"])
def newsletter_unsubscribe(token):
    with get_db() as db:
        db.execute("DELETE FROM newsletters WHERE unsubscribe_token = ?", (token,))
    
    return jsonify({"success": True, "message": "Unsubscribed from newsletter"})


# Daily Digest Generation
@app.route("/api/digest", methods=["GET"])
def get_digest():
    """Generate a digest of trending articles for the user or anonymously."""
    user = current_user()
    
    # Get top articles across categories
    all_articles = []
    for category in CATEGORIES:
        articles = news_cache["articles"].get(category, [])
        all_articles.extend(articles)
    
    # Sort by sentiment, trending score, and recency
    sorted_articles = sorted(
        all_articles,
        key=lambda a: (
            a.get("trending", False),
            a.get("trend_score", 0),
            a.get("sentiment") == "positive"
        ),
        reverse=True
    )[:10]
    
    if user:
        # Store in digest history
        with get_db() as db:
            newsletters = db.execute(
                "SELECT id FROM newsletters WHERE email = ?", (user.get("email"),)
            ).fetchone()
            if newsletters:
                db.execute(
                    "INSERT INTO digest_history (newsletter_id, sent_at, articles_json) VALUES (?, ?, ?)",
                    (newsletters["id"], datetime.now(timezone.utc).isoformat(), json.dumps([a["url"] for a in sorted_articles]))
                )
    
    return jsonify({"digest": sorted_articles})


init_db()

if os.getenv("START_NEWS_SCHEDULER", "").lower() == "true":
    start_news_scheduler()


if __name__ == "__main__":
    start_news_scheduler()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False, use_reloader=False)
