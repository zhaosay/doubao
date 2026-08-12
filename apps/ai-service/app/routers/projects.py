import threading
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, get_settings, new_id
from app.services.paths import to_static_url
from app.services.story_generator import StoryGenerationError, generate_story_scenes

router = APIRouter(prefix="/projects", tags=["projects"])


StyleMode = Literal["comic", "realistic", "render3d", "freeform"]
# character: 人物剧情，剧本正常按角色驱动来写；no_character: 无固定角色，风光/氛围/产品向
# 内容，写剧本时不强行编人物出来凑数，角色库步骤相应弱化(见 characters 路由/前端)。
ContentType = Literal["character", "no_character"]


class CreateProjectBody(BaseModel):
    premise: str
    styleMode: StyleMode = "comic"
    contentType: ContentType = "character"


class UpdateProjectBody(BaseModel):
    title: Optional[str] = None
    premise: Optional[str] = None
    styleMode: Optional[StyleMode] = None
    contentType: Optional[ContentType] = None


class ImportShotBody(BaseModel):
    sceneType: Optional[str] = None
    drawPrompt: str
    motionPrompt: Optional[str] = None
    dialogue: Optional[str] = None
    durationSec: float = 4
    characterName: Optional[str] = None
    transitionToNext: Optional[str] = None
    emotion: Optional[str] = None


class ImportSceneBody(BaseModel):
    summary: str
    shots: list[ImportShotBody] = []


class ImportStoryBody(BaseModel):
    # scenes 的形状跟 claude 自动生成的 scenes 完全一致(见 story_generator.py 里的
    # PROMPT_TEMPLATE)，方便用户参照那份格式自己手写/用其它工具批量产出后直接粘贴导入，
    # 绕开本地 claude CLI。
    scenes: list[ImportSceneBody]
    # append：追加在已有 Scene 后面(默认，跟"重新生成剧本"行为一致)；
    # replace：先清空这个 Story 下所有 Scene/Shot(及其 Asset)，再写入，用于"推倒重来"。
    mode: Literal["append", "replace"] = "append"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("")
def create_project(body: CreateProjectBody):
    premise = body.premise.strip()
    if not premise:
        raise HTTPException(400, "premise 不能为空")

    project_id = new_id()
    story_id = new_id()
    title = premise if len(premise) <= 22 else premise[:22] + "…"

    with get_connection() as conn:
        conn.execute(
            'INSERT INTO "Project" (id, title, premise, status, styleMode, contentType, createdAt) '
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, title, premise, "draft", body.styleMode, body.contentType, _now()),
        )
        conn.execute(
            'INSERT INTO "Story" (id, projectId, content, status) VALUES (?, ?, ?, ?)',
            (story_id, project_id, "", "pending"),
        )

    return {
        "id": project_id,
        "title": title,
        "premise": premise,
        "status": "draft",
        "styleMode": body.styleMode,
        "contentType": body.contentType,
    }


@router.get("")
def list_projects():
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT id, title, premise, status, styleMode, contentType, createdAt, lastExportedAt '
            'FROM "Project" ORDER BY createdAt DESC'
        ).fetchall()
    return [dict(r) for r in rows]


@router.patch("/{project_id}")
def update_project(project_id: str, body: UpdateProjectBody):
    """改项目标题/简介/出图风格/内容类型。styleMode/contentType 切换只影响之后新生成的
    内容（已经写好的剧本、生成好的角色图/场景图/镜头画面不会被重新生成/重新润色），
    跟改剧本文字一样是"从这一刻起生效"，不做批量回刷。
    """
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "没有要更新的字段")
    with get_connection() as conn:
        project = conn.execute('SELECT id FROM "Project" WHERE id = ?', (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, "项目不存在")
        set_clause = ", ".join(f'"{k}" = ?' for k in fields)
        conn.execute(f'UPDATE "Project" SET {set_clause} WHERE id = ?', (*fields.values(), project_id))
        row = conn.execute(
            'SELECT id, title, premise, status, styleMode, contentType, createdAt, lastExportedAt '
            'FROM "Project" WHERE id = ?',
            (project_id,),
        ).fetchone()
    return dict(row)


@router.get("/{project_id}")
def get_project(project_id: str):
    with get_connection() as conn:
        project = conn.execute(
            'SELECT id, title, premise, status, styleMode, contentType, createdAt, lastExportedAt '
            'FROM "Project" WHERE id = ?',
            (project_id,),
        ).fetchone()
        if project is None:
            raise HTTPException(404, "项目不存在")

        story = conn.execute(
            'SELECT id, content, status FROM "Story" WHERE projectId = ?', (project_id,)
        ).fetchone()

        scenes = []
        if story is not None:
            scene_rows = conn.execute(
                'SELECT id, "order", summary, refImagePath, status, error, providerId, model FROM "Scene" '
                'WHERE storyId = ? ORDER BY "order"',
                (story["id"],),
            ).fetchall()
            for scene in scene_rows:
                shot_rows = conn.execute(
                    'SELECT id, "order", sceneType, drawPrompt, motionPrompt, dialogue, '
                    'durationSec, characterName, transitionToNext, emotion FROM "Shot" WHERE sceneId = ? '
                    'ORDER BY "order"',
                    (scene["id"],),
                ).fetchall()
                scene_dict = dict(scene)
                scene_dict["url"] = to_static_url(scene_dict.get("refImagePath"))
                scenes.append({**scene_dict, "shots": [dict(s) for s in shot_rows]})

    return {
        **dict(project),
        "story": dict(story) if story else None,
        "scenes": scenes,
    }


def _next_scene_order(conn, story_id: str) -> int:
    row = conn.execute('SELECT MAX("order") AS m FROM "Scene" WHERE storyId = ?', (story_id,)).fetchone()
    return (row["m"] + 1) if row and row["m"] is not None else 0


def _insert_scenes(conn, story_id: str, scenes: list[dict], start_order: int = 0) -> None:
    """把 [{"summary":..., "shots":[{...}]}] 形状的数据写进 Scene/Shot 表。
    claude 自动生成、手动新增、JSON 批量导入三条路径共用这一份写入逻辑。
    """
    for offset, scene in enumerate(scenes):
        scene_id = new_id()
        conn.execute(
            'INSERT INTO "Scene" (id, storyId, "order", summary) VALUES (?, ?, ?, ?)',
            (scene_id, story_id, start_order + offset, scene.get("summary", "")),
        )
        for shot_order, shot in enumerate(scene.get("shots", [])):
            conn.execute(
                'INSERT INTO "Shot" (id, sceneId, "order", sceneType, drawPrompt, '
                'motionPrompt, dialogue, durationSec, characterName, transitionToNext, emotion) '
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    scene_id,
                    shot_order,
                    shot.get("sceneType"),
                    shot.get("drawPrompt", ""),
                    shot.get("motionPrompt"),
                    shot.get("dialogue"),
                    shot.get("durationSec", 4),
                    shot.get("characterName"),
                    shot.get("transitionToNext"),
                    shot.get("emotion"),
                ),
            )


def _clear_story_scenes(conn, story_id: str) -> None:
    """删这个 Story 下所有 Scene/Shot，及挂在这些 Shot 下的 Asset(外键限制，得按子->父顺序删)。"""
    shot_ids = [
        r["id"]
        for r in conn.execute(
            'SELECT s.id FROM "Shot" s JOIN "Scene" sc ON s.sceneId = sc.id WHERE sc.storyId = ?',
            (story_id,),
        ).fetchall()
    ]
    for shot_id in shot_ids:
        conn.execute('DELETE FROM "Asset" WHERE shotId = ?', (shot_id,))
    conn.execute(
        'DELETE FROM "Shot" WHERE sceneId IN (SELECT id FROM "Scene" WHERE storyId = ?)', (story_id,)
    )
    conn.execute('DELETE FROM "Scene" WHERE storyId = ?', (story_id,))


def _build_story_provider_config(settings: dict) -> dict:
    """把 Setting 表里的 storyGen* 字段整理成 generate_story_scenes 要的
    provider_config 形状。settings 缺字段时(比如老库刚好没跑过自愈)一律退回
    claude_cli，跟字段本身的默认值保持一致。
    """
    return {
        "provider": settings.get("storyGenProvider") or "claude_cli",
        "baseUrl": settings.get("storyGenApiBaseUrl"),
        "apiKey": settings.get("storyGenApiKey"),
        "model": settings.get("storyGenApiModel"),
        "maxTokens": settings.get("storyGenApiMaxTokens"),
    }


def _run_story_generation(
    project_id: str,
    story_id: str,
    premise: str,
    style_mode: str,
    content_type: str,
    custom_style_hints: dict | None = None,
    custom_content_type_hints: dict | None = None,
    provider_config: dict | None = None,
) -> None:
    try:
        scenes = generate_story_scenes(
            premise,
            style_mode=style_mode,
            content_type=content_type,
            custom_style_hints=custom_style_hints,
            custom_content_type_hints=custom_content_type_hints,
            provider_config=provider_config,
        )

        with get_connection() as conn:
            _insert_scenes(conn, story_id, scenes, start_order=_next_scene_order(conn, story_id))
            conn.execute(
                'UPDATE "Story" SET status = ?, content = ? WHERE id = ?',
                ("completed", str(scenes), story_id),
            )
            conn.execute('UPDATE "Project" SET status = ? WHERE id = ?', ("active", project_id))

    except StoryGenerationError as exc:
        with get_connection() as conn:
            conn.execute('UPDATE "Story" SET status = ? WHERE id = ?', ("failed", story_id))
            conn.execute(
                'INSERT INTO "Task" (id, targetType, targetId, kind, status, error, updatedAt) '
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id(), "story", story_id, "story", "failed", str(exc), _now()),
            )
    except Exception as exc:  # noqa: BLE001
        with get_connection() as conn:
            conn.execute('UPDATE "Story" SET status = ? WHERE id = ?', ("failed", story_id))
            conn.execute(
                'INSERT INTO "Task" (id, targetType, targetId, kind, status, error, updatedAt) '
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id(), "story", story_id, "story", "failed", str(exc), _now()),
            )


@router.post("/{project_id}/story/generate")
def generate_story(project_id: str):
    """
    调本机 claude CLI 把 premise 扩成分镜脚本，写入 Scene/Shot 表。
    重新生成会在已有 Scene/Shot 基础上追加，不会先清空——想要干净重跑请先手动删除。
    """
    with get_connection() as conn:
        project = conn.execute(
            'SELECT id, premise, styleMode, contentType FROM "Project" WHERE id = ?', (project_id,)
        ).fetchone()
        if project is None:
            raise HTTPException(404, "项目不存在")
        story = conn.execute('SELECT id FROM "Story" WHERE projectId = ?', (project_id,)).fetchone()
        if story is None:
            raise HTTPException(404, "项目缺少 Story 记录，数据异常")
        story_id = story["id"]
        conn.execute('UPDATE "Story" SET status = ? WHERE id = ?', ("running", story_id))
        settings = get_settings(conn)

    thread = threading.Thread(
        target=_run_story_generation,
        args=(
            project_id,
            story_id,
            project["premise"],
            project["styleMode"],
            project["contentType"],
            settings.get("customStyleHints"),
            settings.get("customContentTypeHints"),
            _build_story_provider_config(settings),
        ),
        daemon=True,
    )
    thread.start()

    return {"storyId": story_id, "status": "running"}


@router.post("/{project_id}/story/import")
def import_story(project_id: str, body: ImportStoryBody):
    """
    手动加剧本：不调用 claude CLI，直接把用户自己写好的 scenes(JSON) 落库。
    格式跟 claude 自动生成的剧本一致：
    {"scenes": [{"summary": "场次描述", "shots": [{"sceneType": "远景", "drawPrompt": "...",
    "motionPrompt": "...", "dialogue": "...", "durationSec": 4, "characterName": "小明"}]}]}
    只有 drawPrompt 是必填，其它字段都可省略。mode=replace 会先清空这个项目已有的场次/镜头
    (连同已生成的素材)，mode=append(默认)是接着往后加。
    """
    if not body.scenes:
        raise HTTPException(400, "scenes 不能为空")

    with get_connection() as conn:
        project = conn.execute('SELECT id FROM "Project" WHERE id = ?', (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, "项目不存在")
        story = conn.execute('SELECT id FROM "Story" WHERE projectId = ?', (project_id,)).fetchone()
        if story is None:
            raise HTTPException(404, "项目缺少 Story 记录，数据异常")
        story_id = story["id"]

        if body.mode == "replace":
            _clear_story_scenes(conn, story_id)
            start_order = 0
        else:
            start_order = _next_scene_order(conn, story_id)

        scenes = [s.model_dump() for s in body.scenes]
        _insert_scenes(conn, story_id, scenes, start_order=start_order)
        conn.execute('UPDATE "Story" SET status = ? WHERE id = ?', ("completed", story_id))
        conn.execute('UPDATE "Project" SET status = ? WHERE id = ?', ("active", project_id))

    return {"storyId": story_id, "status": "completed", "importedScenes": len(scenes)}


class CreateSceneBody(BaseModel):
    summary: str = ""


@router.post("/{project_id}/scenes")
def create_scene(project_id: str, body: CreateSceneBody):
    """手动加一个空场次(不带镜头)，之后可以再用 POST /scenes/{id}/shots 往里加镜头，
    或者直接 PATCH 场次(见 scenes.py) 补 summary。用于在自动生成的剧本基础上手动追加，
    或者完全手动从零搭一份剧本。
    """
    with get_connection() as conn:
        story = conn.execute('SELECT id FROM "Story" WHERE projectId = ?', (project_id,)).fetchone()
        if story is None:
            raise HTTPException(404, "项目不存在或缺少 Story 记录")
        story_id = story["id"]
        scene_id = new_id()
        order = _next_scene_order(conn, story_id)
        conn.execute(
            'INSERT INTO "Scene" (id, storyId, "order", summary) VALUES (?, ?, ?, ?)',
            (scene_id, story_id, order, body.summary),
        )
        conn.execute('UPDATE "Story" SET status = ? WHERE id = ?', ("completed", story_id))
        conn.execute('UPDATE "Project" SET status = ? WHERE id = ?', ("active", project_id))

    return {"id": scene_id, "order": order, "summary": body.summary, "shots": []}


class ReorderScenesBody(BaseModel):
    sceneIds: list[str]


@router.patch("/{project_id}/scenes/reorder")
def reorder_scenes(project_id: str, body: ReorderScenesBody):
    """拖拽调整场次顺序：sceneIds 必须是这个项目下全部场次 id 的一个排列（不能多传/少传/
    传别的项目的场次id），按数组下标重写各自的 order 字段。
    """
    with get_connection() as conn:
        story = conn.execute('SELECT id FROM "Story" WHERE projectId = ?', (project_id,)).fetchone()
        if story is None:
            raise HTTPException(404, "项目不存在或缺少 Story 记录")
        story_id = story["id"]
        existing_ids = {
            r["id"] for r in conn.execute('SELECT id FROM "Scene" WHERE storyId = ?', (story_id,)).fetchall()
        }
        if set(body.sceneIds) != existing_ids or len(body.sceneIds) != len(existing_ids):
            raise HTTPException(400, "sceneIds 必须是这个项目下全部场次 id 的一个排列，不能多也不能少")
        for order, scene_id in enumerate(body.sceneIds):
            conn.execute('UPDATE "Scene" SET "order" = ? WHERE id = ?', (order, scene_id))

    return {"reordered": len(body.sceneIds)}


@router.delete("/{project_id}")
def delete_project(project_id: str):
    """删掉整个项目，连同它下面的场次/镜头/素材/角色库/剧本记录一起删。开着外键约束
    (PRAGMA foreign_keys = ON)，所以必须严格按"子表先删、父表后删"的顺序：
    Asset -> Shot -> Scene -> Character -> Story -> Project。跟删场次/删镜头一样，
    只清数据库记录，已经生成在磁盘上的图片/视频/配音文件不会被顺带删除。
    """
    with get_connection() as conn:
        project = conn.execute('SELECT id FROM "Project" WHERE id = ?', (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, "项目不存在")
        story = conn.execute('SELECT id FROM "Story" WHERE projectId = ?', (project_id,)).fetchone()
        if story is not None:
            story_id = story["id"]
            _clear_story_scenes(conn, story_id)
            conn.execute('DELETE FROM "Character" WHERE storyId = ?', (story_id,))
            conn.execute('DELETE FROM "Story" WHERE id = ?', (story_id,))
        conn.execute('DELETE FROM "Project" WHERE id = ?', (project_id,))
    return {"deleted": project_id}
