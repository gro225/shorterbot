"""Summarizer Telegram bot — extracts the essence from text, URLs, files, video."""

import asyncio
import logging
import tempfile
from collections import OrderedDict
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from telegramify_markdown import convert, split_entities

from config import config
from extractors import (
    extract_file,
    extract_url,
    extract_video_whisper,
    extract_youtube_subs,
    download_audio,
    find_urls,
    is_youtube,
    transcribe,
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


async def send_long(message: types.Message, text: str, reply_markup=None):
    """Convert Markdown to Telegram entities and send, splitting if needed."""
    plain, entities = convert(text)
    chunks = list(split_entities(plain, entities, max_utf16_len=4096))
    for i, (chunk_text, chunk_entities) in enumerate(chunks):
        await message.answer(
            chunk_text,
            entities=[types.MessageEntity(**e.to_dict()) for e in chunk_entities],
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
        )


# Расшифровки последних голосовых — для кнопки «Расшифровка» под пересказом.
# Живут в памяти: после сна/рестарта контейнера кнопка честно говорит, что текста нет.
TRANSCRIPTS: OrderedDict[str, str] = OrderedDict()
TRANSCRIPTS_MAX = 100

# Пользователи, нажавшие /text без вложения: следующее аудио отдаём текстом
AWAIT_TEXT: set[int] = set()

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API не отдаёт файлы больше 20 МБ
FILE_THRESHOLD = 12_000  # длиннее — расшифровка уходит файлом


def remember_transcript(message: types.Message, text: str) -> InlineKeyboardMarkup:
    key = f"{message.chat.id}:{message.message_id}"
    TRANSCRIPTS[key] = text
    TRANSCRIPTS.move_to_end(key)
    while len(TRANSCRIPTS) > TRANSCRIPTS_MAX:
        TRANSCRIPTS.popitem(last=False)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Расшифровка", callback_data=f"tr:{key}"),
    ]])


async def send_transcript(message: types.Message, text: str, header: str = ""):
    """Send raw transcript as plain text (no Markdown), or as a .txt file if long."""
    full = f"{header}\n\n{text}" if header else text
    if len(full) > FILE_THRESHOLD:
        await message.answer_document(
            BufferedInputFile(full.encode(), filename="transcript.txt"),
            caption=f"{header}\n{len(text)} символов" if header else f"{len(text)} символов",
        )
        return
    while full:
        if len(full) <= 4000:
            chunk, full = full, ""
        else:
            cut = full.rfind("\n", 0, 4000)
            if cut < 2000:
                cut = full.rfind(" ", 0, 4000)
            if cut <= 0:
                cut = 4000
            chunk, full = full[:cut], full[cut:].lstrip()
        await message.answer(chunk)


def media_of(message: types.Message | None):
    """Audio-bearing attachment of a message, if any."""
    if not message:
        return None
    return message.voice or message.audio or message.video_note or message.video


async def download_media(obj, suffix: str = ".ogg") -> Path:
    if obj.file_size and obj.file_size > TG_DOWNLOAD_LIMIT:
        raise ValueError(
            "Файл больше 20 МБ — Telegram не даёт ботам такие скачивать. "
            "Залей его куда-нибудь и пришли ссылку: /text <ссылка>"
        )
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file = await bot.get_file(obj.file_id)
        await bot.download_file(file.file_path, tmp)
        return Path(tmp.name)


async def process_and_reply(message: types.Message, content: str, mode: str = "default", reply_markup=None):
    """Summarize content and reply."""
    if not content or not content.strip():
        await message.answer("Не удалось извлечь текст.")
        return
    status = await message.answer("⏳ Обрабатываю...")
    try:
        result = await summarize(content, mode=mode)
        await send_long(message, result, reply_markup=reply_markup)
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
        "/eli5 — простыми словами\n"
        "/text — расшифровка аудио без пересказа\n\n"
        "Голосовое, аудио или кружочек — пришлю пересказ, а под ним кнопку "
        "с полной расшифровкой. Чтобы сразу получить только текст: ответь "
        "на аудио командой /text, подпиши файл /text или пришли /text <ссылка на видео>.\n\n"
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

        content = await asyncio.to_thread(extract_file, tmp_path)
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


async def transcribe_media(message: types.Message, obj) -> tuple[str, str]:
    """Download Telegram media and run Whisper off the event loop."""
    tmp_path = await download_media(obj)
    try:
        return await asyncio.to_thread(transcribe, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


async def reply_transcript(message: types.Message, obj):
    status = await message.answer("🎤 Расшифровываю...")
    try:
        text, lang = await transcribe_media(message, obj)
        await send_transcript(message, text, header=f"Расшифровка (язык: {lang})")
    except Exception as e:
        log.exception("Transcription failed")
        await message.answer(f"Ошибка: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


async def transcribe_url(message: types.Message, url: str):
    status = await message.answer("🎬 Достаю текст...")
    try:
        content = ""
        if is_youtube(url):
            content = await asyncio.to_thread(extract_youtube_subs, url)
        if content:
            await send_transcript(message, content)
            return
        await status.edit_text("🎤 Скачиваю аудио и расшифровываю (это займёт время)...")
        with tempfile.TemporaryDirectory() as tmpdir:
            audio = await asyncio.to_thread(download_audio, url, tmpdir)
            text, lang = await asyncio.to_thread(transcribe, audio)
        await send_transcript(message, text, header=f"Расшифровка (язык: {lang})")
    except Exception as e:
        log.exception("URL transcription failed")
        await message.answer(f"Ошибка: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


@dp.message(Command("text"))
async def cmd_text(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    # Command смотрит и в подпись: аудио с подписью /text приходит сюда же
    obj = media_of(message) or media_of(message.reply_to_message)
    if obj:
        await reply_transcript(message, obj)
        return
    urls = find_urls(message.text or "")
    if urls:
        await transcribe_url(message, urls[0])
        return
    AWAIT_TEXT.add(message.from_user.id)
    await message.answer("Пришли голосовое, аудио или кружочек — верну расшифровку без пересказа.")


@dp.callback_query(F.data.startswith("tr:"))
async def cb_transcript(callback: types.CallbackQuery):
    text = TRANSCRIPTS.get(callback.data[3:])
    if text is None:
        await callback.answer(
            "Расшифровка уже забыта (бот перезапускался). Ответь на аудио командой /text.",
            show_alert=True,
        )
        return
    await callback.answer()
    await send_transcript(callback.message, text)


@dp.message(F.voice | F.audio | F.video_note | (F.video & F.caption.startswith("/text")))
async def handle_voice(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    obj = media_of(message)
    caption = (message.caption or "").strip().lower()
    if caption.startswith("/text") or message.from_user.id in AWAIT_TEXT:
        AWAIT_TEXT.discard(message.from_user.id)
        await reply_transcript(message, obj)
        return

    status = await message.answer("🎤 Транскрибирую аудио...")
    try:
        text, lang = await transcribe_media(message, obj)
        await status.delete()
        markup = remember_transcript(message, text)
        await process_and_reply(
            message, f"[Транскрипция, язык: {lang}]\n\n{text}", reply_markup=markup
        )
    except Exception as e:
        log.exception("Voice processing failed")
        await message.answer(f"Ошибка: {e}")


async def handle_url(message: types.Message, url: str, mode: str = "default"):
    """Process a URL — web article or video."""
    if is_youtube(url):
        status = await message.answer("🎬 Достаю субтитры...")
        try:
            content = await asyncio.to_thread(extract_youtube_subs, url)
            if not content:
                await status.edit_text("Субтитров нет, транскрибирую аудио (это займёт время)...")
                content = await asyncio.to_thread(extract_video_whisper, url)
            await status.delete()
            await process_and_reply(message, content, mode)
        except Exception as e:
            log.exception("YouTube processing failed")
            await message.answer(f"Ошибка: {e}")
    else:
        try:
            content = await asyncio.to_thread(extract_url, url)
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
