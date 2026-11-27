"""Streamlit System Configuration page and safe .env persistence."""

import importlib.util
import os
from pathlib import Path
import re

import streamlit as st

from .mcp_client import add_mcp_server, discover_mcp_tools, list_mcp_servers, remove_mcp_server, set_mcp_server_enabled
from .models import EXTRA_SOURCES, SOURCE_LABELS, Settings
from .ranking import MODEL
from .ui_theme import status_pill

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

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


def settings_page(store, _topics, *, env_path: Path = ENV_PATH) -> None:
    st.title("系统配置")
    settings = Settings.from_env()
    diag = embedding_diagnostics(settings)

    st.subheader("运行与模型自检")
    pills = "".join([
        status_pill("BGE 模型可用" if diag["ready"] else "BGE 模型未就绪",
                    "success" if diag["ready"] else "warning", "check" if diag["ready"] else "alert"),
        status_pill(f"LLM · {settings.llm_model}" if settings.llm_model else "LLM 未配置",
                    "primary" if settings.llm_model else "warning", "spark" if settings.llm_model else "alert"),
        status_pill("OpenAlex 已配置" if settings.openalex_key.get_secret_value() else "OpenAlex 未配置",
                    "accent" if settings.openalex_key.get_secret_value() else "neutral", "book"),
        status_pill("飞书通知已配置" if settings.feishu_webhook.get_secret_value() else "飞书通知未配置",
                    "success" if settings.feishu_webhook.get_secret_value() else "neutral", "check"),
    ])
    st.html(f'<div class="rr-pill-row">{pills}</div>')
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("本地语义模型 (BGE)", "可用" if diag["ready"] else "不可用")
    c2.metric("LLM 模型", settings.llm_model or "未配置")
    c3.metric("OpenAlex", "已配置" if settings.openalex_key.get_secret_value() else "未配置")
    c4.metric("飞书通知", "已配置" if settings.feishu_webhook.get_secret_value() else "未配置")

    if not diag["ready"]:
        st.warning("语义模型不可用原因：" + "；".join(diag["reasons"]))
    else:
        st.success(f"语义模型已就绪：{diag['model_dir']}")

    with st.expander("下载 / 修复本地 BGE 语义模型（约 130MB）", expanded=not diag["ready"]):
        st.caption("本地聚类（动态热点）、混合 RAG 语义检索及 B1 论文排序依赖 `BAAI/bge-small-en-v1.5` 本地权重。")
        mirror = st.text_input(
            "Hugging Face 下载地址（国内网络可填 https://hf-mirror.com，留空走官方源）",
            value=os.getenv("HF_ENDPOINT", ""),
            placeholder="https://hf-mirror.com",
        )
        if st.button("立即下载 BGE 模型权重", type="primary", disabled=not diag["has_pkg"]):
            try:
                with st.spinner("正在下载 BAAI/bge-small-en-v1.5 权重，请稍候…"):
                    target = warm_embedding_weights(settings, mirror)
                if mirror.strip():
                    save_env_updates(env_path, {"HF_ENDPOINT": mirror.strip()})
                st.success(f"模型下载完成：{target}")
                st.rerun()
            except Exception as err:
                st.error(f"下载失败：{err}")

    tab_llm, tab_sources, tab_mcp, tab_secrets = st.tabs([
        "模型与排序参数", "数据源与外部集成", "外部 MCP 工具", "API 密钥与通知凭据"
    ])

    with tab_llm:
        with st.form("cfg_llm_form"):
            base_url = st.text_input("LLM Base URL", value=settings.llm_base_url)
            model_name = st.text_input("LLM 模型名称（如 deepseek-flash / gpt-4o-mini）", value=settings.llm_model)
            t1, t2 = st.columns(2)
            think_levels = ["off", "low", "medium", "high"]
            thinking = t1.selectbox(
                "深度思考等级（Thinking Level）",
                options=think_levels,
                index=think_levels.index(settings.thinking_level),
                format_func=lambda v: {"off": "off（关闭，响应最快）", "low": "low（轻度推理）",
                                       "medium": "medium（平衡推理）", "high": "high（深度推理）"}[v],
            )
            stream_on = t2.checkbox("启用流式输出（Streaming）", value=settings.stream_chat)
            p1, p2 = st.columns(2)
            in_price = p1.text_input("输入单价（USD / 百万 Token，可选）",
                                     value="" if settings.input_price is None else str(settings.input_price))
            out_price = p2.text_input("输出单价（USD / 百万 Token，可选）",
                                      value="" if settings.output_price is None else str(settings.output_price))
            k1, k2 = st.columns(2)
            keyword_only = k1.checkbox("仅使用关键词排序（禁用本地向量模型）", value=settings.keyword_only)
            rerank_on = k2.checkbox("启用 Cross-Encoder 重排（B2，需额外权重）", value=settings.reranker_enabled)
            rerank_model = st.text_input("Cross-Encoder 模型名", value=settings.reranker_model)
            c_calls = st.number_input("单轮对话最大模型调用次数", min_value=2, max_value=16, value=settings.chat_max_calls)
            c_secs = st.number_input("单轮对话超时（秒）", min_value=15, max_value=240, value=settings.chat_max_seconds)
            if st.form_submit_button("保存模型与排序配置", type="primary"):
                updates = {
                    "RADAR_LLM_BASE_URL": base_url.strip(),
                    "RADAR_LLM_MODEL": model_name.strip(),
                    "RADAR_THINKING_LEVEL": thinking,
                    "RADAR_STREAM_CHAT": "true" if stream_on else "false",
                    "RADAR_LLM_INPUT_PRICE": in_price.strip(),
                    "RADAR_LLM_OUTPUT_PRICE": out_price.strip(),
                    "RADAR_KEYWORD_ONLY": "true" if keyword_only else "false",
                    "RADAR_RERANKER_ENABLED": "true" if rerank_on else "false",
                    "RADAR_RERANKER_MODEL": rerank_model.strip(),
                    "RADAR_CHAT_MAX_CALLS": str(int(c_calls)),
                    "RADAR_CHAT_MAX_SECONDS": str(int(c_secs)),
                }
                clear = {k for k in ("RADAR_LLM_INPUT_PRICE", "RADAR_LLM_OUTPUT_PRICE") if not updates[k]}
                save_env_updates(env_path, updates, clear_keys=clear)
                st.success("已写入 .env 并生效；如修改了调度器所需凭据，建议重启桌宠/调度进程。")
                st.rerun()

    with tab_sources:
        with st.form("cfg_sources_form"):
            current_extra = [s.strip() for s in settings.extra_sources.split(",") if s.strip() in EXTRA_SOURCES]
            selected_extra = st.multiselect(
                "启用的扩展学术数据源（arXiv 与 OpenAlex 默认开启）",
                options=list(EXTRA_SOURCES),
                default=current_extra,
                format_func=lambda k: SOURCE_LABELS.get(k, k),
            )
            crossref_mailto = st.text_input("Crossref 联系邮箱（可选）", value=settings.crossref_mailto)
            page_limit = st.number_input("单源单次最大分页数", min_value=1, max_value=300, value=settings.page_limit)
            zotero_uid = st.text_input("Zotero User ID（纯数字，可选）", value=settings.zotero_user_id)
            a1, a2 = st.columns(2)
            api_host = a1.text_input("HTTP API 绑定地址", value=settings.api_host)
            api_port = a2.number_input("HTTP API 端口", min_value=1, max_value=65535, value=settings.api_port)
            if st.form_submit_button("保存数据源与服务配置", type="primary"):
                save_env_updates(env_path, {
                    "RADAR_EXTRA_SOURCES": ",".join(selected_extra),
                    "RADAR_CROSSREF_MAILTO": crossref_mailto.strip(),
                    "RADAR_PAGE_LIMIT": str(int(page_limit)),
                    "ZOTERO_USER_ID": zotero_uid.strip(),
                    "RADAR_API_HOST": api_host.strip(),
                    "RADAR_API_PORT": str(int(api_port)),
                })
                st.success("已保存至 .env。")
                st.rerun()

    with tab_mcp:
        st.caption("为对话助手挂载外部 stdio MCP Server，挂载后可在对话中使用外部学术或本地工具。")
        with st.form("cfg_mcp_add"):
            m_name = st.text_input("服务名称（英文/数字，如 zotero_local）")
            m_cmd = st.text_input("启动命令（如 python 或 npx）")
            m_args = st.text_input("命令参数（用空格分隔，可选）")
            m_env = st.text_input("允许透传的环境变量名（逗号分隔，可选）")
            if st.form_submit_button("添加外部 MCP 服务", type="primary"):
                try:
                    add_mcp_server(
                        store, m_name, m_cmd,
                        args=[x for x in m_args.split() if x],
                        env_keys=[x.strip() for x in m_env.split(",") if x.strip()],
                    )
                    st.success(f"已添加 MCP 服务「{m_name}」")
                    st.rerun()
                except ValueError as err:
                    st.error(str(err))
        for srv in list_mcp_servers(store):
            with st.container(border=True):
                c_info, c_act1, c_act2, c_act3 = st.columns([3, 1, 1, 1])
                c_info.markdown(f"**{srv['name']}** · `{srv['command']} {' '.join(srv['args'])}`")
                if c_act1.button("探测工具", key=f"mcp_probe_{srv['id']}"):
                    try:
                        tools = discover_mcp_tools(srv)
                        st.info("可用工具：" + ("、".join(t["name"] for t in tools) or "无"))
                    except Exception as err:
                        st.warning(f"连接失败：{err}")
                if c_act2.button("禁用" if srv["enabled"] else "启用", key=f"mcp_tog_{srv['id']}"):
                    set_mcp_server_enabled(store, srv["id"], not srv["enabled"])
                    st.rerun()
                if c_act3.button("删除", key=f"mcp_del_{srv['id']}"):
                    remove_mcp_server(store, srv["id"])
                    st.rerun()

    with tab_secrets:
        st.caption("出于安全考虑，页面不回显已保存的明文密钥。如需更新请直接输入新值；勾选“清除”可删除对应密钥。")
        with st.form("cfg_secrets_form"):
            inputs = {}
            clears = set()
            for env_key, label, help_text in SECRET_SPECS:
                cur_val = os.getenv(env_key, "")
                col_in, col_clr = st.columns([4, 1], vertical_alignment="bottom")
                inputs[env_key] = col_in.text_input(
                    f"{label}（当前：{mask_secret(cur_val)}）",
                    value="",
                    type="password",
                    placeholder=help_text,
                    key=f"secret_in_{env_key}",
                )
                if col_clr.checkbox("清除", key=f"secret_clr_{env_key}"):
                    clears.add(env_key)
            if st.form_submit_button("保存密钥配置", type="primary"):
                to_save = {k: v for k, v in inputs.items() if v.strip()}
                if to_save or clears:
                    save_env_updates(env_path, to_save, clear_keys=clears)
                    st.success("密钥已安全更新到 .env。后台调度进程将在下次重启后加载最新密钥。")
                    st.rerun()
                else:
                    st.info("未检测到新的密钥变更。")
