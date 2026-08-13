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
Orientation = Literal["portrait", "landscape", "9:16", "1:1", "4:3"]


def _split_paths(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _build_reference_paths_and_hint(
    character_paths: list[str], scene_paths: list[str], legacy_paths: list[str]
) -> tuple[list[str], str]:
    """把"角色参考图"和"环境参考图"合并成一份传给 Seedream 的路径列表，并生成一句
    prompt 提示语。Ark 的 images/generations 接口本身不支持给每张参考图标注"这张是
    角色/这张是环境"这种角色区分——多张参考图对模型来说就是无差别的一组图，所以这里
    的分类主要是我们这边"管理上"分开(用户上传/预览体验更清楚)，真正喂给模型的仍是
    一份合并列表；额外拼一句文字提示，让模型大致知道"前几张管人物、后几张管环境"，
    是 best-effort，不是强约束。
    legacy_paths 是老版本(拆分之前)存的不分类参考图，只有 character/scene 都没填时
    才会用它兜底，保证老记录"重新生成"还能读到原来的参考图。
    """
    if not character_paths and not scene_paths and legacy_paths:
        return legacy_paths, ""

    merged = character_paths + scene_paths
    hint_parts = []
    if character_paths:
        hint_parts.append(f"前 {len(character_paths)} 张参考图用于角色长相/穿着参考")
    if scene_paths:
        hint_parts.append(f"{'后' if character_paths else '前'} {len(scene_paths)} 张参考图用于环境/场景/光线参考")
    hint = f"（{'，'.join(hint_parts)}）" if hint_parts else ""
    return merged, hint


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
    # 老字段，保留兼容(早期版本不分类的参考图)；新前端已经不再往这里写，
    # 统一用下面两个分类字段。
    referenceImagePaths: Optional[str] = None
    # 角色参考图：逗号分隔的本地路径，想让画面人物长相/穿着贴近这几张图。
    characterReferenceImagePaths: Optional[str] = None
    # 环境参考图：逗号分隔的本地路径，想让画面场景/背景/光线贴近这几张图。
    sceneReferenceImagePaths: Optional[str] = None
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
            "characterReferenceImagePaths, sceneReferenceImagePaths, status, createdAt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                image_id,
                body.projectId,
                prompt,
                body.orientation,
                body.styleMode,
                body.referenceImagePaths,
                body.characterReferenceImagePaths,
                body.sceneReferenceImagePaths,
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
            _split_paths(body.characterReferenceImagePaths),
            _split_paths(body.sceneReferenceImagePaths),
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
    character_paths: list[str],
    scene_paths: list[str],
    legacy_paths: list[str],
) -> None:
    try:
        reference_paths, hint = _build_reference_paths_and_hint(character_paths, scene_paths, legacy_paths)
        result = generate_standalone_image(
            image_id,
            prompt + hint,
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
            _split_paths(row["characterReferenceImagePaths"]),
            _split_paths(row["sceneReferenceImagePaths"]),
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
