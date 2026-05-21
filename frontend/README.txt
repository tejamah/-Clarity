Live Summarized News frontend

The app is a static HTML/CSS/JavaScript client in frontend-react/public/index.html.
It is served by the Flask backend at http://127.0.0.1:5000/.

Run from the repository root:

1. python -m venv .venv
2. .venv\Scripts\activate
3. pip install -r backend\requirements.txt
4. python backend\app.py

The backend uses free public RSS feeds and a local extractive summarizer. No API keys
or paid services are required.

Optional Google Custom Search API:

1. Create a Google API key and Programmable Search Engine ID.
2. Set GOOGLE_API_KEY and GOOGLE_CSE_ID before running the backend.

Google's free Custom Search JSON API quota is limited, so the backend only refreshes
that optional source every 4 hours. Google News RSS continues refreshing every 2
minutes without requiring keys.
