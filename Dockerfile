
# Optional: deploy anywhere with Docker
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Platforms (Render/Railway) will override PORT
ENV PORT=10000
EXPOSE 10000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
