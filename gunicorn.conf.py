# gunicorn.conf.py
import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
workers = int(os.getenv("WEB_CONCURRENCY", "1"))      # start with 1 while debugging
threads = int(os.getenv("GUNICORN_THREADS", "1"))      # keep simple; scale later
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
keepalive = 5
accesslog = "-"
errorlog = "-"

# IMPORTANT: don't preload in a multi-worker setup when you create MongoClient at import time
preload_app = False
