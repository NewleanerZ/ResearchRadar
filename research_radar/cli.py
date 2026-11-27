import argparse
from pathlib import Path
import json
import logging
import sys
from datetime import date

from pydantic import ValidationError

from .delivery import retry_notification
from .models import Settings, Topic, utc_now
from .service import enqueue_daily, library, open_store, print_json, refresh_publications, sample_topics
from .workflow import Worker


def parser():
    cli = argparse.ArgumentParser(prog="radar", description="ResearchRadar Agent")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("init-db", "seed-topics", "topics", "restore-reports", "refresh-publications", "warm-embedding"):
        commands.add_parser(name)
    scheduler = commands.add_parser("scheduler")
    scheduler_mode = scheduler.add_mutually_exclusive_group()
    scheduler_mode.add_argument("--manual-only", action="store_true", help="仅处理已提交的任务")
    scheduler_mode.add_argument("--daily-only", action="store_true", help="调度关注主题的每日检索并处理手动任务")
    save = commands.add_parser("save-topic")
    save.add_argument("json_path", type=Path)
    save.add_argument("--revision", type=int, default=0)
    for name in ("topic", "pause", "resume", "archive", "confirm-expansions", "run-daily", "backfill"):
        sub = commands.add_parser(name)
        sub.add_argument("topic")
        if name in {"run-daily", "backfill"}:
            sub.add_argument("--wait", action="store_true")
        if name == "backfill":
            sub.add_argument("--days", type=int, choices=(90, 180), default=180)
    for name in ("retry-run", "retry-notification", "run", "report"):
        commands.add_parser(name).add_argument("run_id")
    for name in ("runs", "trends", "papers"):
        sub = commands.add_parser(name)
        sub.add_argument("--topic", required=name != "runs")
        if name == "papers":
            sub.add_argument("--query", default="")
            sub.add_argument("--after", type=date.fromisoformat)
            sub.add_argument("--before", type=date.fromisoformat)
            sub.add_argument("--favorites", action="store_true")
            sub.add_argument("--unread", action="store_true")
            sub.add_argument("--limit", type=int, default=50)
    sub = commands.add_parser("reading")
    sub.add_argument("topic")
    sub.add_argument("paper_id", type=int)
    sub.add_argument("--favorite", action=argparse.BooleanOptionalAction)
    sub.add_argument("--read", action=argparse.BooleanOptionalAction)
    commands.add_parser("backup").add_argument("destination", type=Path)

    idx = commands.add_parser("index-pdfs")
    idx.add_argument("--topic", required=True)
    idx.add_argument("--limit", type=int, default=10)
    for name in ("weekly-report", "monthly-report"):
        rep = commands.add_parser(name)
        rep.add_argument("topic")
        rep.add_argument("--day", type=date.fromisoformat)
    zot = commands.add_parser("sync-zotero")
    zot.add_argument("topic")
    zot.add_argument("--collection", default="")
    zot.add_argument("--push", action="store_true")
    gap = commands.add_parser("gap-analysis")
    gap.add_argument("topic")
    gap.add_argument("--project", default=None)
    serve_api = commands.add_parser("serve-api")
    serve_api.add_argument("--host", default=None)
    serve_api.add_argument("--port", type=int, default=None)
    serve_api.add_argument("--no-worker", action="store_true", help="禁用内置后台任务消费线程")
    commands.add_parser("mcp")
    return cli


def run_command(args, settings):
    cmd = args.command
    if cmd == "warm-embedding":
        from huggingface_hub import snapshot_download
        from .ranking import MODEL
        print(snapshot_download(MODEL, revision=settings.embedding_revision,
                                local_dir=settings.embedding_path(), max_workers=2,
                                allow_patterns=["*.json", "*.txt", "model.safetensors", "1_Pooling/*"]))
        return 0
    store = open_store(settings)
    if cmd == "init-db":
        print(store.path)
    elif cmd == "seed-topics":
        keys = {topic.id for topic in store.topics()}
        for topic in sample_topics():
            if topic.id not in keys:
                store.save_topic(topic)
        print_json([topic.id for topic in store.topics()])
    elif cmd == "topics":
        print_json([topic.model_dump(mode="json") for topic in store.topics()])
    elif cmd == "topic":
        topic, revision = store.topic(args.topic)
        print_json({"revision": revision, "topic": topic.model_dump(mode="json")})
    elif cmd == "save-topic":
        store.save_topic(Topic.model_validate_json(args.json_path.read_text(encoding="utf-8-sig")), args.revision)
    elif cmd in {"pause", "resume", "archive", "confirm-expansions"}:
        topic, revision = store.topic(args.topic)
        if cmd == "confirm-expansions":
            topic.expansions = list(dict.fromkeys(topic.expansions + topic.proposed_expansions))[:12]
            topic.proposed_expansions = []
        else:
            topic.enabled = cmd == "resume"
            topic.archived = cmd == "archive"
        store.save_topic(topic, revision)
    elif cmd == "run-daily":
        run_id = enqueue_daily(store, args.topic)
        print(run_id, flush=True)
        if args.wait:
            worker = Worker(store, settings)
            try:
                worker.tick(schedule=False, run_id=run_id, notify=False)
            finally:
                worker.client.close()
            status = store.one("SELECT status FROM runs WHERE id=:id", id=run_id)["status"]
            print(status)
            return 1 if status == "failed" else 0
    elif cmd == "backfill":
        topic = store.topic(args.topic)[0]
        now = utc_now()
        jobs = [store.enqueue(topic, now, "backfill-recent")]
        if args.days == 180:
            jobs.append(store.enqueue(topic, now, "backfill-older"))
        print_json(jobs)
        if args.wait:
            worker = Worker(store, settings)
            try:
                for run_id in jobs:
                    worker.tick(schedule=False, run_id=run_id, notify=False)
            finally:
                worker.client.close()
            states = {run_id: store.one("SELECT status FROM runs WHERE id=:id", id=run_id)["status"]
                      for run_id in jobs}
            print_json(states)
            return 1 if any(status == "failed" for status in states.values()) else 0
    elif cmd == "scheduler":
        Worker(store, settings).serve(schedule=not args.manual_only, maintenance=not args.daily_only)
    elif cmd == "runs":
        print_json(store.all("""SELECT r.id,r.topic_id,r.local_day,r.kind,r.status,r.error,r.usage,
            n.status notification FROM runs r LEFT JOIN notifications n ON n.run_id=r.id
            WHERE (:topic IS NULL OR r.topic_id=:topic) ORDER BY r.created_at DESC LIMIT 100""", topic=args.topic))
    elif cmd in {"run", "report"}:
        result = store.one("SELECT * FROM runs WHERE id=:id", id=args.run_id) if cmd == "run" else store.one(
            "SELECT path,markdown FROM reports WHERE run_id=:id", id=args.run_id)
        if not result:
            raise ValueError("找不到运行或日报")
        if cmd == "run":
            result["result"] = json.loads(result["result"] or "{}")
            result["usage"] = json.loads(result["usage"])
            result["scans"] = store.all("SELECT * FROM scans WHERE run_id=:id", id=args.run_id)
            result["notification"] = store.one("SELECT status,attempts,detail,next_at FROM notifications WHERE run_id=:id",
                                               id=args.run_id)
            print_json(result)
        else:
            sys.stdout.buffer.write(result["markdown"].encode("utf-8"))
    elif cmd == "papers":
        topic = store.topic(args.topic)[0]
        records = library(store, topic, args.query, args.after, args.before, args.favorites, args.unread)
        print_json([{**item, "records": [entry.model_dump(mode="json") for entry in item["records"]]}
                    for item in records[:max(0, args.limit)]])
    elif cmd == "reading":
        store.set_reading_state(args.topic, args.paper_id, args.favorite, args.read)
    elif cmd == "trends":
        snapshots = store.all("SELECT snapshot FROM trends WHERE topic_id=:topic ORDER BY local_day DESC LIMIT 30",
                              topic=args.topic)
        print_json([json.loads(item["snapshot"]) for item in snapshots])
    elif cmd == "retry-run":
        store.retry(args.run_id)
    elif cmd == "retry-notification":
        retry_notification(store, args.run_id)
    elif cmd == "backup":
        store.backup(args.destination)
    elif cmd == "restore-reports":
        store.restore_markdown()
    elif cmd == "refresh-publications":
        result = refresh_publications(store, settings)
        print_json(result)
        return 1 if result["errors"] else 0
    elif cmd == "index-pdfs":
        import time as clock
        import httpx
        from .rag import index_paper_pdf
        topic = store.topic(args.topic)[0]
        indexed, errors = 0, []
        with httpx.Client() as client:
            for item in store.papers(topic.id)[:max(1, args.limit)]:
                try:
                    index_paper_pdf(store, settings, client, item["paper_id"], item["records"], clock.monotonic() + 30)
                    indexed += 1
                except ValueError as err:
                    errors.append({"paper_id": item["paper_id"], "reason": str(err)})
        print_json({"indexed": indexed, "errors": errors})
    elif cmd in {"weekly-report", "monthly-report"}:
        from zoneinfo import ZoneInfo
        from .period_reports import generate_period_report
        topic = store.topic(args.topic)[0]
        day = args.day or utc_now().astimezone(ZoneInfo(topic.timezone)).date()
        rtype = "weekly" if cmd == "weekly-report" else "monthly"
        print_json(generate_period_report(store, settings, topic, day, rtype))
    elif cmd == "sync-zotero":
        from .zotero import pull_zotero_collection, push_favorites_to_zotero
        pulled = pull_zotero_collection(store, settings, args.topic, collection_key=args.collection)
        pushed = push_favorites_to_zotero(store, settings, args.topic, collection_key=args.collection) if args.push else None
        print_json({"pulled": pulled, "pushed": pushed})
    elif cmd == "gap-analysis":
        from .gap_workflow import run_gap_analysis
        print_json(run_gap_analysis(store, settings, args.topic, project_id=args.project))
    elif cmd == "serve-api":
        import threading
        import uvicorn
        from .api import create_app
        host = args.host or settings.api_host
        port = args.port or settings.api_port
        runtime_settings = settings.model_copy(update={"api_host": host, "api_port": port})
        stop_worker = threading.Event()
        worker_thread = None
        if not args.no_worker:
            def _worker_loop():
                bg_worker = Worker(store, runtime_settings)
                try:
                    while not stop_worker.is_set():
                        try:
                            fresh = Settings.from_env().model_copy(update={"data_dir": store.directory})
                            bg_worker.settings = fresh
                            bg_worker.ranker.settings = fresh
                            bg_worker.tick(schedule=False, notify=False, maintenance=False, research=True)
                        except Exception as exc:
                            logging.getLogger(__name__).warning("background worker tick error: %s", type(exc).__name__)
                        stop_worker.wait(3.0)
                finally:
                    bg_worker.client.close()

            worker_thread = threading.Thread(target=_worker_loop, name="radar-api-worker", daemon=True)
            worker_thread.start()
        try:
            uvicorn.run(create_app(store, runtime_settings), host=host, port=port)
        finally:
            stop_worker.set()
            if worker_thread is not None:
                worker_thread.join(timeout=2.0)
    elif cmd == "mcp":
        from .mcp_server import create_mcp_server
        create_mcp_server(store, settings).run()
    return 0


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run_command(parser().parse_args(), Settings.from_env())
    except ValidationError as error:
        print(error.json(include_input=False, include_url=False, include_context=False), file=sys.stderr)
    except (ValueError, OSError, ImportError) as error:
        print(error, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
