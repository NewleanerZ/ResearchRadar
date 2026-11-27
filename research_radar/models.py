from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from .runtime_paths import app_dir, bundle_dir, is_frozen


CORE_SOURCES = ("arxiv", "openalex")
EXTRA_SOURCES = ("crossref", "openreview", "dblp", "acl", "pmlr", "semantic_scholar", "cvf")
DEFAULT_EXTRA_SOURCES = ("crossref", "openreview", "acl", "pmlr")
SOURCE_LABELS = dict(zip(
    (*CORE_SOURCES, *EXTRA_SOURCES, "github", "huggingface"),
    ("arXiv", "OpenAlex", "Crossref", "OpenReview", "DBLP", "ACL Anthology", "PMLR",
     "Semantic Scholar", "CVF Open Access", "GitHub", "Hugging Face"),
))


ARXIV_ID = re.compile(r"^((?:\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7}))(v\d+)?$", re.I)


def arxiv_identifier(value: str) -> tuple[str | None, str | None]:
    if "://" in value:
        url = urlsplit(value)
        if url.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            return None, None
        value = re.sub(r"^/(abs|pdf)/", "", url.path).removesuffix(".pdf")
    match = ARXIV_ID.fullmatch(value.strip())
    return (match[1].lower(), match[2]) if match else (None, None)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fingerprint(value) -> str:
    return sha256(dump(value).encode()).hexdigest()


class Topic(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1500)
    keywords: list[str] = Field(min_length=1, max_length=12)
    excluded: list[str] = Field(default_factory=list, max_length=20)
    proposed_expansions: list[str] = Field(default_factory=list, max_length=12)
    expansions: list[str] = Field(default_factory=list, max_length=12)
    categories: list[str] = Field(default_factory=list, max_length=12)
    subtopics: dict[str, list[str]] = Field(default_factory=dict, max_length=12)
    timezone: str = "Asia/Hong_Kong"
    schedule: time = time(9, 5)
    enabled: bool = True  # Persisted setting for following the topic's automatic schedule.
    notify: bool = True
    archived: bool = False
    max_minutes: int = Field(default=15, ge=1, le=15)
    max_llm_calls: int = Field(default=15, ge=0, le=25)
    max_tokens: int = Field(default=65000, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("timezone")
    @classmethod
    def supported_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as error:
            raise ValueError("Use an IANA timezone") from error
        return value

    @field_validator("schedule")
    @classmethod
    def daily_schedule(cls, value):
        if value.tzinfo or value.second or value.microsecond:
            raise ValueError("Use a local HH:MM time")
        return value

    @field_validator("keywords", "excluded", "proposed_expansions", "expansions", "categories")
    @classmethod
    def clean_terms(cls, values):
        if any(not value.strip() or len(value.strip()) > 160 for value in values):
            raise ValueError("Use nonempty terms of up to 160 characters")
        return list(dict.fromkeys(value.strip() for value in values))

    @field_validator("subtopics")
    @classmethod
    def clean_subtopics(cls, values):
        if any(not key.strip() or key.strip() == "全部" or not items or len(items) > 12
               for key, items in values.items()):
            raise ValueError("Each subtopic needs a name and 1–12 terms")
        return {key.strip(): cls.clean_terms(items) for key, items in values.items()}

    def query_version(self) -> str:
        fields = {"keywords", "excluded", "expansions", "categories", "subtopics", "timezone"}
        return fingerprint(self.model_dump(include=fields))[:20]


class TopicDraft(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    name: str = Field(min_length=1, max_length=100)
    keywords: list[str] = Field(min_length=1, max_length=6)
    excluded: list[str] = Field(default_factory=list, max_length=8)
    proposed_expansions: list[str] = Field(default_factory=list, max_length=6)
    subtopics: dict[str, list[str]] = Field(default_factory=dict, max_length=5)


class Settings(BaseModel):
    data_dir: Path = Field(default_factory=lambda: (app_dir() / "data") if is_frozen() else Path("data"))
    openalex_key: SecretStr = SecretStr("")
    crossref_mailto: str = ""
    extra_sources: str = ",".join(DEFAULT_EXTRA_SOURCES)
    llm_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    input_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    feishu_webhook: SecretStr = SecretStr("")
    feishu_secret: SecretStr = SecretStr("")
    page_limit: int = Field(default=40, ge=1, le=300)
    embedding_revision: str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    keyword_only: bool = False
    chat_max_calls: int = Field(default=20, ge=2, le=40)
    chat_max_seconds: int = Field(default=240, ge=15, le=600)
    chat_history_tokens: int = Field(default=48000, ge=4000, le=180000)
    chat_turn_tokens: int = Field(default=120000, ge=16000, le=250000)
    artifact_inline_chars: int = Field(default=2400, ge=600, le=20000)
    s2_api_key: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    hf_token: SecretStr = SecretStr("")
    zotero_api_key: SecretStr = SecretStr("")
    zotero_user_id: str = ""
    api_token: SecretStr = SecretStr("")
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    reranker_enabled: bool = False
    reranker_model: str = "BAAI/bge-reranker-base"
    thinking_level: Literal["off", "low", "medium", "high"] = "off"
    stream_chat: bool = True

    @classmethod
    def from_env(cls):
        if is_frozen():
            env_file = app_dir() / ".env"
            example = bundle_dir() / ".env.example"
            if not env_file.exists() and example.exists():
                env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            load_dotenv(dotenv_path=env_file)
        else:
            load_dotenv()
        names = {
            "data_dir": "RADAR_DATA_DIR", "openalex_key": "OPENALEX_API_KEY",
            "crossref_mailto": "RADAR_CROSSREF_MAILTO", "extra_sources": "RADAR_EXTRA_SOURCES",
            "llm_key": "RADAR_LLM_API_KEY", "llm_base_url": "RADAR_LLM_BASE_URL",
            "llm_model": "RADAR_LLM_MODEL", "input_price": "RADAR_LLM_INPUT_PRICE",
            "output_price": "RADAR_LLM_OUTPUT_PRICE", "feishu_webhook": "FEISHU_WEBHOOK_URL",
            "feishu_secret": "FEISHU_WEBHOOK_SECRET", "page_limit": "RADAR_PAGE_LIMIT",
            "embedding_revision": "RADAR_EMBEDDING_REVISION", "keyword_only": "RADAR_KEYWORD_ONLY",
            "chat_max_calls": "RADAR_CHAT_MAX_CALLS", "chat_max_seconds": "RADAR_CHAT_MAX_SECONDS",
            "chat_history_tokens": "RADAR_CHAT_HISTORY_TOKENS", "chat_turn_tokens": "RADAR_CHAT_TURN_TOKENS",
            "artifact_inline_chars": "RADAR_ARTIFACT_INLINE_CHARS",
            "s2_api_key": "S2_API_KEY", "github_token": "GITHUB_TOKEN", "hf_token": "HF_TOKEN",
            "zotero_api_key": "ZOTERO_API_KEY", "zotero_user_id": "ZOTERO_USER_ID",
            "api_token": "RADAR_API_TOKEN", "api_host": "RADAR_API_HOST", "api_port": "RADAR_API_PORT",
            "reranker_enabled": "RADAR_RERANKER_ENABLED", "reranker_model": "RADAR_RERANKER_MODEL",
            "thinking_level": "RADAR_THINKING_LEVEL", "stream_chat": "RADAR_STREAM_CHAT",
        }
        return cls(**{field: os.environ[name] for field, name in names.items()
                      if os.getenv(name) or (field == "extra_sources" and name in os.environ)})

    def catalog_sources(self):
        requested = {source.strip().lower() for source in self.extra_sources.split(",")}
        return tuple(source for source in EXTRA_SOURCES if source in requested)

    def embedding_path(self) -> Path:
        local = self.data_dir.resolve() / "models" / "bge-small-en-v1.5" / self.embedding_revision
        if (local / "model.safetensors").exists() or (local / "pytorch_model.bin").exists():
            return local
        bundled = bundle_dir() / "models" / "bge-small-en-v1.5" / self.embedding_revision
        if bundled.exists():
            return bundled
        return local

    @field_validator("embedding_revision")
    @classmethod
    def fixed_embedding_revision(cls, value):
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("Use a full lowercase model commit hash")
        return value


class Publication(BaseModel):
    status: Literal["published", "accepted", "submitted", "preprint", "unknown"]
    venue: str | None = None


PUBLICATION_LABELS = {
    "published": "已发表", "accepted": "已录用", "submitted": "已投稿",
    "preprint": "预印本（尚未查到正式发表）", "unknown": "发表状态未确认",
}


class Record(BaseModel):
    source: Literal[
        "arxiv", "openalex", "crossref", "openreview", "dblp", "acl", "pmlr",
        "semantic_scholar", "cvf", "github", "huggingface",
    ]
    external_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://[^\s]+$")
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    open_pdf_urls: list[str] = Field(default_factory=list, max_length=8)
    version: str | None = None
    preprint_at: datetime | None = None
    published_on: date | None = None
    published_year: int | None = Field(default=None, ge=1900, le=2200)
    published_month: int | None = Field(default=None, ge=1, le=12)
    updated_at: datetime | None = None
    status: str = "unknown"
    publication: Publication | None = None
    citation_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def normalize_time(self):
        for name in ("preprint_at", "updated_at"):
            value = getattr(self, name)
            if value:
                setattr(self, name, value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc))
        return self


def paper_publication(records: list[Record]) -> Publication:
    # Older OpenAlex records guessed publication status from work type alone.
    # Only carry forward the preprint label until those records are refreshed.
    options = [record.publication or Publication(
        status="preprint" if record.source == "arxiv" or record.status == "preprint" else "unknown",
        venue="arXiv" if record.source == "arxiv" else None,
    ) for record in records]
    return min(options, key=lambda item: (list(PUBLICATION_LABELS).index(item.status), item.venue is None),
               default=Publication(status="unknown"))


def publication_label(publication: Publication) -> str:
    label = PUBLICATION_LABELS[publication.status]
    if publication.venue:
        return f"{label} · {publication.venue}"
    if publication.status in {"published", "accepted", "submitted"}:
        return f"{label} · 去向未确认"
    return label


class Page(BaseModel):
    records: list[Record]
    cursor: str | None = None


DATE_BACKFILL_PREFIX = "backfill-range:"


def backfill_dates(kind: str) -> tuple[date, date] | None:
    if not kind.startswith(DATE_BACKFILL_PREFIX):
        return None
    start, through = kind.partition("~")[0].removeprefix(DATE_BACKFILL_PREFIX).partition("@")[0].split(":")
    return date.fromisoformat(start), date.fromisoformat(through)


def backfill_venues(kind: str) -> list[str]:
    venues = kind.partition("~")[0].partition("@")[2]
    return venues.split(",") if kind.startswith(DATE_BACKFILL_PREFIX) and venues else []


def backfill_catalogs(kind: str):
    return tuple(kind.partition("~")[2].split(",")) if kind.startswith(DATE_BACKFILL_PREFIX) and "~" in kind else None


VENUE_ALIASES = {
    "NEURIPS": ("nips", "neural information processing systems"),
    "ICML": ("international conference on machine learning",),
    "ICLR": ("international conference on learning representations",),
    "CVPR": ("conference on computer vision and pattern recognition",),
    "ICCV": ("international conference on computer vision",),
    "ECCV": ("european conference on computer vision",),
    "AAAI": ("aaai conference on artificial intelligence",),
    "IJCAI": ("international joint conference on artificial intelligence",),
    "ACL": ("annual meeting of the association for computational linguistics",),
    "EMNLP": ("conference on empirical methods in natural language processing",),
    "NAACL": ("north american chapter of the association for computational linguistics",),
    "EACL": ("european chapter of the association for computational linguistics",),
    "AISTATS": ("artificial intelligence and statistics",),
    "COLT": ("conference on learning theory",),
    "UAI": ("conference on uncertainty in artificial intelligence",),
    "CORL": ("conference on robot learning",),
    "MLSYS": ("machine learning and systems", "machine learning systems"),
}


def venue_matches(publication: Publication | None, venues: list[str]) -> bool:
    if publication is None or publication.status not in {"published", "accepted"} or not publication.venue:
        return False
    name = re.sub(r"[^a-z0-9]+", " ", publication.venue.casefold()).strip()
    if re.search(r"\b(workshops?|challenge|companion|tutorials?)\b", name):
        return False
    return any(re.search(rf"\b{re.escape(alias)}\b", name)
               for venue in venues for alias in (venue.casefold(), *VENUE_ALIASES.get(venue, ())))
