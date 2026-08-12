from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, new_id, now_iso

# 海报"类型"库：医院海报/地陪翻译/科普知识/价格表/知识卡片……都是这里的一条条模版，
# 用户可以自己增删改，不用等改代码才能加新的业务场景。db.py 的 _ensure_startup_
# migrations 会在这张表首次建表时预置 5 条覆盖常见场景。跟 /posters 平级，是完全
# 独立的一份小清单，不挂在任何海报或项目下面——模版被删除不影响已经用它生成过的海报
# (Poster.promptText/templateLabel 是生成当下复制的快照，不是引用)。
router = APIRouter(prefix="/poster-templates", tags=["poster-templates"])

LayoutMode = Literal["title", "textBlocks"]


def _serialize(row) -> dict:
    return dict(row)


@router.get("")
def list_poster_templates():
    with get_connection() as conn:
        rows = conn.execute('SELECT * FROM "PosterTemplate" ORDER BY createdAt DESC').fetchall()
    return [_serialize(r) for r in rows]


class CreatePosterTemplateBody(BaseModel):
    label: str
    promptText: str
    # title: 标题+副标题两行字，适合宣传氛围类海报；textBlocks: 标题+任意多行正文，
    # 适合价格表/知识卡片这种需要精确罗列具体文字内容的场景。只是给前端一个默认值，
    # 生成海报时用户仍然可以在创建表单里调整。
    layoutMode: LayoutMode = "title"


@router.post("")
def create_poster_template(body: CreatePosterTemplateBody):
    label = body.label.strip()
    prompt_text = body.promptText.strip()
    if not label:
        raise HTTPException(400, "模版名称不能为空")
    if not prompt_text:
        raise HTTPException(400, "模版提示词不能为空")

    template_id = new_id()
    with get_connection() as conn:
        conn.execute(
            'INSERT INTO "PosterTemplate" (id, label, promptText, layoutMode, createdAt) VALUES (?, ?, ?, ?, ?)',
            (template_id, label, prompt_text, body.layoutMode, now_iso()),
        )
        row = conn.execute('SELECT * FROM "PosterTemplate" WHERE id = ?', (template_id,)).fetchone()
    return _serialize(row)


@router.delete("/{template_id}")
def delete_poster_template(template_id: str):
    with get_connection() as conn:
        template = conn.execute('SELECT id FROM "PosterTemplate" WHERE id = ?', (template_id,)).fetchone()
        if template is None:
            raise HTTPException(404, "模版不存在")
        conn.execute('DELETE FROM "PosterTemplate" WHERE id = ?', (template_id,))
    return {"deleted": template_id}
