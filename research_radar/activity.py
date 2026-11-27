"""Persist compact, user-visible events without prompts, keys or raw HTTP bodies."""

from .models import dump, utc_now
from .store import write


def log_event(store, scope, target, stage, status, *, detail=None, elapsed_ms=None, connection=None):
    statement = """INSERT INTO task_events(scope,target,stage,status,detail,elapsed_ms,created_at)
                   VALUES (:scope,:target,:stage,:status,:detail,:elapsed,:now)"""
    values = {"scope": scope, "target": target, "stage": stage, "status": status,
              "detail": dump(detail or {}), "elapsed": elapsed_ms, "now": utc_now().isoformat()}
    if connection is None:
        store.execute(statement, **values)
    else:
        write(connection, statement, **values)


def timeline(store, scope, target, limit=100):
    return list(reversed(store.all("""SELECT stage,status,detail,elapsed_ms,created_at FROM task_events
        WHERE scope=:scope AND target=:target ORDER BY seq DESC LIMIT :limit""",
        scope=scope, target=target, limit=limit)))


def event_sink(store, scope, target):
    return lambda stage, status, **data: log_event(store, scope, target, stage, status, **data)
