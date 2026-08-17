import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, get_settings, new_id, now_iso
from app.providers import minimax as minimax_provider
from app.providers import seedance as seedance_provider
from app.providers.seedream import DEFAULT_IMAGE_RATIO, IMAGE_RATIOS
from app.services.paths import to_static_url

# 无剧本图生视频：独立的一级功能，不挂在任何 Project 详情页下面——不需要先建视频项目、
# 写剧本、拆场次镜头，上传一张参考图 + 写一段描述就能直接出视频，所以路由前缀是顶层
# /video-generations，跟 /posters 是同一个模式。
router = APIRouter(prefix="/video-generations", tags=["video-generations"])

Ratio = Literal["portrait", "landscape", "9:16", "1:1", "4:3"]


def _serialize(row) -> dict:
    d = dict(row)
    d["url"] = to_static_url(d.get("filePath"))
    d["ratioLabel"] = IMAGE_RATIOS.get(d.get("ratio"), {}).get("label", d.get("ratio"))
    return d


@router.get("")
def list_video_generations():
    """列出所有图生视频记录，不按 projectId 过滤，最新生成的排最前面。"""
    with get_connection() as conn:
        rows = conn.execute('SELECT * FROM "VideoGeneration" ORDER BY createdAt DESC').fetchall()
    return [_serialize(r) for r in rows]


@router.get("/options")
def list_video_generation_options():
    """给前端渲染"生成比例"选择器用，跟海报/文生图共用同一份比例词典
    (app/providers/seedream.py 的 IMAGE_RATIOS)。"""
    return {"ratios": [{"id": rid, "label": cfg["label"]} for rid, cfg in IMAGE_RATIOS.items()]}


class CreateVideoGenerationBody(BaseModel):
    referenceImagePath: str
    prompt: str
    ratio: Ratio = DEFAULT_IMAGE_RATIO
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
            'INSERT INTO "VideoGeneration" (id, projectId, referenceImagePath, prompt, ratio, status, createdAt) '
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (video_id, body.projectId, reference_path, prompt, body.ratio, "running", now_iso()),
        )

    thread = threading.Thread(
        target=_run_video_generation,
        args=(video_id, reference_path, prompt, body.ratio, body.projectId),
        daemon=True,
    )
    thread.start()

    return {"videoId": video_id, "status": "running"}


def _generate_video_from_image(
    video_id: str, reference_path: str, prompt: str, ratio: str, project_id: Optional[str]
) -> dict:
    """按全局设置 Setting.videoProvider(seedance 默认 | minimax) 在两个 provider 的
    独立"图生视频"函数之间派发——这个功能没有 Story/Shot 结构，不走 registry/Provider
    抽象(见 seedance.py 里 generate_video_from_image 的注释)，所以两条 provider 各自的
    实现之间要一个手动派发点，跟分镜生成视频那边靠 registry.resolve("video", 名字)
    派发是同一个选择逻辑，只是没有 registry 可用。
    """
    with get_connection() as conn:
        settings = get_settings(conn)
    provider_name = settings.get("videoProvider") or "seedance"
    if provider_name == "minimax":
        return minimax_provider.generate_video_from_image(video_id, reference_path, prompt, ratio=ratio, project_id=project_id)
    return seedance_provider.generate_video_from_image(video_id, reference_path, prompt, ratio=ratio, project_id=project_id)


def _run_video_generation(
    video_id: str, reference_path: str, prompt: str, ratio: str, project_id: Optional[str]
) -> None:
    try:
        result = _generate_video_from_image(video_id, reference_path, prompt, ratio, project_id)
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
        args=(video_id, row["referenceImagePath"], row["prompt"], row["ratio"], row["projectId"]),
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
