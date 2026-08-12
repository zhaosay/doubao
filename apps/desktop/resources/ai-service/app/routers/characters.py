import re
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, new_id
from app.providers.seedream import generate_character_reference
from app.services.paths import to_static_url

router = APIRouter(tags=["characters"])

_SPLIT_RE = re.compile(r"[、,，/]")


def _split_names(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [n.strip() for n in _SPLIT_RE.split(raw) if n.strip()]


def _serialize(row) -> dict:
    d = dict(row)
    d["url"] = to_static_url(d.get("refImagePath"))
    return d


def _get_story_id_or_404(conn, project_id: str) -> str:
    story = conn.execute('SELECT id FROM "Story" WHERE projectId = ?', (project_id,)).fetchone()
    if story is None:
        raise HTTPException(404, "项目不存在或缺少 Story 记录")
    return story["id"]


@router.get("/projects/{project_id}/characters")
def list_characters(project_id: str):
    """
    列出这个项目剧本里出现过的所有角色。第一次调用时会扫一遍所有 Shot 的
    characterName 字段，把新出现的角色名同步进 Character 表（已存在的不动）。
    """
    with get_connection() as conn:
        story_id = _get_story_id_or_404(conn, project_id)

        shot_rows = conn.execute(
            """
            SELECT s.characterName
            FROM "Shot" s
            JOIN "Scene" sc ON s.sceneId = sc.id
            WHERE sc.storyId = ?
            """,
            (story_id,),
        ).fetchall()

        names: set[str] = set()
        for row in shot_rows:
            names.update(_split_names(row["characterName"]))

        for name in names:
            existing = conn.execute(
                'SELECT id FROM "Character" WHERE storyId = ? AND name = ?', (story_id, name)
            ).fetchone()
            if existing is None:
                conn.execute(
                    'INSERT INTO "Character" (id, storyId, name, status) VALUES (?, ?, ?, ?)',
                    (new_id(), story_id, name, "pending"),
                )

        rows = conn.execute(
            'SELECT * FROM "Character" WHERE storyId = ? ORDER BY name', (story_id,)
        ).fetchall()

    return [_serialize(r) for r in rows]


@router.get("/characters/search")
def search_characters(q: Optional[str] = None, excludeCharacterId: Optional[str] = None, limit: int = 30):
    """
    跨所有项目搜已经生成完成的角色设定图，给"复用已有角色"用：同一个角色（甚至只是
    长得像的角色）没必要在每个新项目里重新调一次 Seedream，直接复用现成的参考图，
    省配额，视觉上也更一致。q 为空就按最近生成时间倒序返回最近的一批，方便不知道
    该搜什么关键词时直接翻着看。附带项目标题，方便区分"这是哪个项目里的角色"。
    """
    with get_connection() as conn:
        sql = (
            'SELECT c.*, p.title AS projectTitle, p.id AS projectId '
            'FROM "Character" c '
            'JOIN "Story" st ON c.storyId = st.id '
            'JOIN "Project" p ON st.projectId = p.id '
            'WHERE c.status = "completed"'
        )
        params: list = []
        if q and q.strip():
            sql += ' AND c.name LIKE ?'
            params.append(f"%{q.strip()}%")
        if excludeCharacterId:
            sql += ' AND c.id != ?'
            params.append(excludeCharacterId)
        sql += ' ORDER BY c.createdAt DESC LIMIT ?'
        params.append(max(1, min(limit, 100)))
        rows = conn.execute(sql, params).fetchall()

    return [_serialize(r) for r in rows]


class UpdateCharacterBody(BaseModel):
    # 外观描述/自定义提示词，改完点"重新生成设定图"就会带上新描述重新出图。
    # 传空字符串表示清空(不能传 None，None 在下面会被当"没传"过滤掉)。
    prompt: Optional[str] = None


@router.patch("/characters/{character_id}")
def update_character(character_id: str, body: UpdateCharacterBody):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "没有要更新的字段")
    with get_connection() as conn:
        char = conn.execute('SELECT id FROM "Character" WHERE id = ?', (character_id,)).fetchone()
        if char is None:
            raise HTTPException(404, "角色不存在")
        set_clause = ", ".join(f'"{k}" = ?' for k in fields)
        conn.execute(f'UPDATE "Character" SET {set_clause} WHERE id = ?', (*fields.values(), character_id))
        row = conn.execute('SELECT * FROM "Character" WHERE id = ?', (character_id,)).fetchone()
    return _serialize(row)


class ReuseCharacterBody(BaseModel):
    sourceCharacterId: str


@router.post("/characters/{character_id}/reuse")
def reuse_character(character_id: str, body: ReuseCharacterBody):
    """把另一个已生成完成的角色的参考图"复用"过来，不调用生成接口：
    直接把 refImagePath/providerId/model 复制到这个角色身上，标成 completed。
    """
    with get_connection() as conn:
        target = conn.execute('SELECT id FROM "Character" WHERE id = ?', (character_id,)).fetchone()
        if target is None:
            raise HTTPException(404, "角色不存在")
        source = conn.execute(
            'SELECT refImagePath, providerId, model, status, prompt FROM "Character" WHERE id = ?',
            (body.sourceCharacterId,),
        ).fetchone()
        if source is None:
            raise HTTPException(404, "要复用的源角色不存在")
        if source["status"] != "completed" or not source["refImagePath"]:
            raise HTTPException(400, "源角色还没有生成完成的设定图，不能复用")

        # 连原始提示词(prompt)一起复制过来——不然复用完设定图，角色库输入框里那句话是空的，
        # 看起来这张图"凭空冒出来"，也没法照着原提示词接着改。之前这里漏了这个字段。
        conn.execute(
            'UPDATE "Character" SET status = ?, refImagePath = ?, providerId = ?, model = ?, prompt = ?, error = NULL '
            "WHERE id = ?",
            ("completed", source["refImagePath"], source["providerId"], source["model"], source["prompt"], character_id),
        )
        row = conn.execute('SELECT * FROM "Character" WHERE id = ?', (character_id,)).fetchone()

    return _serialize(row)


def _run_character_generation(character_id: str, name: str, prompt: Optional[str]) -> None:
    try:
        result = generate_character_reference(character_id, name, appearance=prompt)
        with get_connection() as conn:
            conn.execute(
                'UPDATE "Character" SET status = ?, refImagePath = ?, providerId = ?, model = ?, '
                "error = NULL WHERE id = ?",
                ("completed", result["filePath"], result.get("providerId"), result.get("model"), character_id),
            )
    except Exception as exc:  # noqa: BLE001
        with get_connection() as conn:
            conn.execute(
                'UPDATE "Character" SET status = ?, error = ? WHERE id = ?',
                ("failed", str(exc), character_id),
            )


@router.post("/characters/{character_id}/generate")
def generate_character(character_id: str):
    with get_connection() as conn:
        char = conn.execute('SELECT * FROM "Character" WHERE id = ?', (character_id,)).fetchone()
        if char is None:
            raise HTTPException(404, "角色不存在")
        conn.execute('UPDATE "Character" SET status = ?, error = NULL WHERE id = ?', ("running", character_id))

    thread = threading.Thread(
        target=_run_character_generation, args=(character_id, char["name"], char["prompt"]), daemon=True
    )
    thread.start()

    return {"characterId": character_id, "status": "running"}
