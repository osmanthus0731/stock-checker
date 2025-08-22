
# Deploy your Flask + MongoDB app online (Render.com)

This kit adds the missing deployment files so you can push your existing project online in minutes.

## What you get
- `requirements.txt` — exact Python deps for a production build
- `Procfile` — tells the platform how to start Gunicorn
- `gunicorn.conf.py` — sensible production defaults
- `wsgi.py` — exports your `app` for the process manager
- `render.yaml` — one‑click infra on Render (recommended region: Singapore)
- `.env.example` — the only 2 secrets you must set

> Your current code should already define `app = Flask(__name__)` in `app.py`.  
> If you used a different file name, adjust `wsgi.py` to import from it.

---

## Step 1 — Prepare MongoDB Atlas
1. Create a **Database User** (username + password).
2. Allow access from your server: add an IP rule (you can start with `0.0.0.0/0` for testing).
3. Copy the **SRV connection string** and paste it in `.env` later.
   It looks like:  
   `mongodb+srv://<user>:<pass>@<cluster>/<dbname>?retryWrites=true&w=majority&appName=<AppName>`

## Step 2 — Add these files to your repo
Drop all files from this kit into the **project root** (same folder as your `app.py`).  
Commit and push to GitHub.

## Step 3 — Deploy on Render (recommended)
1. Go to Render → **New** → **Web Service** → **Connect your repo**.
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `gunicorn -c gunicorn.conf.py wsgi:app`
4. Environment: **Python 3.12** (or latest available).
5. Set **Environment Variables**:
   - `MONGO_URI` — your Atlas URI (from Step 1)
   - `FLASK_SECRET` — any long random string
   - *(optional)* `WEB_CONCURRENCY` (default 2), `GUNICORN_THREADS` (default 4)
6. Region: **Singapore** (closest to Malaysia). Click **Create Web Service**.

Render will build and start your app at a public URL like:  
`https://<your-service-name>.onrender.com`

## Optional — Docker / VPS
A `Dockerfile` is included if you prefer a VPS or Railway.  
Run locally with:  
```bash
docker build -t inventory-app .
docker run -p 10000:10000 --env-file .env inventory-app
```

---

## Notes / Gotchas
- **Windows-only ODBC (MS Access)**: If your code tries to import `pyodbc`, it's disabled on Linux hosts.
  The sample `requirements.txt` only installs `pyodbc` on Windows to avoid build failures.
  Make sure your app guards any Access-specific code paths or runs fine without them.
- **QR images**: Your app can continue writing to `static/qr_codes/`. If you redeploy, files may be cleared.
  You can regenerate on demand, or switch to storing the PNGs in MongoDB GridFS or S3 later.
- **Port**: Platforms set `$PORT`. `wsgi.py` and `gunicorn.conf.py` already respect it.
- **Python version**: Use 3.12 for widest platform support today.

Happy shipping! 🚀
