"""Topic-level research defaults, with immutable revisions."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import dump, utc_now
from .store import row, write


class ProjectMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    venues: list[str] = Field(default_factory=list, max_length=12)
    excluded: list[str] = Field(default_factory=list, max_length=20)
    datasets: list[str] = Field(default_factory=list, max_length=20)
    research_protocol: str = Field(default="", max_length=1500)
    main_only: bool = False

    @field_validator("venues", "excluded", "datasets")
    @classmethod
    def normalized_list(cls, values, info):
        clean = list(dict.fromkeys(item.strip() for item in values if item.strip()))
        if info.field_name == "venues":
            clean = list(dict.fromkeys(item.upper() for item in clean))
            if any(not re.fullmatch(r"[A-Z][A-Z0-9-]{1,19}", item) for item in clean):
                raise ValueError("会议范围请填写简称，例如 ICML 或 CVPR")
        if any(len(item) > 160 for item in clean):
            raise ValueError("每一项最多 160 字")
        return clean


def project_memory(store, topic_id):
    item = store.one("""SELECT revision,body FROM project_memories WHERE topic_id=:topic
                       ORDER BY revision DESC LIMIT 1""", topic=topic_id)
    if item:
        return item["revision"], ProjectMemory.model_validate_json(item["body"])
    return 0, ProjectMemory()


def save_project_memory(store, topic_id, expected_revision, memory: ProjectMemory):
    with store.transaction() as con:
        if not row(con, "SELECT id FROM topics WHERE id=:id", id=topic_id):
            raise ValueError("找不到主题")
        existing = row(con, """SELECT revision,body FROM project_memories
                               WHERE topic_id=:topic ORDER BY revision DESC LIMIT 1""", topic=topic_id)
        revision = existing["revision"] if existing else 0
        if revision != expected_revision:
            raise ValueError("项目记忆已更新，请重新打开后再编辑")
        value = memory.model_dump(mode="json")
        previous = ProjectMemory.model_validate_json(existing["body"]) if existing else ProjectMemory()
        if previous.model_dump(mode="json") == value:
            return revision
        write(con, "INSERT INTO project_memories VALUES (:topic,:revision,:body,:now)",
              topic=topic_id, revision=revision + 1, body=dump(value), now=utc_now().isoformat())
        return revision + 1


def memory_snapshot(store, topic_id):
    if not topic_id:
        return None
    revision, memory = project_memory(store, topic_id)
    return {"topic_id": topic_id, "revision": revision, **memory.model_dump(mode="json")}
