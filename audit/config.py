import json
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
    llm_max_continuations: int = 2  # extra calls to finish an answer cut off at the output limit
    llm_pdf_input: str = "off"  # file_id | inline | images | off -- how the original PDF goes to the model
    llm_image_dpi: int = 150          # images mode: render resolution (72 dpi misread chart labels on Qwen)
    llm_image_pages: str = "figures"  # images mode: figures = pages with a figure/table only; all = every page
    llm_cache_control: bool = False   # mark system prompt and paper as cacheable (cache_control, Qwen/Anthropic style)
    # Extra request parameters, passed through unchanged (provider-specific), e.g. to turn off or
    # limit a model's hidden reasoning, which is billed as output and can fill the output limit.
    llm_reasoning_effort: str | None = None  # sent as reasoning_effort (OpenAI-style), if set
    llm_extra_body: dict | None = None       # JSON object merged into the request body, if set
    llm_json_mode: bool = False               # response_format = {"type": "json_object"}


PDF_MODES = ("file_id", "inline", "images", "off")
IMAGE_PAGES = ("figures", "all")


def _pdf_mode(v: str) -> str:
    v = v.strip().lower()
    if v not in PDF_MODES:
        raise ValueError(f"LLM_PDF_INPUT must be one of {', '.join(PDF_MODES)}, not {v!r}")
    return v


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
        llm_max_continuations=int(os.getenv("LLM_MAX_CONTINUATIONS", "2")),
        llm_pdf_input=_pdf_mode(os.getenv("LLM_PDF_INPUT", "off")),
        llm_image_dpi=int(os.getenv("LLM_IMAGE_DPI", "150")),
        llm_image_pages=_choice("LLM_IMAGE_PAGES", os.getenv("LLM_IMAGE_PAGES", "figures"), IMAGE_PAGES),
        llm_cache_control=_flag(os.getenv("LLM_CACHE_CONTROL", "0")),
        llm_reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip() or None,
        llm_extra_body=_json_obj(os.getenv("LLM_EXTRA_BODY", "")),
        llm_json_mode=_flag(os.getenv("LLM_JSON_MODE", "0")),
    )


def _flag(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes")


def _choice(name: str, v: str, allowed: tuple) -> str:
    v = v.strip().lower()
    if v not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, not {v!r}")
    return v


def _json_obj(v: str) -> dict | None:
    v = v.strip()
    if not v:
        return None
    try:
        obj = json.loads(v)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM_EXTRA_BODY must be a JSON object, e.g. {{\"thinking\": {{\"type\": \"disabled\"}}}}: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("LLM_EXTRA_BODY must be a JSON object")
    return obj
