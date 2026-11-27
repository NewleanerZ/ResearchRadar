import base64
from datetime import timedelta
import hashlib
import hmac
import html
import re
from urllib.parse import urlsplit

import httpx

from .models import Publication, publication_label, utc_now
from .sources import retry_delay
from .store import row, write


def md(value) -> str:
    safe = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#|!])", r"\\\1", safe)


def render_report(topic, run, result, trend):
    lines = [f"# {md(topic.name)} · 文献日报", "", f"{run['local_day']} · {md(topic.timezone)}", "",
             "## 数据来源", ""]
    for name, info in result["sources"].items():
        state = {"complete": "完成", "failed": "失败", "truncated": "截断"}[info["status"]]
        reason = f" · {md(info['detail'])}" if info.get("detail") else ""
        lines.append(f"- {name} · {state} · {info['start']} 至 {info['end']} 零点"
                     f" · {info['received']} 条{reason}")
    lines.extend(["", f"去重后待评估 {result['candidates']} 篇，达到相关性阈值 {result['eligible']} 篇。"])
    if result["warnings"]:
        lines.extend(["", *[f"- {md(warning)}" for warning in result["warnings"]]])
    lines.extend(["", "## 重点论文", ""])
    if not result["recommendations"]:
        lines.append("当前覆盖范围内没有待推荐的论文。" if all(
            entry["status"] == "complete" for entry in result["sources"].values())
            else "数据源覆盖不完整，暂未生成推荐。")
    for item in result["recommendations"]:
        publication = Publication(status=item["status"], venue=item.get("venue"))
        lines.extend([f"### {md(item['title'])}", "",
                      f"{md('、'.join(item['authors']) or '作者未确认')} · "
                      f"{item.get('published') or '日期未确认'} · 相关性 {item['components']['relevance']:.2f}", "",
                      f"**发表状态** · {md(publication_label(publication))}", ""])
        lines.append(" · ".join(f"[{link['source']}]({link['url']})" for link in item["links"]))
        card = item["card"]
        analysis = card.get("analysis")
        if analysis:
            lines.extend(["", md(analysis["summary"]["statement"])])
            if analysis["problem"]:
                lines.append(f"- 研究问题 · {md(analysis['problem']['statement'])}")
            if analysis["method"]:
                lines.append(f"- 方法 · {md(analysis['method']['statement'])}")
            lines.extend(f"- 作者主张 · {md(claim['statement'])}" for claim in analysis["author_claims"])
            if analysis["inference"]:
                lines.append(f"- 主题关联 · {md(analysis['inference'])}")
        else:
            lines.extend(["", f"摘要卡片状态 · {md(card['reason'])}"])
        if card["evidence"]:
            lines.extend(["", *[f"> {md(evidence['text'])}" for evidence in card["evidence"]]])
        lines.append("")
    lines.extend(["## 主题趋势", "", f"查询版本 `{run['version']}`，截至 {run['local_day']} 当地零点。", "",
                  "| 子主题 | 窗口 | 篇数 | 上一窗口 | 平滑增长 | 份额变化 | 状态 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"])
    for item in trend["windows"]:
        growth = f"{item['growth']:+.2f}" if item["growth"] is not None else "—"
        delta = f"{item['delta']:+.1%}" if item["delta"] is not None else "—"
        state = "升温" if item["heating"] else ("可比较" if item["comparable"] else "覆盖不足")
        prior = item["prior"] if item["prior"] is not None else "—"
        lines.append(f"| {md(item['label'])} | {item['days']} 日 | {item['count']} | {prior} | {growth} | {delta} | {state} |")
    return "\n".join(lines) + "\n"


def notification_text(topic, run, result):
    items = result["recommendations"]
    coverage = "、".join(f"{source} {info['status']}" for source, info in result["sources"].items())
    parts = [f"{topic.name} · {run['local_day']} 文献日报", coverage]
    for item in items:
        label = item["card"].get("analysis")
        summary = label["summary"]["statement"][:220] if label else ""
        parts.extend([item["title"][:300], summary, item["links"][0]["url"]])
    if not items:
        parts.append("当前覆盖范围内没有待推荐的论文。" if all(
            entry["status"] == "complete" for entry in result["sources"].values())
            else "数据源覆盖不完整，暂未生成推荐。")
    return "\n".join(part for part in parts if part)


def retry_notification(store, run_id, *, connection=None):
    statement = """UPDATE notifications SET status='pending',detail=NULL,next_at=:now,attempts=0,manual_retry=1
                   WHERE run_id=:id AND status IN ('uncertain','failed')"""
    params = {"id": run_id, "now": utc_now().isoformat()}
    changed = store.execute(statement, **params) if connection is None else write(connection, statement, **params).rowcount
    if not changed:
        raise ValueError("仅可在核对后重发状态为失败或待确认的通知")


def send_one(store, settings, client, *, manual_only=False):
    webhook = settings.feishu_webhook.get_secret_value()
    if not webhook:
        return False
    url = urlsplit(webhook)
    if url.scheme != "https" or url.hostname != "open.feishu.cn" or not re.fullmatch(
            r"/open-apis/bot/v2/hook/[\w-]+", url.path) or url.username or url.query or url.fragment or url.port:
        raise ValueError("飞书 Webhook 地址格式无效")
    now = utc_now()
    with store.transaction() as con:
        job = row(con, """SELECT * FROM notifications WHERE status='pending' AND next_at<=:now
                        AND (NOT :manual OR manual_retry) ORDER BY next_at,run_id LIMIT 1""",
                  now=now.isoformat(), manual=manual_only)
        if not job:
            return False
        attempt = job["attempts"] + 1
        write(con, "UPDATE notifications SET status='sending',attempts=:count WHERE run_id=:id",
              count=attempt, id=job["run_id"])
    payload = {"msg_type": "text", "content": {"text": job["payload"][:3800]}}
    secret = settings.feishu_secret.get_secret_value()
    if secret:
        timestamp = str(int(now.timestamp()))
        key = f"{timestamp}\n{secret}".encode()
        payload.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
    wait = 0
    try:
        response = client.post(webhook, json=payload, timeout=12)
        try:
            result = response.json()
        except ValueError:
            result = {}
        code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
        if response.status_code == 200 and code == 0:
            status, detail = "sent", None
        elif response.status_code == 429 or code in {11232, 9499}:
            status, detail = ("pending" if attempt < 3 else "failed"), "rate_limit"
            wait = retry_delay(response, 30 * (2 ** (attempt - 1)))
        elif 400 <= response.status_code < 500:
            status, detail = "failed", f"http_{response.status_code}"
        elif response.status_code == 200 and type(code) is int:
            status, detail = "failed", f"feishu_{code}"
        else:
            status, detail = "uncertain", f"http_{response.status_code}"
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        status, detail = ("pending" if attempt < 3 else "failed"), "connection_error"
        wait = 30 * (2 ** (attempt - 1))
    except httpx.RequestError:
        status, detail = "uncertain", "response_unconfirmed"
    store.execute("UPDATE notifications SET status=:state,next_at=:next,detail=:detail WHERE run_id=:id",
                  state=status, next=(now + timedelta(seconds=min(wait, 86400))).isoformat(),
                  detail=detail, id=job["run_id"])
    return True


def send_event_alerts(store, settings, client) -> int:
    """Deliver pending research event alerts to Feishu with retry/rate-limit handling."""
    webhook = settings.feishu_webhook.get_secret_value()
    if not webhook:
        return 0
    url = urlsplit(webhook)
    if url.scheme != "https" or url.hostname != "open.feishu.cn" or not re.fullmatch(
            r"/open-apis/bot/v2/hook/[\w-]+", url.path) or url.username or url.query or url.fragment or url.port:
        raise ValueError("飞书 Webhook 地址格式无效")
    sent_count = 0
    for _ in range(5):
        now = utc_now()
        with store.transaction() as con:
            job = row(con, """SELECT * FROM event_notifications
                              WHERE status='pending' AND next_at<=:now
                              ORDER BY next_at, id LIMIT 1""", now=now.isoformat())
            if not job:
                break
            attempt = job["attempts"] + 1
            write(con, "UPDATE event_notifications SET status='sending',attempts=:count WHERE id=:id",
                  count=attempt, id=job["id"])
        payload = {"msg_type": "text", "content": {"text": job["payload"][:3800]}}
        secret = settings.feishu_secret.get_secret_value()
        if secret:
            timestamp = str(int(now.timestamp()))
            key = f"{timestamp}\n{secret}".encode()
            payload.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
        wait = 0
        try:
            response = client.post(webhook, json=payload, timeout=12)
            try:
                body = response.json()
            except ValueError:
                body = {}
            code = body.get("code", body.get("StatusCode")) if isinstance(body, dict) else None
            if response.status_code == 200 and code == 0:
                status, detail = "sent", None
                sent_count += 1
            elif response.status_code == 429 or code in {11232, 9499}:
                status, detail = ("pending" if attempt < 3 else "failed"), "rate_limit"
                wait = retry_delay(response, 30 * (2 ** (attempt - 1)))
            elif 400 <= response.status_code < 500:
                status, detail = "failed", f"http_{response.status_code}"
            elif response.status_code == 200 and type(code) is int:
                status, detail = "failed", f"feishu_{code}"
            else:
                status, detail = "uncertain", f"http_{response.status_code}"
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            status, detail = ("pending" if attempt < 3 else "failed"), "connection_error"
            wait = 30 * (2 ** (attempt - 1))
        except httpx.RequestError:
            status, detail = "uncertain", "response_unconfirmed"
        store.execute("UPDATE event_notifications SET status=:state,next_at=:next,detail=:detail WHERE id=:id",
                      state=status, next=(now + timedelta(seconds=min(wait, 86400))).isoformat(),
                      detail=detail, id=job["id"])
    return sent_count
