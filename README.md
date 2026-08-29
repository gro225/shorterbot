# Summarizer Bot

Telegram-бот для извлечения сути из текста, ссылок, файлов и видео.

## Возможности

- **Текст** — пришли текст, получи суть
- **URL** — ссылка на статью → извлечение + суммаризация
- **Файлы** — PDF, DOCX, TXT, MD, HTML, CSV
- **Видео** — YouTube (субтитры), другие платформы (yt-dlp + whisper)
- **Голосовые** — транскрипция через faster-whisper
- **Режимы** — `/brief`, `/detailed`, `/bullets`, `/eli5`

## Деплой

```bash
cp .env.example .env
# заполни .env

docker compose up -d --build
```

## Без Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# нужен ffmpeg в системе для whisper/yt-dlp
cp .env.example .env
python bot.py
```

## Использование

- Просто отправь текст (>50 символов) → получишь суть
- Отправь ссылку → бот скачает и обработает
- Отправь файл (PDF/DOCX/TXT) → извлечёт текст и суммаризует
- Отправь YouTube-ссылку → возьмёт субтитры (или транскрибирует)
- `/brief https://...` → сверхкраткая выжимка по ссылке
- Голосовое сообщение → транскрибирует и суммаризует
