import json
import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, get_settings, new_id, now_iso
from app.providers.seedream import (
    DEFAULT_POSTER_ORIENTATION,
    DEFAULT_STYLE_MODE,
    POSTER_ORIENTATIONS,
    generate_poster_background,
)
from app.services.paths import to_static_url
from app.services.poster_composer import PosterComposeError, compose_poster

# 海报是独立的一级功能，不挂在任何 Project 详情页下面——建海报不需要先建视频项目、
# 写完剧本才能出海报，所以路由前缀是顶层 /posters，不是 /projects/{project_id}/posters。
router = APIRouter(prefix="/posters", tags=["posters"])

StyleMode = Literal["comic", "realistic", "render3d", "freeform"]
Orientation = Literal["portrait", "landscape", "9:16", "1:1", "4:3"]
LayoutMode = Literal["title", "textBlocks"]


def _parse_body_lines(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _serialize(row) -> dict:
    d = dict(row)
    d["url"] = to_static_url(d.get("filePath"))
    d["backgroundUrl"] = to_static_url(d.get("backgroundPath"))
    d["orientationLabel"] = POSTER_ORIENTATIONS.get(d.get("orientation"), {}).get("label", d.get("orientation"))
    d["bodyLines"] = _parse_body_lines(d.get("bodyLines"))
    return d


def _split_paths(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


@router.get("")
def list_posters():
    """列出所有海报，跟视频项目完全独立，不按 projectId 过滤——海报列表就是一份
    扁平的、全局的海报清单，最新生成的排最前面。"""
    with get_connection() as conn:
        rows = conn.execute('SELECT * FROM "Poster" ORDER BY createdAt DESC').fetchall()
    return [_serialize(r) for r in rows]


@router.get("/options")
def list_poster_options():
    """给前端渲染"朝向"选择器用，label 和 id 都在这，不用前端自己维护一份重复的
    列表。"类型"选择器改成从 GET /poster-templates 拉取——那是一份用户能自己
    增删改的模版清单，不再是这里写死的固定几个。"""
    return {
        "orientations": [{"id": oid, "label": cfg["label"]} for oid, cfg in POSTER_ORIENTATIONS.items()],
    }


class CreatePosterBody(BaseModel):
    orientation: Orientation = DEFAULT_POSTER_ORIENTATION
    # 选一个已保存的模版(从 PosterTemplate 复用它的 promptText/layoutMode)，或者不选、
    # 自己临时写一次性的提示词(这时 promptText 必填)。两者选一个即可。
    templateId: Optional[str] = None
    promptText: Optional[str] = None
    # 只有没选模版时才生效，决定排版方式；选了模版就用模版自己的 layoutMode，
    # 忽略这个字段。
    layoutMode: LayoutMode = "title"
    # layoutMode 最终解析成 'textBlocks' 时必填：每一项是一行正文，支持
    # "项目名|价格" 这种竖线分隔的两栏格式(价格右对齐)，价格表/知识卡片场景用。
    bodyLines: Optional[list[str]] = None
    styleMode: StyleMode = DEFAULT_STYLE_MODE
    title: str
    subtitle: Optional[str] = None
    extraPrompt: Optional[str] = None
    # 逗号分隔的本地文件路径，跟分镜/场景参考图输入框同一个约定，可以是角色设定图/
    # 场景参考图，也可以是用户手动选的任意本地图片(比如一张实拍参考照)。
    referenceImagePaths: Optional[str] = None
    # 可选：备注这张海报是照哪个视频项目的调子出的，纯粹是提示性字段，不影响生成逻辑，
    # 项目被删掉时这里会自动置空(外键 ON DELETE SET NULL)，不会连累海报被删。
    projectId: Optional[str] = None


def _resolve_content(conn, template_id: Optional[str], prompt_text: Optional[str], layout_mode: str):
    """把"选模版"或"临时手写提示词"两种输入统一解析成
    (templateId, templateLabel, promptText, layoutMode) 四元组，存进 Poster 行时是
    一份快照——模版之后被改名/改提示词/删除，都不影响回看这张海报当初的设置。"""
    if template_id:
        template = conn.execute('SELECT * FROM "PosterTemplate" WHERE id = ?', (template_id,)).fetchone()
        if template is None:
            raise HTTPException(404, "选择的模版不存在")
        return template_id, template["label"], template["promptText"], template["layoutMode"]
    resolved_prompt = (prompt_text or "").strip()
    if not resolved_prompt:
        raise HTTPException(400, "请选择一个模版，或者填写提示词")
    return None, None, resolved_prompt, layout_mode


@router.post("")
def create_poster(body: CreatePosterBody):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")

    with get_connection() as conn:
        template_id, template_label, prompt_text, layout_mode = _resolve_content(
            conn, body.templateId, body.promptText, body.layoutMode
        )

        body_lines_json = None
        if layout_mode == "textBlocks":
            lines = [line.strip() for line in (body.bodyLines or []) if line and line.strip()]
            if not lines:
                raise HTTPException(400, "多行正文排版至少要填一行内容")
            body_lines_json = json.dumps(lines, ensure_ascii=False)

        if body.projectId:
            project = conn.execute('SELECT id FROM "Project" WHERE id = ?', (body.projectId,)).fetchone()
            if project is None:
                raise HTTPException(404, "关联的项目不存在")

        poster_id = new_id()
        conn.execute(
            'INSERT INTO "Poster" (id, projectId, orientation, templateId, templateLabel, promptText, '
            "layoutMode, bodyLines, styleMode, title, subtitle, extraPrompt, referenceImagePaths, status, "
            "createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                poster_id,
                body.projectId,
                body.orientation,
                template_id,
                template_label,
                prompt_text,
                layout_mode,
                body_lines_json,
                body.styleMode,
                title,
                body.subtitle,
                body.extraPrompt,
                body.referenceImagePaths,
                "running",
                now_iso(),
            ),
        )

    thread = threading.Thread(
        target=_run_poster_generation,
        args=(
            poster_id,
            body.orientation,
            prompt_text,
            body.styleMode,
            body.projectId,
            body.extraPrompt,
            _split_paths(body.referenceImagePaths),
            title,
            body.subtitle,
            layout_mode,
            _parse_body_lines(body_lines_json),
        ),
        daemon=True,
    )
    thread.start()

    return {"posterId": poster_id, "status": "running"}


def _run_poster_generation(
    poster_id: str,
    orientation: str,
    content_prompt: Optional[str],
    style_mode: str,
    project_id: Optional[str],
    extra_prompt: Optional[str],
    reference_paths: list[str],
    title: str,
    subtitle: Optional[str],
    layout_mode: str,
    body_lines: list[str],
) -> None:
    try:
        bg = generate_poster_background(
            poster_id,
            orientation,
            content_prompt or "",
            style_mode=style_mode,
            project_id=project_id,
            extra_prompt=extra_prompt,
            reference_image_paths=reference_paths or None,
        )
        with get_connection() as conn:
            font_path = get_settings(conn).get("posterFontPath")
        dest = str(Path(bg["filePath"]).parent / "poster.png")
        compose_poster(
            background_path=bg["filePath"],
            title=title,
            subtitle=subtitle,
            dest_path=dest,
            font_path=font_path,
            body_lines=body_lines if layout_mode == "textBlocks" else None,
        )
        with get_connection() as conn:
            conn.execute(
                'UPDATE "Poster" SET status = ?, backgroundPath = ?, filePath = ?, providerId = ?, model = ?, '
                "error = NULL WHERE id = ?",
                ("completed", bg["filePath"], dest, bg.get("providerId"), bg.get("model"), poster_id),
            )
    except Exception as exc:  # noqa: BLE001
        with get_connection() as conn:
            conn.execute('UPDATE "Poster" SET status = ?, error = ? WHERE id = ?', ("failed", str(exc), poster_id))


@router.post("/{poster_id}/regenerate")
def regenerate_poster(poster_id: str):
    """重新走一遍完整流程(重新调 Seedream 出新背景 + 用当前存的标题/副标题/正文重新
    叠字)。只想改文字、背景不用换的话，用下面的 PATCH，不用重新调 AI。已经把
    promptText/layoutMode/bodyLines 存成快照了，不需要再去查模版表。"""
    with get_connection() as conn:
        poster = conn.execute('SELECT * FROM "Poster" WHERE id = ?', (poster_id,)).fetchone()
        if poster is None:
            raise HTTPException(404, "海报不存在")
        conn.execute('UPDATE "Poster" SET status = ?, error = NULL WHERE id = ?', ("running", poster_id))

    thread = threading.Thread(
        target=_run_poster_generation,
        args=(
            poster_id,
            poster["orientation"],
            poster["promptText"],
            poster["styleMode"],
            poster["projectId"],
            poster["extraPrompt"],
            _split_paths(poster["referenceImagePaths"]),
            poster["title"],
            poster["subtitle"],
            poster["layoutMode"],
            _parse_body_lines(poster["bodyLines"]),
        ),
        daemon=True,
    )
    thread.start()

    return {"posterId": poster_id, "status": "running"}


class UpdatePosterTextBody(BaseModel):
    title: Optional[str] = None
    subtitle: Optional[str] = None
    # 只有 layoutMode='textBlocks' 的海报会用到；传了就整个列表替换(不是增量合并)。
    bodyLines: Optional[list[str]] = None


@router.patch("/{poster_id}")
def update_poster_text(poster_id: str, body: UpdatePosterTextBody):
    """只改标题/副标题/正文文字。已经有背景图的话就地重新叠字(同步执行，Pillow 排版
    很快，不用像调 AI 那样走后台线程+轮询)；还没生成出背景图的话就只是先把文字存下来，
    等生成/重新生成的时候会用最新的文字。"""
    fields: dict = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.subtitle is not None:
        fields["subtitle"] = body.subtitle
    if body.bodyLines is not None:
        fields["bodyLines"] = json.dumps([line for line in body.bodyLines if line and line.strip()], ensure_ascii=False)
    if not fields:
        raise HTTPException(400, "没有要更新的字段")

    with get_connection() as conn:
        poster = conn.execute('SELECT * FROM "Poster" WHERE id = ?', (poster_id,)).fetchone()
        if poster is None:
            raise HTTPException(404, "海报不存在")

        set_clause = ", ".join(f'"{k}" = ?' for k in fields)
        conn.execute(f'UPDATE "Poster" SET {set_clause} WHERE id = ?', (*fields.values(), poster_id))

        title = fields.get("title", poster["title"])
        subtitle = fields.get("subtitle", poster["subtitle"])
        body_lines = _parse_body_lines(fields.get("bodyLines", poster["bodyLines"]))

        if poster["backgroundPath"]:
            font_path = get_settings(conn).get("posterFontPath")
            try:
                compose_poster(
                    background_path=poster["backgroundPath"],
                    title=title,
                    subtitle=subtitle,
                    dest_path=poster["filePath"] or poster["backgroundPath"],
                    font_path=font_path,
                    body_lines=body_lines if poster["layoutMode"] == "textBlocks" else None,
                )
                conn.execute('UPDATE "Poster" SET error = NULL WHERE id = ?', (poster_id,))
            except PosterComposeError as exc:
                raise HTTPException(400, str(exc)) from exc

        row = conn.execute('SELECT * FROM "Poster" WHERE id = ?', (poster_id,)).fetchone()

    return _serialize(row)


@router.delete("/{poster_id}")
def delete_poster(poster_id: str):
    with get_connection() as conn:
        poster = conn.execute('SELECT id FROM "Poster" WHERE id = ?', (poster_id,)).fetchone()
        if poster is None:
            raise HTTPException(404, "海报不存在")
        conn.execute('DELETE FROM "Poster" WHERE id = ?', (poster_id,))
    return {"deleted": poster_id}
