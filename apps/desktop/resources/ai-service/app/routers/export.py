from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, get_settings
from app.services.exporter import ExportError, export_project_video
from app.services.paths import to_static_url

router = APIRouter(prefix="/projects", tags=["export"])


class ExportBody(BaseModel):
    # 不传就用设置页里保存的默认值(exportBurnSubtitles)；显式传 true/false 时以请求为准，
    # 这样前端"这次导出要不要烧字幕"的临时勾选，能覆盖掉设置页的默认选项。
    burnSubtitles: Optional[bool] = None


@router.post("/{project_id}/export")
def export_project(project_id: str, body: ExportBody = ExportBody()):
    with get_connection() as conn:
        project = conn.execute('SELECT id FROM "Project" WHERE id = ?', (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, "项目不存在")

        story = conn.execute('SELECT id FROM "Story" WHERE projectId = ?', (project_id,)).fetchone()
        if story is None:
            raise HTTPException(400, "项目还没有剧本")

        rows = conn.execute(
            """
            SELECT s.id, s.dialogue, s.durationSec, sc."order" AS sceneOrder, s."order" AS shotOrder
            FROM "Shot" s
            JOIN "Scene" sc ON s.sceneId = sc.id
            WHERE sc.storyId = ?
            ORDER BY sc."order", s."order"
            """,
            (story["id"],),
        ).fetchall()

        shots_with_video = []
        for row in rows:
            asset = conn.execute(
                'SELECT filePath FROM "Asset" WHERE shotId = ? AND type = ? AND status = ? '
                'ORDER BY selected DESC, createdAt DESC LIMIT 1',
                (row["id"], "video", "completed"),
            ).fetchone()
            shots_with_video.append(
                {
                    "videoPath": asset["filePath"] if asset else None,
                    "dialogue": row["dialogue"],
                    "durationSec": row["durationSec"],
                    "sceneOrder": row["sceneOrder"],
                    "shotOrder": row["shotOrder"],
                }
            )

    if body.burnSubtitles is None:
        with get_connection() as conn:
            burn_subtitles = get_settings(conn).get("exportBurnSubtitles", True)
    else:
        burn_subtitles = body.burnSubtitles

    try:
        final_path, skipped = export_project_video(project_id, shots_with_video, burn_subtitles=burn_subtitles)
    except ExportError as exc:
        raise HTTPException(400, str(exc)) from exc

    # 导出真的跑成功了才记这个时间戳——项目列表的"已导出"标签靠它判断，跑失败/跑到一半
    # 抛异常的不应该被当成"已经导出过一次"。
    with get_connection() as conn:
        conn.execute(
            'UPDATE "Project" SET lastExportedAt = ? WHERE id = ?',
            (datetime.now(timezone.utc).isoformat(), project_id),
        )

    # exportDir 设成了 output 目录之外的地方时 url 会是 None（安全起见不对外暴露任意
    # 文件系统路径的 HTTP 访问），filePath 这时候是唯一能定位成片的信息，前端要显示出来。
    return {
        "filePath": final_path,
        "url": to_static_url(final_path),
        # 缺视频被跳过的镜头，前端拿去提示"第几场第几镜没进成片"，场次/镜号都是从0开始存的，
        # 这里 +1 换成人看的"第几场第几镜"。
        "skippedShots": [
            {"sceneOrder": s["sceneOrder"] + 1, "shotOrder": s["shotOrder"] + 1} for s in skipped
        ],
    }
