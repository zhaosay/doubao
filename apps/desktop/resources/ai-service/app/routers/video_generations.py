import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, new_id, now_iso
from app.providers.seedance import generate_video_from_image
from app.services.paths import to_static_url

# 无剧本图生视频：独立的一级功能，不挂在任何 Project 详情页下面——不需要先建视频项目、
# 写剧本、拆场次镜头，上传一张参考图 + 写一段描述就能直接出视频，所以路由前缀是顶层
# /video-generations，跟 /posters 是同一个模式。
router = APIRouter(prefix="/video-generations", tags=["video-generations"])


def _serialize(row) -> dict:
    d = dict(row)
    d["url"] = to_static_url(d.get("filePath"))
    return d


@router.get("")
def list_video_generations():
    """列出所有图生视频记录，不按 projectId 过滤，最新生成的排最前面。"""
    with get_connection() as conn:
        rows = conn.execute('SELECT * FROM "VideoGeneration" ORDER BY createdAt DESC').fetchall()
    return [_serialize(r) for r in rows]


class CreateVideoGenerationBody(BaseModel):
    referenceImagePath: str
    prompt: str
    # 可选：备注这条视频是照哪个视频项目的调子出的，纯粹是提示性字段，不影响生成逻辑。
    projectId: Optional[str] = None


@router.post("")
def create_video_generation(body: CreateVideoGenerationBody):
    reference_path = body.referenceImagePath.strip()
    if not reference_path:
        raise HTTPException(400, "请先选择一张参考图")
    if not Path(reference_path).expanduser().is_file():
        raise HTTPException(400, f"参考图文件不存在: {reference_path}")

    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "请填写画面/运镜描述")

    with get_connection() as conn:
        if body.projectId:
            project = conn.execute('SELECT id FROM "Project" WHERE id = ?', (body.projectId,)).fetchone()
            if project is None:
                raise HTTPException(404, "关联的项目不存在")

        video_id = new_id()
        conn.execute(
            'INSERT INTO "VideoGeneration" (id, projectId, referenceImagePath, prompt, status, createdAt) '
            "VALUES (?, ?, ?, ?, ?, ?)",
            (video_id, body.projectId, reference_path, prompt, "running", now_iso()),
        )

    thread = threading.Thread(
        target=_run_video_generation,
        args=(video_id, reference_path, prompt, body.projectId),
        daemon=True,
    )
    thread.start()

    return {"videoId": video_id, "status": "running"}


def _run_video_generation(video_id: str, reference_path: str, prompt: str, project_id: Optional[str]) -> None:
    try:
        result = generate_video_from_image(video_id, reference_path, prompt, project_id=project_id)
        with get_connection() as conn:
            conn.execute(
                'UPDATE "VideoGeneration" SET status = ?, filePath = ?, providerId = ?, model = ?, '
                "error = NULL WHERE id = ?",
                ("completed", result["filePath"], result.get("providerId"), result.get("model"), video_id),
            )
    except Exception as exc:  # noqa: BLE001
        with get_connection() as conn:
            conn.execute(
                'UPDATE "VideoGeneration" SET status = ?, error = ? WHERE id = ?', ("failed", str(exc), video_id)
            )


@router.post("/{video_id}/regenerate")
def regenerate_video_generation(video_id: str):
    with get_connection() as conn:
        row = conn.execute('SELECT * FROM "VideoGeneration" WHERE id = ?', (video_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "记录不存在")
        conn.execute('UPDATE "VideoGeneration" SET status = ?, error = NULL WHERE id = ?', ("running", video_id))

    thread = threading.Thread(
        target=_run_video_generation,
        args=(video_id, row["referenceImagePath"], row["prompt"], row["projectId"]),
        daemon=True,
    )
    thread.start()

    return {"videoId": video_id, "status": "running"}


@router.delete("/{video_id}")
def delete_video_generation(video_id: str):
    with get_connection() as conn:
        row = conn.execute('SELECT id FROM "VideoGeneration" WHERE id = ?', (video_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "记录不存在")
        conn.execute('DELETE FROM "VideoGeneration" WHERE id = ?', (video_id,))
    return {"deleted": video_id}
