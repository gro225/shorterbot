"""Summarizer Telegram bot — extracts the essence from text, URLs, files, video."""

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile

from telegramify_markdown import convert, split_entities

from config import config
from extractors import (
    extract_file,
    extract_telegram_voice,
    extract_url,
    extract_video_whisper,
    extract_youtube_subs,
    find_urls,
    is_youtube,
)
from summarizer import summarize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

session = AiohttpSession(proxy=config.proxy_url) if config.proxy_url else None
bot = Bot(token=config.telegram_token, session=session)
dp = Dispatcher()

# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


def is_allowed(user_id: int) -> bool:
    if not config.allowed_users:
        return True  # no whitelist = open
    return user_id in config.allowed_users


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MODES = {
    "/brief": "brief",
    "/detailed": "detailed",
    "/bullets": "bullets",
    "/eli5": "eli5",
}


async def send_long(message: types.Message, text: str):
    """Convert Markdown to Telegram entities and send, splitting if needed."""
    plain, entities = convert(text)
    for chunk_text, chunk_entities in split_entities(plain, entities, max_utf16_len=4096):
        await message.answer(
            chunk_text,
            entities=[types.MessageEntity(**e.to_dict()) for e in chunk_entities],
        )


async def process_and_reply(message: types.Message, content: str, mode: str = "default"):
    """Summarize content and reply."""
    if not content or not content.strip():
        await message.answer("Не удалось извлечь текст.")
        return
    status = await message.answer("⏳ Обрабатываю...")
    try:
        result = await summarize(content, mode=mode)
        await send_long(message, result)
    except Exception as e:
        log.exception("Summarization failed")
        await message.answer(f"Ошибка: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "Присылай текст, ссылку, файл (PDF/DOCX/TXT) или видео-ссылку — "
        "я извлеку суть.\n\n"
        "Режимы:\n"
        "/brief — сверхкраткая выжимка\n"
        "/detailed — подробный пересказ\n"
        "/bullets — тезисы списком\n"
        "/eli5 — простыми словами\n\n"
        "Режим можно добавить перед текстом/ссылкой:\n"
        "/brief https://example.com/article"
    )


@dp.message(Command("brief", "detailed", "bullets", "eli5"))
async def cmd_mode(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    cmd = message.text.split()[0].lower()
    mode = MODES.get(cmd, "default")
    rest = message.text[len(cmd) :].strip()

    if not rest:
        await message.answer(f"Режим `{mode}` — пришли текст или ссылку после команды.")
        return

    urls = find_urls(rest)
    if urls:
        await handle_url(message, urls[0], mode)
    else:
        await process_and_reply(message, rest, mode)


@dp.message(F.document)
async def handle_document(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    doc = message.document
    suffix = Path(doc.file_name or "file").suffix.lower()
    if suffix not in (".pdf", ".docx", ".doc", ".txt", ".md", ".html", ".csv"):
        await message.answer(f"Формат `{suffix}` не поддерживается. Отправь PDF, DOCX или текстовый файл.")
        return

    status = await message.answer("📥 Скачиваю файл...")
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            file = await bot.get_file(doc.file_id)
            await bot.download_file(file.file_path, tmp)
            tmp_path = Path(tmp.name)

        content = extract_file(tmp_path)
        await status.delete()
        await process_and_reply(message, content)
    except Exception as e:
        log.exception("File processing failed")
        await message.answer(f"Ошибка при обработке файла: {e}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@dp.message(F.voice | F.audio)
async def handle_voice(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    obj = message.voice or message.audio
    status = await message.answer("🎤 Транскрибирую аудио...")
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            file = await bot.get_file(obj.file_id)
            await bot.download_file(file.file_path, tmp)
            tmp_path = Path(tmp.name)

        content = extract_telegram_voice(tmp_path)
        await status.delete()
        await process_and_reply(message, content)
    except Exception as e:
        log.exception("Voice processing failed")
        await message.answer(f"Ошибка: {e}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


async def handle_url(message: types.Message, url: str, mode: str = "default"):
    """Process a URL — web article or video."""
    if is_youtube(url):
        status = await message.answer("🎬 Достаю субтитры...")
        try:
            content = extract_youtube_subs(url)
            if not content:
                await status.edit_text("Субтитров нет, транскрибирую аудио (это займёт время)...")
                content = extract_video_whisper(url)
            await status.delete()
            await process_and_reply(message, content, mode)
        except Exception as e:
            log.exception("YouTube processing failed")
            await message.answer(f"Ошибка: {e}")
    else:
        try:
            content = extract_url(url)
            await process_and_reply(message, content, mode)
        except Exception as e:
            log.exception("URL extraction failed")
            await message.answer(f"Ошибка при обработке ссылки: {e}")


@dp.message(F.text)
async def handle_text(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    text = message.text.strip()
    if not text:
        return

    urls = find_urls(text)
    if urls:
        # If the message is just a URL (± whitespace), process as URL
        # If it's URL + surrounding text, process the whole thing as text
        stripped = text.replace(urls[0], "").strip()
        if not stripped or len(stripped) < 20:
            await handle_url(message, urls[0])
            return

    # Plain text — summarize directly
    if len(text) < 50:
        await message.answer("Текст слишком короткий для суммаризации. Пришли побольше.")
        return

    await process_and_reply(message, text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    log.info("Starting summarizer bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
