# Deployment

The easiest free path for this app is Render with the included `render.yaml`.

## Deploy On Render

1. Push this repository to GitHub.
2. Open Render and choose **New +** -> **Web Service** or **Blueprint**.
3. Connect the GitHub repository.
4. Use the free instance type.
5. Click **Deploy Web Service** and wait for the build to finish.

For a manual web service, use:

```bash
Build Command: pip install -r requirements.txt
Start Command: gunicorn -w 1 -b 0.0.0.0:$PORT backend.app:app
```

`-w 1` is intentional because the app runs a background news refresh scheduler.
Multiple workers would start multiple schedulers.

## Environment Variables

Required:

- `PYTHON_VERSION=3.11.9` - also pinned in `.python-version`.
- `SECRET_KEY` - generated automatically by Render.
- `START_NEWS_SCHEDULER=true` - starts continuous news refreshes under Gunicorn.
- `SESSION_COOKIE_SECURE=true` - secure cookies over HTTPS.

Optional:

- `GOOGLE_API_KEY`
- `GOOGLE_CSE_ID`
- `SENTRY_DSN`
- `FRONTEND_ORIGIN`

The app works without Google API keys because it uses free RSS feeds by default.

## Local Production Smoke Test

From the repo root:

```bash
pip install -r backend/requirements-deploy.txt
python backend/app.py
```

Then open:

```text
http://127.0.0.1:5000/
```

## Notes

The SQLite database is local to the deployed instance. On free hosts, local files may be reset when the service restarts or redeploys. For durable saved users/articles, move the database to a managed free Postgres service later.
