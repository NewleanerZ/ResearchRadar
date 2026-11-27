"""Local configuration, secret masking and embedding setup."""

import importlib.util
import os
from pathlib import Path
import re

from .models import Settings
from .ranking import MODEL
from .runtime_paths import app_dir

ENV_PATH = app_dir() / ".env"

SECRET_SPECS = [
    ("OPENALEX_API_KEY", "OpenAlex API Key", "提升 OpenAlex 检索稳定性和配额"),
    ("RADAR_LLM_API_KEY", "LLM API Key", "用于论文卡片、对话助手与精读"),
    ("FEISHU_WEBHOOK_URL", "飞书 Webhook URL", "https://open.feishu.cn/open-apis/bot/v2/hook/..."),
    ("FEISHU_WEBHOOK_SECRET", "飞书签名密钥", "群机器人开启签名校验时填写"),
    ("S2_API_KEY", "Semantic Scholar API Key", "可选，提高 Semantic Scholar 配额"),
    ("GITHUB_TOKEN", "GitHub Token", "可选，提高 GitHub 代码动态限流配额"),
    ("HF_TOKEN", "Hugging Face Token", "可选，访问受限模型/数据集时填写"),
    ("ZOTERO_API_KEY", "Zotero API Key", "用于 Zotero 文献导入与推送"),
    ("RADAR_API_TOKEN", "HTTP API Bearer Token", "启动 serve-api 时的强制鉴权凭据"),
]


def mask_secret(value: str) -> str:
    clean = (value or "").strip()
    if not clean:
        return "未配置"
    if len(clean) <= 6:
        return "已配置"
    return f"已配置（••••{clean[-4:]}）"


def _sanitize_env_value(value: str) -> str:
    return re.sub(r"[\r\n\x00]+", " ", value).strip()


def _format_env_value(value: str) -> str:
    clean = _sanitize_env_value(value)
    if any(ch in clean for ch in (" ", "#", '"', "'")) or clean == "":
        escaped = clean.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return clean


def save_env_updates(env_path: Path, updates: dict[str, str], clear_keys: set[str] | None = None) -> None:
    """Safely update keys in .env atomically and mirror them into os.environ."""
    clear_keys = clear_keys or set()
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = {k: _sanitize_env_value(v) for k, v in updates.items() if k not in clear_keys}
    new_lines = []
    seen = set()
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _, _ = line.partition("=")
        key = key.strip()
        if key in clear_keys:
            os.environ.pop(key, None)
            continue
        if key in remaining:
            new_lines.append(f"{key}={_format_env_value(remaining[key])}")
            os.environ[key] = remaining[key]
            seen.add(key)
        else:
            new_lines.append(line)
    for key, val in remaining.items():
        if key not in seen and val != "":
            new_lines.append(f"{key}={_format_env_value(val)}")
            os.environ[key] = val
        elif key not in seen:
            os.environ[key] = ""
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    tmp.replace(env_path)


def embedding_diagnostics(settings: Settings) -> dict:
    """Inspect why local BGE semantic embeddings are ready or unavailable."""
    model_dir = settings.embedding_path()
    has_weights = (model_dir / "model.safetensors").exists() or (model_dir / "pytorch_model.bin").exists()
    has_pkg = importlib.util.find_spec("sentence_transformers") is not None
    ready = (not settings.keyword_only) and has_pkg and has_weights
    reasons = []
    if settings.keyword_only:
        reasons.append("已开启 RADAR_KEYWORD_ONLY=true（强制仅使用关键词）")
    if not has_pkg:
        reasons.append("当前环境未安装 sentence-transformers")
    if not has_weights:
        reasons.append(f"本地尚未下载 BGE 权重目录（{model_dir}）")
    return {
        "ready": ready,
        "has_weights": has_weights,
        "has_pkg": has_pkg,
        "model_dir": model_dir,
        "reasons": reasons,
    }


def warm_embedding_weights(settings: Settings, hf_endpoint: str = "") -> str:
    """Download pinned BAAI/bge-small-en-v1.5 weights into settings.embedding_path()."""
    from huggingface_hub import snapshot_download

    endpoint = _sanitize_env_value(hf_endpoint)
    kwargs = {
        "revision": settings.embedding_revision,
        "local_dir": settings.embedding_path(),
        "max_workers": 2,
        "allow_patterns": ["*.json", "*.txt", "model.safetensors", "1_Pooling/*"],
    }
    if endpoint:
        if not re.fullmatch(r"https://[^\s]+", endpoint):
            raise ValueError("Hugging Face 镜像地址必须以 https:// 开头")
        kwargs["endpoint"] = endpoint.rstrip("/")
    return str(snapshot_download(MODEL, **kwargs))
