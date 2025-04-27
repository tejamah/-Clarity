from flask import Flask, jsonify, request
from flask_socketio import SocketIO
from flask_cors import CORS
import threading
import time
import feedparser

from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
import torch

# === Flask Setup ===
app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# === Model Setup ===
tokenizer = AutoTokenizer.from_pretrained("teja00007/model-name")
model = AutoModelForSeq2SeqLM.from_pretrained("teja00007/model-name")

# === RSS Feeds for News Categories ===
rss_feeds = {
    "general": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "technology": "https://news.google.com/rss/search?q=technology&hl=en-US&gl=US&ceid=US:en",
    "business": "https://news.google.com/rss/search?q=business&hl=en-US&gl=US&ceid=US:en",
    "sports": "https://news.google.com/rss/search?q=sports&hl=en-US&gl=US&ceid=US:en",
    "entertainment": "https://news.google.com/rss/search?q=entertainment&hl=en-US&gl=US&ceid=US:en"
}

# === Global Cache for News ===
news_cache = {}

# === Summarization Route ===
@app.route("/summarize", methods=["POST"])
def summarize():
    data = request.get_json()
    text = data.get("text", "")

    if not text:
        return jsonify({"error": "No text provided"}), 400

    try:
        inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
        output = model.generate(**inputs, max_new_tokens=100)
        summary = tokenizer.decode(output[0], skip_special_tokens=True)
        return jsonify({"summary": summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === Summarize Text Internally ===
def summarize_internal(text):
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    output = model.generate(**inputs, max_new_tokens=100)
    summary = tokenizer.decode(output[0], skip_special_tokens=True)
    return {"summary": summary}

# === Fetch News from RSS and Summarize ===
def fetch_and_summarize_news():
    global news_cache
    new_data = {}

    print("[+] Fetching and summarizing news from Google RSS...")

    for category, feed_url in rss_feeds.items():
        try:
            feed = feedparser.parse(feed_url)
            summarized_articles = []

            for entry in feed.entries[:20]:  # Limit to 20 articles per category
                try:
                    title = entry.title
                    description = entry.summary if hasattr(entry, 'summary') else title

                    # Summarize the description
                    summary_response = summarize_internal(description)
                    summary = summary_response.get("summary", "No summary available.")

                    summarized_articles.append({
                        "title": title,
                        "summary": summary,
                        "url": entry.link,
                        "image": ""  # No direct image in RSS
                    })

                except Exception as e:
                    print(f"[{category}] Error summarizing article: {e}")

            new_data[category] = summarized_articles

        except Exception as e:
            print(f"[{category}] Error fetching RSS feed: {e}")

    news_cache.clear()
    news_cache.update(new_data)
    socketio.emit("news_update", news_cache)
    print("[+] News updated and sent to clients.")

# === Scheduled News Updates ===
def schedule_updates():
    while True:
        fetch_and_summarize_news()
        time.sleep(300)  # every 5 minutes

# === API Routes ===
@app.route("/")
def home():
    return jsonify({"message": "Live Summarized News API is running!"})

@app.route("/news/<category>")
def get_news(category):
    news = news_cache.get(category)
    if news is None:
        return jsonify({"error": "Category not found"}), 404
    return jsonify(news)

@socketio.on("connect")
def handle_connect():
    print("[+] Client connected!")

# === Main Entry Point ===
if __name__ == "__main__":
    threading.Thread(target=schedule_updates, daemon=True).start()
    fetch_and_summarize_news()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
