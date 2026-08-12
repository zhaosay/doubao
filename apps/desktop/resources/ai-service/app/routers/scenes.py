import threading
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, new_id
from app.providers.seedream import generate_scene_reference
from app.services.paths import to_static_url

router = APIRouter(tags=["scenes"])


class UpdateSceneBody(BaseModel):
    summary: str


class CreateShotBody(BaseModel):
    sceneType: Optional[str] = None
    drawPrompt: str = ""
    motionPrompt: Optional[str] = None
    dialogue: Optional[str] = None
    durationSec: float = 4
    characterName: Optional[str] = None
    transitionToNext: Optional[str] = None
    emotion: Optional[str] = None


class GenerateSceneBody(BaseModel):
    # 有值就走图生图：比如手头有一张实拍场地照片/别处找的参考图，想让 Seedream 照着这个
    # 环境画，而不是纯靠文字描述让它自己发挥。不传就是原来的纯文生图。
    referenceImagePaths: Optional[list[str]] = None


def _serialize_scene(row) -> dict:
    d = dict(row)
    d["url"] = to_static_url(d.get("refImagePath"))
    return d


def _run_scene_generation(scene_id: str, summary: str, reference_image_paths: Optional[list[str]] = None) -> None:
    try:
        result = generate_scene_reference(scene_id, summary, reference_image_paths)
        with get_connection() as conn:
            conn.execute(
                'UPDATE "Scene" SET status = ?, refImagePath = ?, providerId = ?, model = ?, '
                "error = NULL WHERE id = ?",
                ("completed", result["filePath"], result.get("providerId"), result.get("model"), scene_id),
            )
    except Exception as exc:  # noqa: BLE001
        with get_connection() as conn:
            conn.execute(
                'UPDATE "Scene" SET status = ?, error = ? WHERE id = ?',
                ("failed", str(exc), scene_id),
            )


@router.post("/scenes/{scene_id}/generate")
def generate_scene(scene_id: str, body: GenerateSceneBody = GenerateSceneBody()):
    """生成这场戏的环境母版图（多镜头一致性用）：跟角色设定图同一套异步生成模式，
    立刻返回 running，前端轮询 GET /projects/{id} 里 scenes[].status 看进度。
    body.referenceImagePaths 有值时走图生图。
    """
    with get_connection() as conn:
        scene = conn.execute('SELECT * FROM "Scene" WHERE id = ?', (scene_id,)).fetchone()
        if scene is None:
            raise HTTPException(404, "场次不存在")
        conn.execute('UPDATE "Scene" SET status = ?, error = NULL WHERE id = ?', ("running", scene_id))

    thread = threading.Thread(
        target=_run_scene_generation,
        args=(scene_id, scene["summary"], body.referenceImagePaths),
        daemon=True,
    )
    thread.start()

    return {"sceneId": scene_id, "status": "running"}


@router.get("/scenes/search")
def search_scenes(q: Optional[str] = None, excludeSceneId: Optional[str] = None, limit: int = 30):
    """跨所有项目搜已经生成完成的场景参考图，给"复用已有场景"用：跟角色复用同一个道理——
    同一个环境（比如系列里反复出现的同一个房间/街道）没必要每个项目都重新调一次生成。
    按场次描述(summary)关键词搜；q 为空就按最近生成时间倒序返回最近一批。
    """
    with get_connection() as conn:
        sql = (
            'SELECT sc.*, p.title AS projectTitle, p.id AS projectId '
            'FROM "Scene" sc '
            'JOIN "Story" st ON sc.storyId = st.id '
            'JOIN "Project" p ON st.projectId = p.id '
            'WHERE sc.status = "completed"'
        )
        params: list = []
        if q and q.strip():
            sql += ' AND sc.summary LIKE ?'
            params.append(f"%{q.strip()}%")
        if excludeSceneId:
            sql += ' AND sc.id != ?'
            params.append(excludeSceneId)
        sql += ' ORDER BY sc.rowid DESC LIMIT ?'
        params.append(max(1, min(limit, 100)))
        rows = conn.execute(sql, params).fetchall()

    return [_serialize_scene(r) for r in rows]


class ReuseSceneBody(BaseModel):
    sourceSceneId: str


@router.post("/scenes/{scene_id}/reuse")
def reuse_scene(scene_id: str, body: ReuseSceneBody):
    """把另一个已生成完成的场景参考图"复用"过来，不调用生成接口：
    直接复制 refImagePath/providerId/model，标成 completed。
    """
    with get_connection() as conn:
        target = conn.execute('SELECT id FROM "Scene" WHERE id = ?', (scene_id,)).fetchone()
        if target is None:
            raise HTTPException(404, "场次不存在")
        source = conn.execute(
            'SELECT refImagePath, providerId, model, status FROM "Scene" WHERE id = ?',
            (body.sourceSceneId,),
        ).fetchone()
        if source is None:
            raise HTTPException(404, "要复用的源场景不存在")
        if source["status"] != "completed" or not source["refImagePath"]:
            raise HTTPException(400, "源场景还没有生成完成的参考图，不能复用")

        conn.execute(
            'UPDATE "Scene" SET status = ?, refImagePath = ?, providerId = ?, model = ?, error = NULL '
            "WHERE id = ?",
            ("completed", source["refImagePath"], source["providerId"], source["model"], scene_id),
        )
        row = conn.execute('SELECT * FROM "Scene" WHERE id = ?', (scene_id,)).fetchone()

    return _serialize_scene(row)


@router.patch("/scenes/{scene_id}")
def update_scene(scene_id: str, body: UpdateSceneBody):
    """手动改场次描述(summary)，比如手动加剧本时补一句场景说明。"""
    with get_connection() as conn:
        scene = conn.execute('SELECT id FROM "Scene" WHERE id = ?', (scene_id,)).fetchone()
        if scene is None:
            raise HTTPException(404, "场次不存在")
        conn.execute('UPDATE "Scene" SET summary = ? WHERE id = ?', (body.summary, scene_id))
        row = conn.execute('SELECT * FROM "Scene" WHERE id = ?', (scene_id,)).fetchone()
    return dict(row)


@router.post("/scenes/{scene_id}/shots")
def create_shot(scene_id: str, body: CreateShotBody):
    """给一个场次手动加一个镜头(空白或者带内容都行)，之后跟自动生成的镜头一样可以
    PATCH 改字段、点「生成图片/视频/配音」。用于手动搭剧本，或者在自动生成结果里插一镜。
    """
    with get_connection() as conn:
        scene = conn.execute('SELECT id FROM "Scene" WHERE id = ?', (scene_id,)).fetchone()
        if scene is None:
            raise HTTPException(404, "场次不存在")
        order_row = conn.execute(
            'SELECT MAX("order") AS m FROM "Shot" WHERE sceneId = ?', (scene_id,)
        ).fetchone()
        order = (order_row["m"] + 1) if order_row and order_row["m"] is not None else 0
        shot_id = new_id()
        conn.execute(
            'INSERT INTO "Shot" (id, sceneId, "order", sceneType, drawPrompt, motionPrompt, '
            'dialogue, durationSec, characterName, transitionToNext, emotion) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                shot_id,
                scene_id,
                order,
                body.sceneType,
                body.drawPrompt,
                body.motionPrompt,
                body.dialogue,
                body.durationSec,
                body.characterName,
                body.transitionToNext,
                body.emotion,
            ),
        )
        row = conn.execute('SELECT * FROM "Shot" WHERE id = ?', (shot_id,)).fetchone()
    return dict(row)


class ReorderShotsBody(BaseModel):
    shotIds: list[str]


@router.patch("/scenes/{scene_id}/shots/reorder")
def reorder_shots(scene_id: str, body: ReorderShotsBody):
    """拖拽调整这场戏里镜头的顺序：shotIds 必须是这场戏下全部镜头 id 的一个排列，
    按数组下标重写各自的 order 字段。"""
    with get_connection() as conn:
        scene = conn.execute('SELECT id FROM "Scene" WHERE id = ?', (scene_id,)).fetchone()
        if scene is None:
            raise HTTPException(404, "场次不存在")
        existing_ids = {
            r["id"] for r in conn.execute('SELECT id FROM "Shot" WHERE sceneId = ?', (scene_id,)).fetchall()
        }
        if set(body.shotIds) != existing_ids or len(body.shotIds) != len(existing_ids):
            raise HTTPException(400, "shotIds 必须是这场戏下全部镜头 id 的一个排列，不能多也不能少")
        for order, shot_id in enumerate(body.shotIds):
            conn.execute('UPDATE "Shot" SET "order" = ? WHERE id = ?', (order, shot_id))

    return {"reordered": len(body.shotIds)}


@router.delete("/scenes/{scene_id}")
def delete_scene(scene_id: str):
    """删掉一个场次，连同它下面所有镜头和已生成的素材一起删(外键限制，得按子->父顺序删)。"""
    with get_connection() as conn:
        scene = conn.execute('SELECT id FROM "Scene" WHERE id = ?', (scene_id,)).fetchone()
        if scene is None:
            raise HTTPException(404, "场次不存在")
        shot_ids = [
            r["id"] for r in conn.execute('SELECT id FROM "Shot" WHERE sceneId = ?', (scene_id,)).fetchall()
        ]
        for shot_id in shot_ids:
            conn.execute('DELETE FROM "Asset" WHERE shotId = ?', (shot_id,))
        conn.execute('DELETE FROM "Shot" WHERE sceneId = ?', (scene_id,))
        conn.execute('DELETE FROM "Scene" WHERE id = ?', (scene_id,))
    return {"deleted": scene_id}
