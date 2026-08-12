import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, new_id, now_iso
from app.providers.seedream import (
    DEFAULT_POSTER_ORIENTATION,
    DEFAULT_STYLE_MODE,
    POSTER_ORIENTATIONS,
    generate_standalone_image,
)
from app.services.paths import to_static_url

# 独立文生图：跟 /posters 一样是顶层一级功能，不挂在任何 Project 下。
router = APIRouter(prefix="/text-images", tags=["text-images"])

StyleMode = Literal["comic", "realistic", "render3d", "freeform"]
Orientation = Literal["portrait", "landscape"]


def _split_paths(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _serialize(row) -> dict:
    d = dict(row)
    d["url"] = to_static_url(d.get("filePath"))
    d["orientationLabel"] = POSTER_ORIENTATIONS.get(d.get("orientation"), {}).get("label", d.get("orientation"))
    return d


@router.get("")
def list_text_images():
    with get_connection() as conn:
        rows = conn.execute('SELECT * FROM "TextImage" ORDER BY createdAt DESC').fetchall()
    return [_serialize(r) for r in rows]


@router.get("/options")
def list_text_image_options():
    """给前端渲染"画幅"选择器用，跟海报共用同一份朝向配置。"""
    return {
        "orientations": [{"id": oid, "label": cfg["label"]} for oid, cfg in POSTER_ORIENTATIONS.items()],
    }


class CreateTextImageBody(BaseModel):
    prompt: str
    styleMode: StyleMode = DEFAULT_STYLE_MODE
    orientation: Orientation = DEFAULT_POSTER_ORIENTATION
    # 逗号分隔的本地参考图路径，可选——不传就是纯文生图。
    referenceImagePaths: Optional[str] = None
    projectId: Optional[str] = None


@router.post("")
def create_text_image(body: CreateTextImageBody):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "请填写画面描述")

    with get_connection() as conn:
        if body.projectId:
            project = conn.execute('SELECT id FROM "Project" WHERE id = ?', (body.projectId,)).fetchone()
            if project is None:
                raise HTTPException(404, "关联的项目不存在")

        image_id = new_id()
        conn.execute(
            'INSERT INTO "TextImage" (id, projectId, prompt, orientation, styleMode, referenceImagePaths, '
            "status, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                image_id,
                body.projectId,
                prompt,
                body.orientation,
                body.styleMode,
                body.referenceImagePaths,
                "running",
                now_iso(),
            ),
        )

    thread = threading.Thread(
        target=_run_text_image_generation,
        args=(
            image_id,
            prompt,
            body.styleMode,
            body.orientation,
            body.projectId,
            _split_paths(body.referenceImagePaths),
        ),
        daemon=True,
    )
    thread.start()

    return {"imageId": image_id, "status": "running"}


def _run_text_image_generation(
    image_id: str,
    prompt: str,
    style_mode: str,
    orientation: str,
    project_id: Optional[str],
    reference_paths: list[str],
) -> None:
    try:
        result = generate_standalone_image(
            image_id,
            prompt,
            style_mode=style_mode,
            orientation=orientation,
            project_id=project_id,
            reference_image_paths=reference_paths or None,
        )
        with get_connection() as conn:
            conn.execute(
                'UPDATE "TextImage" SET status = ?, filePath = ?, providerId = ?, model = ?, '
                "error = NULL WHERE id = ?",
                ("completed", result["filePath"], result.get("providerId"), result.get("model"), image_id),
            )
    except Exception as exc:  # noqa: BLE001
        with get_connection() as conn:
            conn.execute('UPDATE "TextImage" SET status = ?, error = ? WHERE id = ?', ("failed", str(exc), image_id))


@router.post("/{image_id}/regenerate")
def regenerate_text_image(image_id: str):
    with get_connection() as conn:
        row = conn.execute('SELECT * FROM "TextImage" WHERE id = ?', (image_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "记录不存在")
        conn.execute('UPDATE "TextImage" SET status = ?, error = NULL WHERE id = ?', ("running", image_id))

    thread = threading.Thread(
        target=_run_text_image_generation,
        args=(
            image_id,
            row["prompt"],
            row["styleMode"],
            row["orientation"],
            row["projectId"],
            _split_paths(row["referenceImagePaths"]),
        ),
        daemon=True,
    )
    thread.start()

    return {"imageId": image_id, "status": "running"}


@router.delete("/{image_id}")
def delete_text_image(image_id: str):
    with get_connection() as conn:
        row = conn.execute('SELECT id FROM "TextImage" WHERE id = ?', (image_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "记录不存在")
        conn.execute('DELETE FROM "TextImage" WHERE id = ?', (image_id,))
    return {"deleted": image_id}
