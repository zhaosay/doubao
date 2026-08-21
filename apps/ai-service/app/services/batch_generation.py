""""总览模式"里几个"一键生成"按钮背后的批量触发逻辑：角色/场景/分镜图片/分镜视频/
分镜配音，各自独立、互不牵动——用户可以只点其中一个，不会连带触发别的。

这跟之前设计的"一键生成全片(剧本->角色->场景->图片->视频->自动导出，一个大编排器)"
是两回事：讨论下来用户真正想要的是总览页面能一眼看完参考图和所有分镜画面，自己确认
没问题了，再挑着点"分镜视频一键生成"之类的按钮，而不是一个不透明的大黑盒从头跑到尾。
所以这里没有整体的"运行状态"字段——每一类批量操作跑得怎么样，直接看具体那条
Character/Scene/Asset 记录自己的 status 字段就行(前端已经在轮询这些)，不需要在
Project 表上额外加一层"这一次批量任务跑完了没"的状态。

每一类内部用有限并发(见 _BATCH_CONCURRENCY)，避免同时打太多请求过去撞 Ark 配额/
速率限制；具体的单项生成函数(角色/场景/分镜)本身已经有失败重试逻辑，这里只负责
"挑出还没完成的有哪些，并发跑一遍"。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.db import get_connection
from app.routers.characters import _run_character_generation, list_characters
from app.routers.scenes import _run_scene_generation
from app.routers.shots import GenerateAssetBody, _latest_completed_asset_path, _prepare_running_asset, _run_generation

_BATCH_CONCURRENCY = 3


def generate_all_characters(project_id: str) -> None:
    """角色一键生成：先跑一遍"从 Shot.characterName 同步新角色"的逻辑(list_characters
    本身就会做这件事)，再把所有还没 completed 的角色并发生成一遍。"""
    all_chars = list_characters(project_id)
    pending = [c for c in all_chars if c.get("status") != "completed"]
    if not pending:
        return

    with get_connection() as conn:
        for c in pending:
            conn.execute('UPDATE "Character" SET status = ?, error = NULL WHERE id = ?', ("running", c["id"]))

    with ThreadPoolExecutor(max_workers=_BATCH_CONCURRENCY) as ex:
        futures = [ex.submit(_run_character_generation, c["id"], c["name"], c.get("prompt")) for c in pending]
        for fut in as_completed(futures):
            fut.result()  # 生成函数自己兜底了所有异常并写回 DB，这里理论上不会抛


def generate_all_scenes(story_id: str) -> None:
    """场景参考图一键生成：这个项目下所有还没 completed 的场次。"""
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT id, summary FROM "Scene" WHERE storyId = ? AND status != ?', (story_id, "completed")
        ).fetchall()
        pending = [dict(r) for r in rows]
        for scene in pending:
            conn.execute('UPDATE "Scene" SET status = ?, error = NULL WHERE id = ?', ("running", scene["id"]))
    if not pending:
        return

    with ThreadPoolExecutor(max_workers=_BATCH_CONCURRENCY) as ex:
        futures = [ex.submit(_run_scene_generation, s["id"], s["summary"], None) for s in pending]
        for fut in as_completed(futures):
            fut.result()


def generate_all_shot_assets(story_id: str, kind: str) -> None:
    """分镜图片/视频/配音一键生成共用的批量逻辑，kind 是 'image'/'video'/'voice'。

    - image/video：挑出这个项目下所有还没有一条 completed 素材的镜头，触发单张生成
      (不走批量候选，跟手动点一次「生成」等价)。
    - voice：多一条过滤——只处理有台词(dialogue 非空)的镜头，没词的镜头本来就没什么
      好配的，直接跳过，不占位生成、也不会因为"台词是空的"报错刷一堆失败提示出来。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id AS id, s.dialogue AS dialogue
            FROM "Shot" s
            JOIN "Scene" sc ON s.sceneId = sc.id
            WHERE sc.storyId = ?
            ORDER BY sc."order", s."order"
            """,
            (story_id,),
        ).fetchall()

        pending_ids = []
        for row in rows:
            if kind == "voice" and not (row["dialogue"] and row["dialogue"].strip()):
                continue
            if _latest_completed_asset_path(conn, row["id"], kind) is None:
                pending_ids.append(row["id"])

        if not pending_ids:
            return

        jobs = [(shot_id, *_prepare_running_asset(conn, shot_id, kind)) for shot_id in pending_ids]

    body = GenerateAssetBody()
    # 视频平台的 429 限流比生图严格得多；视频按顺序提交，图片/配音仍保持 3 路并发。
    max_workers = 1 if kind == "video" else _BATCH_CONCURRENCY
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [
            ex.submit(_run_generation, shot_id, kind, asset_id, task_id, body)
            for shot_id, asset_id, task_id in jobs
        ]
        for fut in as_completed(futures):
            fut.result()
