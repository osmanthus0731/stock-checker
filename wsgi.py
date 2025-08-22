
# WSGI entrypoint for production
# Ensures your Flask app is exposed to Gunicorn / Render.

import os

# If your main file isn't 'app.py', change the import below accordingly:
from app import app as application

# Export as 'app' as well for platforms that look for this name
app = application

if __name__ == "__main__":
    # Local run (not used in Render, but handy to test)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
