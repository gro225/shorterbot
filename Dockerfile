FROM python:3.12-slim

# deb.debian.org недоступен с датадома (блокировка Fastly-адресов), берём зеркало
RUN sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# JS runtime for yt-dlp: YouTube extraction without one is deprecated
COPY --from=denoland/deno:bin-2.9.6 /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
