# ResearchRadar

ResearchRadar 是一个单用户科研文献追踪工具。它按主题检索论文，合并跨来源记录和版本，在本地 SQLite 保存论文、证据和任务状态。React 网页用于查看论文库、简报、趋势和专题研究，Windows 桌宠可以启动本地服务和打开网页。

## 快速开始

准备 Python 3.11、[uv](https://docs.astral.sh/uv/) 和 Node.js 22.12 或更高的 22.x 版本。在项目根目录执行。

```bash
uv python install 3.11
uv sync --locked --extra ui --extra embedding
npm --prefix web ci
npm --prefix web run build
uv run python -m research_radar.cli seed-topics
```

`ui` 安装 Windows 桌宠所需的 PySide6。只运行网页和后台调度时，可以省略 `--extra ui`。论文库、模型权重和生成的报告默认保存在 `data/`，可通过 `RADAR_DATA_DIR` 指定数据目录。

创建项目根目录的 `.env` 时，以 `.env.example` 为模板。

| 配置 | 用途 |
| --- | --- |
| `OPENALEX_API_KEY` | 启用 OpenAlex 检索。 |
| `RADAR_LLM_BASE_URL`、`RADAR_LLM_API_KEY`、`RADAR_LLM_MODEL` | 启用模型摘要和对话。论文卡片需要 JSON 对象输出，对话需要 Chat Completions 工具调用。 |
| `RADAR_LLM_INPUT_PRICE`、`RADAR_LLM_OUTPUT_PRICE` | 每百万 Token 的美元单价。为主题设置货币预算时，需要配置两项单价。 |
| `FEISHU_WEBHOOK_URL`、`FEISHU_WEBHOOK_SECRET` | 可选。配置飞书投递，机器人启用签名时同时配置签名密钥。 |
| `RADAR_API_TOKEN` | FastAPI 的 Bearer Token。服务绑定到非回环地址时，需配置此项才能访问业务接口。 |

使用本地 BGE 语义排序前，执行以下命令下载权重。不配置语义模型时，排序回退到关键词。也可以使用 `RADAR_KEYWORD_ONLY=true` 固定采用关键词排序。

```bash
uv run python -m research_radar.cli warm-embedding
```

### Windows 桌宠

双击 `launch_radar.bat`，或在项目根目录执行以下命令。桌宠启动后会出现在主屏幕右下角，并启动后台调度和本地网页服务。点击角色展开面板，加上 `--panel` 可以在启动时展开面板。

```bash
uv run python -B -m research_radar.desktop --open-web
```

桌宠使用 `scheduler --daily-only`，为关注的主题安排每日检索，也可处理手动任务和专题研究。自动历史回填、来源维护、自动周期报告和自动飞书投递需要下文的完整调度器。同一数据目录运行一个调度器。

### 网页和完整调度

分别在两个终端运行以下命令，然后打开浏览器访问 `http://127.0.0.1:8000`。FastAPI 同时提供 API 和构建好的 React 网页。

```bash
uv run python -m research_radar.cli scheduler
```

```bash
uv run python -m research_radar.cli serve-api --host 127.0.0.1 --port 8000
```

开发前端时可以额外执行 `npm --prefix web run dev`，再访问 `http://localhost:5173`。Vite 会将 API 请求代理到本地 FastAPI。

## 使用方式

- 在「设置与运行」创建和关注研究主题，配置检索词、数据源和研究口径。默认在每天 09:05（Asia/Hong_Kong）调度关注主题。
- 在「文献工作台」搜索或收藏论文，按日期补录，检查公开 PDF 和摘要证据。arXiv 和 OpenAlex 是核心来源，Crossref、OpenReview、ACL Anthology、PMLR 等扩展来源按任务和主题启用。趋势覆盖沿用 arXiv 和 OpenAlex。
- 在「今日简报」查看每日推荐，在「趋势与热点」查看滚动窗口和周期报告。报告在 SQLite 留存，也导出到 `data/reports/`。
- 在「对话助手」检索或收藏论文，提交专题研究和调整查证范围。敏感操作会显示确认卡，后台任务可在网页关闭后继续运行，前提是调度器仍在运行。
- 在「深度研究与项目」查看项目档案、方法比较和 Research Gap 反向查证。可配置 Zotero 同步，或使用 `uv run python -m research_radar.cli mcp` 启动 MCP 服务。

常用命令如下。完整列表见 `uv run python -m research_radar.cli --help`。

```bash
uv run python -m research_radar.cli run-daily industrial_3d_ad --wait
uv run python -m research_radar.cli runs
uv run python -m research_radar.cli backfill industrial_3d_ad --days 180 --wait
uv run python -m research_radar.cli papers --topic industrial_3d_ad --query "point cloud"
```

## Linux Docker 部署

在项目根目录准备 `.env`，设置 `RADAR_API_TOKEN`。Compose 构建 React 网页，将它交给 FastAPI 提供，并为网页和调度器挂载同一数据卷。主机端口只绑定回环地址。

```bash
docker compose build scheduler
docker compose run --rm scheduler python -m research_radar.cli seed-topics
docker compose run --rm scheduler python -m research_radar.cli warm-embedding
docker compose up -d
ssh -N -L 8000:127.0.0.1:8000 user@your-server
```

通过 SSH 隧道打开 `http://127.0.0.1:8000`，在网页输入 `RADAR_API_TOKEN`。容器的启动配置来自宿主机的 `.env`。修改后运行 `docker compose up -d --force-recreate` 使配置生效。

旧部署迁移时，在同一目录以 `docker compose up -d --remove-orphans` 启动新版，可移除旧网页容器并沿用原有卷。备份时先停止调度器，使用 `docker compose run --rm scheduler python -m research_radar.cli backup /data/radar.backup.sqlite3` 并将备份复制到卷外。恢复时先停止网页和调度器，保存现有 SQLite 及 WAL/SHM 文件，再用已验证的备份恢复数据库。可运行 `restore-reports` 从数据库重建 Markdown 报告。

## 测试

```bash
uv run --locked --extra ui python -B -m pytest -q -p no:cacheprovider
npm --prefix web ci
npm --prefix web run build
```
