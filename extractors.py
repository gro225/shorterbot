"""Content extractors: URL, files (PDF/DOCX/TXT), video (YouTube subs / whisper)."""

import logging
import os
import re
import tempfile
import threading
from pathlib import Path

import httpx
import trafilatura

log = logging.getLogger(__name__)

_PROXY_URL = os.getenv("PROXY_URL", "")

# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------

YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+)"
)


def is_youtube(url: str) -> bool:
    return bool(YOUTUBE_RE.search(url))


def extract_url(url: str) -> str:
    """Fetch and extract main text from a web page."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ValueError(f"Не удалось загрузить {url}")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
    if not text:
        raise ValueError("Не удалось извлечь текст со страницы")
    return text


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def extract_pdf(path: Path) -> str:
    import pdfplumber

    pages = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            t = p.extract_text()
            if t:
                pages.append(t)
    if not pages:
        raise ValueError("PDF пустой или содержит только изображения")
    return "\n\n".join(pages)


def extract_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if not text:
        raise ValueError("DOCX пустой")
    return text


def extract_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    elif suffix in (".docx", ".doc"):
        return extract_docx(path)
    else:
        # plain text / md / html / csv — read as-is
        return path.read_text(errors="replace")


# ---------------------------------------------------------------------------
# Video / Audio — YouTube subtitles or Whisper
# ---------------------------------------------------------------------------


def _pick_sub_file(files: list[Path], lang: str) -> Path:
    """Prefer the video's own language, then original English, then whatever we got."""
    for want in (lang, "en-orig", "en", "ru"):
        if not want:
            continue
        for f in files:
            # names look like subs.<lang>.vtt
            if f.name.split(".")[-2] == want:
                return f
    return files[0]


def extract_youtube_subs(url: str) -> str:
    """Get subtitles via yt-dlp. Prefers manual subs, falls back to auto."""
    import json
    import subprocess

    # Try to get subtitle info
    proxy_args = ["--proxy", _PROXY_URL] if _PROXY_URL else []
    result = subprocess.run(
        ["yt-dlp", *proxy_args, "--dump-json", "--skip-download", url],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        raise ValueError(f"yt-dlp не смог обработать URL: {result.stderr[:200]}")

    info = json.loads(result.stdout)
    title = info.get("title", "")

    # Download subtitles
    with tempfile.TemporaryDirectory() as tmpdir:
        sub_args = [
            "yt-dlp",
            *proxy_args,
            "--ignore-errors",  # one unavailable language must not kill the rest
            "--write-subs", "--write-auto-subs",
            "--sub-langs", "ru,en",
            "--sub-format", "vtt",
            "--skip-download",
            "-o", f"{tmpdir}/subs",
            url,
        ]
        subprocess.run(sub_args, capture_output=True, timeout=300)

        # Find downloaded sub file
        sub_files = list(Path(tmpdir).glob("*.vtt"))
        if not sub_files:
            return ""  # no subs available — caller should try whisper
        sub_file = _pick_sub_file(sub_files, info.get("language") or "")

        raw = sub_file.read_text(errors="replace")
        # Clean VTT: remove timestamps and tags
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("WEBVTT") or "-->" in line:
                continue
            line = re.sub(r"<[^>]+>", "", line)
            if line and (not lines or line != lines[-1]):
                lines.append(line)

    text = " ".join(lines)
    if title:
        text = f"[{title}]\n\n{text}"
    return text


def extract_video_whisper(url: str) -> str:
    """Download audio via yt-dlp, transcribe with faster-whisper."""
    from config import config

    with tempfile.TemporaryDirectory() as tmpdir:
        return _transcribe_file(download_audio(url, tmpdir), config.whisper_model)


def extract_telegram_voice(file_path: Path) -> str:
    """Transcribe a voice/audio file sent directly to Telegram."""
    from config import config
    return _transcribe_file(file_path, config.whisper_model)


def _transcribe_file(path: Path, model_size: str = "base") -> str:
    text, lang = transcribe(path, model_size)
    return f"[Транскрипция, язык: {lang}]\n\n{text}"


_models: dict = {}
# модель грузится один раз, а распознавание идёт по одному: на CPU параллельные
# прогоны только мешают друг другу
_whisper_lock = threading.Lock()


def _get_model(model_size: str):
    from faster_whisper import WhisperModel

    if model_size not in _models:
        log.info("Loading whisper model %s", model_size)
        _models[model_size] = WhisperModel(
            model_size, device="cpu", compute_type="int8", cpu_threads=8
        )
    return _models[model_size]


def _decode_audio(path: Path):
    """Decode to 16 kHz mono float32 via ffmpeg.

    Свой декодер вместо встроенного: faster-whisper 1.2.1 зовёт
    av.open(metadata_errors=...), а в свежем PyAV этого аргумента нет.
    """
    import subprocess

    import numpy as np

    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, timeout=600,
    )
    if result.returncode != 0:
        raise ValueError(f"ffmpeg не смог прочитать аудио: {result.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(result.stdout, np.int16).astype(np.float32) / 32768.0


def transcribe(path: Path, model_size: str | None = None) -> tuple[str, str]:
    """Whisper transcript split into paragraphs on pauses. Returns (text, language)."""
    from config import config

    model_size = model_size or config.whisper_model
    with _whisper_lock:
        model = _get_model(model_size)
        segments, info = model.transcribe(_decode_audio(path), beam_size=5, vad_filter=True)

        paragraphs: list[str] = []
        current: list[str] = []
        prev_end = 0.0
        for seg in segments:
            piece = seg.text.strip()
            if not piece:
                continue
            size = sum(len(x) for x in current)
            # новый абзац на паузе или когда абзац разросся и фраза закончилась
            if current and (seg.start - prev_end > 2.0 or (size > 600 and current[-1][-1:] in ".!?…")):
                paragraphs.append(" ".join(current))
                current = []
            current.append(piece)
            prev_end = seg.end
        if current:
            paragraphs.append(" ".join(current))

    text = "\n\n".join(paragraphs)
    if not text:
        raise ValueError("Whisper не распознал текст")
    return text, info.language


def download_audio(url: str, tmpdir: str) -> Path:
    """Download audio of any yt-dlp supported page into tmpdir."""
    import subprocess

    proxy_args = ["--proxy", _PROXY_URL] if _PROXY_URL else []
    result = subprocess.run(
        ["yt-dlp", *proxy_args, "-x", "--audio-format", "mp3", "-o", f"{tmpdir}/audio.%(ext)s", url],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise ValueError(f"Не удалось скачать аудио: {result.stderr[:200]}")
    candidates = list(Path(tmpdir).glob("audio*"))
    if not candidates:
        raise ValueError("Аудиофайл не найден после скачивания")
    return candidates[0]


# ---------------------------------------------------------------------------
# Detect URLs in text
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://\S+")


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text)
