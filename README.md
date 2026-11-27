# ResearchRadar Agent

ResearchRadar 将按主题检索到的论文、来源、版本和摘要证据保存在本地 SQLite。独立进程生成 Markdown 日报和 7/30/90 日趋势，可向配置的飞书群投递。CLI 和 Streamlit 共用同一任务队列。

## 计算机论文来源

| 来源 | 使用方式 | 日期和状态 |
|---|---|---|
| arXiv、OpenAlex | 日检、日期补录、专题研究 | 分别按首次提交日和索引发表日检索。趋势覆盖沿用这两个来源，便于与历史趋势比较。 |
| Crossref | 日检、日期补录、专题研究 | 收录 DOI 登记的会议与期刊论文，包含 ACM、IEEE 等出版方提交的元数据。链接可跳转到出版方。仅有年或月时保留原有精度。[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) |
| OpenReview | 日检、日期补录、专题研究 | 收录公开论文。录用与正式发表分别标注，公开 PDF 可用于页码证据。[OpenReview 日期字段](https://docs.openreview.net/getting-started/frequently-asked-questions/how-do-i-locate-the-date-a-submission-is-made-public) |
| ACL Anthology | 语言模型、自然语言处理等主题的补录与专题研究 | 从官方 XML 获取 ACL、EMNLP、NAACL、EACL、Findings 和 COLING 合集。保留官方给出的月份或年份及公开 PDF 链接。[ACL Anthology 元数据](https://aclanthology.org/faq/) |
| PMLR | 机器学习相关主题的补录与专题研究 | 读取官方会议卷的引用数据，覆盖所支持的 ICML、AISTATS、COLT、UAI、CoRL、MLSys 等。日期取卷内正式出版元数据。[PMLR](https://proceedings.mlr.press/) |
| DBLP | 可选，补录与专题研究 | 使用官方文献搜索，并通过 Crossref DOI 补齐精确日期。本机测试时官方入口返回访问验证页，因此默认关闭。[DBLP 查询接口](https://dblp.org/faq/How%2Bto%2Buse%2Bthe%2Bdblp%2Bsearch%2BAPI.html) |

默认开启 `RADAR_EXTRA_SOURCES=crossref,openreview,acl,pmlr`。可按需加上 `dblp`，或用空值只使用 arXiv 和 OpenAlex。可选 `RADAR_CROSSREF_MAILTO` 用来设置 Crossref API 联系邮箱。相同 DOI 会合并到同一论文条目，论文库可以按来源筛选。数据源分页或时间达到上限时，运行记录会保留已收录论文并标出未完成的来源。

ACM Digital Library、IEEE Xplore、CVF 和 USENIX 的论文可通过聚合索引和出版元数据命中，覆盖取决于索引和登记情况。Crossref 的 DOI 链接支持核对出版记录，全文可用性取决于来源。[ACM Digital Library](https://dl.acm.org/) · [IEEE Xplore](https://ieeexplore.ieee.org/) · [CVF Open Access](https://openaccess.thecvf.com/) · [USENIX](https://www.usenix.org/conferences)

## 本地运行

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后在项目根目录执行。

```bash
uv python install 3.11
uv sync --locked --extra ui --extra embedding
uv run python -m research_radar.cli seed-topics
uv run python -m research_radar.cli warm-embedding
```

uv 在项目根目录创建可重建的 `.venv`，Python 安装和依赖下载缓存使用 uv 默认的用户目录。示例报告和评测候选保存在 `data/acceptance/`。日常运行生成的数据库和报告也集中在 `data/`，可通过 `RADAR_DATA_DIR` 指定其他位置。

如果本地还没有 `.env`，将 `.env.example` 复制为 `.env`。为双源完整运行配置 `OPENALEX_API_KEY`，为中文摘要配置支持 JSON 对象响应的模型地址、密钥和名称。模型单价以 USD/百万 Token 填写。主题配置货币预算时，系统需要输入和输出单价才能发起模型调用。已配置模型但请求回执丢失的调用会按预留用量记账。

用 DeepSeek 生成论文卡片时，将 `RADAR_LLM_BASE_URL` 设为 `https://api.deepseek.com`，`RADAR_LLM_MODEL` 设为 `deepseek-flash`，并在 `RADAR_LLM_API_KEY` 填写 DeepSeek 密钥。论文卡片使用 JSON 输出和非思考模式。修改凭据后重启桌宠或调度进程。[DeepSeek 模型与接口](https://api-docs.deepseek.com/quick_start/pricing/) · [JSON 输出文档](https://api-docs.deepseek.com/guides/json_mode/)

### 桌宠与网页原型

```bash
uv sync --locked --extra ui
uv run python -B -m research_radar.desktop --open-web
```

桌宠启动时出现在屏幕右下角。Windows 上使用透明背景，可以直接拖动角色。单击角色展开或收起操作面板，从面板可以切换主题、触发检索和查看日报。状态灯随检索进度变化。右键菜单可以打开研究概览、减少动效或退出。启动时加 `--panel` 可以直接展开面板。

桌宠会启动本地网页和检索进程。桌宠运行期间，已关注的主题每天自动检索，也可手动提交检索和补录。关闭桌宠会关闭它启动的进程。Windows 上可用 `uv run python -m tkinter` 检查 Python 是否包含桌面组件。

桌宠和网页共用原创雷达机器人素材。角色常驻、按需展开操作的思路参考了开源桌宠。[VPet](https://github.com/LorisYounger/VPet) · [oneko.js](https://github.com/adryd325/oneko.js)

<details>
<summary>角色素材生成记录</summary>

使用内置图像生成制作透明 PNG，保存为 `research_radar/pet-mascot.png`。旧版机器人图标来自 [Twemoji](https://github.com/jdecked/twemoji/blob/v16.0.1/assets/72x72/1f916.png)，采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。

```text
Use case: stylized-concept. Asset type: a single desktop pet sprite for a scholarly research assistant, small on-screen size about 110 px. Create an ORIGINAL delightful little hovering research robot, perfectly legible at thumbnail scale, like a collectible companion rather than an application icon. Front three-quarter seated floating pose, round compact silhouette, off-white ceramic shell, dark ink navy wraparound face display with two expressive mint-green eyes and a small smile, a short mint-tipped antenna, tiny floating arms, hugging a small ivory paper with two simple green lines (not letters). Cozy sophisticated gaming sprite, clean soft-shaded 2.5D proportions with crisp dark contour and tactile ceramic finish, no resemblance to existing mascots or brand marks. Entire figure including antenna and lower legs fully visible. Center the complete figure, fill about 82% of square frame. Actual transparent alpha background. No backing circle, base, platform, scenery, ground shadow, glow beyond the outline, UI, text, border, watermark or multiple poses.
```

</details>

### 命令行与常驻调度

先手动跑一次检查报告，再启动调度和网页。第一次手动运行将打印运行 ID 与状态，日报位于 `data/reports/<主题>/<本地日期>.md`。

```bash
uv run python -m research_radar.cli run-daily industrial_3d_ad --wait
uv run python -m research_radar.cli runs
uv run python -m research_radar.cli scheduler
```

另开一个终端运行网页，在浏览器打开 `http://127.0.0.1:8501`。

```bash
uv run python -m streamlit run streamlit_app.py --server.address=127.0.0.1 --browser.gatherUsageStats=false
```

默认每日 09:05（Asia/Hong_Kong）。调度器为已有日报任务的每个主题依次建立最近 90 日与之前 90 日的回填任务，并每周刷新近 90 日的 arXiv 版本及近 30 日的 OpenAlex 数据。可先在网页暂停示例中暂时用不到的主题。历史任务和通知均有独立状态。缺少 OpenAlex 凭据时，日报会显示该数据源失败。缺少模型时，卡片显示原摘要摘句。临时只跑关键词时可设置 `RADAR_KEYWORD_ONLY=true`。

## 管理主题和运行

在网页「主题管理」填写一句研究方向，DeepSeek 会生成主题名称、核心检索词和补充词。确认前可以核对它们，也可以在「高级设置」修改。这里也可以搜索、编辑和删除主题。删除后的主题可以在「已删除」中恢复。

「关注」开启每日自动检索。取消关注后，主题仍可手动检索和补录。CLI 中的 `pause` 和 `resume` 对应取消关注和关注。

在「论文库」选中主题，展开「按日期补录论文」，输入起止日期后点击「开始补录」。起止日期都包含在检索范围内，重复论文会合并。进度和各来源的结果可在「运行状态」查看；补录会把论文放进所选主题库。

论文库和日报显示已发表、已录用、已投稿、预印本或待确认状态，并在来源提供时显示期刊或会议名称。发表去向采用来源记录。运行 `uv run python -m research_radar.cli refresh-publications` 可以补查现有论文的状态，已有摘要和推荐顺序会保留。

```bash
uv run python -m research_radar.cli topic industrial_3d_ad
uv run python -m research_radar.cli backfill industrial_3d_ad --days 180 --wait
uv run python -m research_radar.cli run RUN_ID
uv run python -m research_radar.cli report RUN_ID
uv run python -m research_radar.cli papers --topic industrial_3d_ad --query "point cloud"
uv run python -m research_radar.cli retry-run RUN_ID
```

`save-topic PATH --revision N` 导入主题 JSON，编辑时使用 `topic` 输出的版本号，并将其中的 `topic` 对象写到文件。可用 `python -m research_radar.cli --help` 查看全部命令。已完成日报的同日手动触发会返回现有运行 ID。重试部分日报会续跑未完成的扫描和更新所属日报，已投递的通知沿用其送达记录。

飞书通过 `FEISHU_WEBHOOK_URL` 配置。群机器人启用签名后同时填写 `FEISHU_WEBHOOK_SECRET`。运行状态页显示待发送、已发送、失败和待确认。处理「待确认」时先查看目标群，再从页面提交重发或执行 `retry-notification RUN_ID`。先用测试群确认收件，再换正式群。

## 对话助手与专题研究

从网页左侧选择「对话助手」，或点击桌宠面板的「打开对话」。对话历史保存在本地 SQLite，可新建会话或继续上次的内容。对话使用现有模型配置，所选模型需支持 Chat Completions 工具调用。

- 「查三维工业异常检测最近 30 天的论文」「收藏第二篇」可以连续使用。助手通过实际返回的论文列表解析编号。
- 「检索一次当前主题」会返回任务卡，显示排队、处理进度和结果。编辑主题、按日期补录和重发失败通知会先显示确认卡。
- 可以在一句话里要求新建主题并补录某个日期范围的会议论文。确认卡会列出日期和会议，后台仅收录来源记录为已发表或已录用、且会议匹配的论文。
- 「做个专题研究，比较近一年使用 MVTec 3D-AD 的论文如何计算 AUROC」会交给后台调度器。专题会主动查论文库和相关的学术来源，按需读取公开 PDF。报告可以从对话或运行中心查看和下载。
- 对话会实时更新当前步骤，结束后可展开「任务时间线」查看模型耗时和用量、工具结果、确认回执。日报补录和专题另有来源命中量及失败、截断记录。
- 专题运行中可以输入「只看 ICML 主会」「优先查证数据集划分」「优先读 2410.12345」，也可以通过任务卡提交调整。系统会在当前步骤结束后继续原任务，并在报告里列出实际应用的调整。主会筛选依据已录用或发表的来源记录。
- 在「主题管理」编辑主题时，可以保存常用会议、排除词、数据集和研究口径。选择同一主题的会话共享这份项目记忆。对话时间线和专题报告会显示实际使用的版本，运行中的专题保留提交时的版本。

专题默认最多两轮外部检索。运行中添加查证重点时，可在同一时间和模型预算内申请最多两轮额外补查。每次最多筛查 40 篇候选、尝试读取 5 篇公开全文。PDF 链接来自 arXiv 或标有开放获取的受支持学术站点。能定位的引用附 PDF 页码。全文不可得时，报告标明摘要证据与缺口。关闭网页后，已提交任务由正在运行的调度进程继续执行。

普通对话每轮默认最多 9 次模型调用、120 秒，可通过 `RADAR_CHAT_MAX_CALLS` 和 `RADAR_CHAT_MAX_SECONDS` 调整。达到上限时，对话会保留已查到的结果和已生成的确认卡。

## Linux 部署与恢复

单机运行一个调度进程。Compose 共享数据卷，网页只绑定服务器回环地址。部署前完成 `.env` 的配置。

```bash
docker compose build scheduler
docker compose run --rm scheduler python -m research_radar.cli warm-embedding
docker compose run --rm scheduler python -m research_radar.cli seed-topics
docker compose up -d
ssh -N -L 8501:127.0.0.1:8501 user@your-server
```

通过 SSH 隧道在本地打开 `http://127.0.0.1:8501`。备份时先停止调度器，执行 `docker compose run --rm scheduler python -m research_radar.cli backup /data/radar.backup.sqlite3`，再将备份复制到卷外并恢复调度器。CLI 会验证备份数据库完整性。恢复时停止两个服务，在数据卷保留原数据库及其 WAL/SHM 文件作为回退副本，把已验证的备份放到 `/data/radar.sqlite3`，再运行 `restore-reports` 用数据库重建 Markdown 并启动服务。先在独立数据卷演练，恢复已有通知记录后检查待发送项。

## 测试与评测

```bash
uv run python -B -m pytest -q -p no:cacheprovider
uv run python -B -m research_radar.evaluation replay-nas
uv run python -B -m research_radar.evaluation audit
uv run python -m research_radar.evaluation freeze industrial_3d_ad \
  --from 2026-08-28 --through 2026-11-25 data/eval/industrial.json
uv run python -m research_radar.evaluation compare \
  data/eval/industrial.json data/eval/industrial-results.json
```

测试数据库使用系统临时目录。评测前先完成指定窗口的历史回填。冻结候选后，将 `candidates` 中的 `relevant` 填为人工标注的布尔值。召回评估需要独立检索得到的对照文献，补入候选文件并标记 `retrieved: false`。比较命令按同一候选集输出关键词和 Embedding 排名，带 `--llm` 时加入模型重排并记录费用。候选不足和标签未完成时，结果会记录实际数量或空指标。

`replay-nas` 使用固定模型回复和来源，在临时数据库复现主题确认、重复点击和指定会议补录。`audit` 用既有候选和固定标注重新计算前 10 篇排序准确率，并核对摘要摘句能否定位到原文。

本轮实测数据和发布前待验项见 [验收记录](ACCEPTANCE.md)。
