"""Summarization via Google Gemini (OpenAI-compatible API)."""

import asyncio
import logging

import httpx

from config import config

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — инструмент для извлечения сути. Твоя задача — кратко и чётко пересказать \
содержание присланного текста.

Правила:
- Начинай сразу с сути, без вводных фраз
- Выдели главные идеи и ключевые факты
- Если текст длинный — структурируй ответ разделами
- Сохраняй важные числа, имена, даты
- Всегда отвечай на русском языке, независимо от языка исходного текста
- Объём ответа: 15-25% от исходного текста (но не менее 3 предложений)
"""

MODE_PROMPTS = {
    "brief": "Сделай максимально краткую выжимку — 3-5 предложений, только самое важное.",
    "detailed": "Подробный пересказ с сохранением структуры и деталей.",
    "bullets": "Ключевые тезисы списком, каждый — одно предложение.",
    "eli5": "Объясни суть простыми словами, как будто рассказываешь другу.",
}


async def summarize(text: str, mode: str = "default") -> str:
    """Send text to LLM and get summary back."""
    user_msg = text
    system = SYSTEM_PROMPT
    if mode in MODE_PROMPTS:
        system += f"\n\nРежим: {MODE_PROMPTS[mode]}"

    # Truncate if too long
    if len(user_msg) > config.max_input_chars:
        user_msg = user_msg[: config.max_input_chars] + "\n\n[...текст обрезан...]"

    max_tokens = 16384 if mode == "detailed" else 4096

    payload = {
        "model": config.llm_model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    }

    proxy = config.proxy_url or None
    headers = {
        "Authorization": f"Bearer {config.llm_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{config.llm_base_url}/chat/completions"

    async with httpx.AsyncClient(timeout=120, proxy=proxy) as client:
        for attempt in range(3):
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 503:
                log.warning("LLM API 503, retry %d/3...", attempt + 1)
                await asyncio.sleep(3 * (attempt + 1))
                continue
            break
        if resp.status_code != 200:
            log.error("LLM API error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        data = resp.json()

    return data["choices"][0]["message"]["content"]
