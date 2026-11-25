import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_temperature: float
    db_path: Path
    max_doc_chars: int
    llm_max_output_tokens: int | None


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    db_path = Path(os.getenv("AUDIT_DB_PATH", "data/audit.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    return Settings(
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_model=os.getenv("LLM_MODEL", ""),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        db_path=db_path,
        # Whole paper in one call; only papers longer than this are split into chunks.
        # (SEGMENT_MAX_CHARS from v1 is no longer used.)
        max_doc_chars=int(os.getenv("MAX_DOC_CHARS", "200000")),
        llm_max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "16000")) or None,
    )
