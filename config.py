import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    telegram_token: str = field(default_factory=lambda: os.environ["TELEGRAM_TOKEN"])
    llm_base_url: str = field(
        default_factory=lambda: os.getenv(
            "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
        )
    )
    llm_api_key: str = field(default_factory=lambda: os.environ["LLM_API_KEY"])
    llm_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "gemini-2.5-flash")
    )
    allowed_users: list[int] = field(default_factory=lambda: [
        int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()
    ])
    proxy_url: str = field(
        default_factory=lambda: os.getenv("PROXY_URL", "")
    )
    max_input_chars: int = 100_000
    whisper_model: str = field(
        default_factory=lambda: os.getenv("WHISPER_MODEL", "base")
    )


config = Config()
