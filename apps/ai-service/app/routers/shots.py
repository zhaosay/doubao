import re
import threading
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, new_id, now_iso
from app.providers.registry import registry
from app.services.paths import to_static_url

_SPLIT_NAMES_RE = re.compile(r"[、,，/]")

router = APIRouter(prefix="/shots", tags=["shots"])

ASSET_KINDS = ("image", "video", "voice")


class UpdateShotBody(BaseModel):
    sceneType: Optional[str] = None
    drawPrompt: Optional[str] = None
    motionPrompt: Optional[str] = None
    dialogue: Optional[str] = None
    durationSec: Optional[float] = None
    characterName: Optional[str] = None
    # 这一镜接下一镜(不管是否跨场次)用什么转场手法，取自分镜手册"转场"词条。
    # 传空字符串表示清空(不能传 None，因为 None 会被下面的 model_dump 过滤掉当"没传")。
    transitionToNext: Optional[str] = None
    # 这一镜的情绪/表演基调，取自分镜手册"情绪"词条，跟 sceneType 同一个"独立小字段"模式。
    emotion: Optional[str] = None


class GenerateAssetBody(BaseModel):
    # image 用：参考图本地路径（比如角色设定图），不传就是纯文生图
    referenceImagePaths: Optional[list[str]] = None
    # video 用：起始帧本地路径，不传则自动取该分镜最近一次生成成功的 image 素材
    startImagePath: Optional[str] = None
    # voice 用
    referenceAudioPath: Optional[str] = None
    voiceId: Optional[str] = None
    speed: float = 1.0
    # 批量候选生成用：一次生成几张备选（目前只对 kind=image 生效，video/voice 忽略这个字段，
    # 因为视频生成慢/贵，配音候选选优意义不大，先不做）。>1 时不会覆盖旧素材，
    # 而是作为新的候选行插入，全部生成完之后需要调用 select 接口挑一张生效。
    count: int = 1


def _get_shot_or_404(conn, shot_id: str):
    shot = conn.execute('SELECT * FROM "Shot" WHERE id = ?', (shot_id,)).fetchone()
    if shot is None:
        raise HTTPException(404, "分镜不存在")
    return shot


def _latest_completed_asset_path(conn, shot_id: str, kind: str) -> Optional[str]:
    """取这个 shot+type 下游要用的那条素材路径：优先选中(selected=1)的那条；
    如果批量生成了候选还没手动选（全都是 selected=0），退化成"最新生成完成的那条"，
    保证不用户不点选也能继续走完流程，选了之后才会真正生效。
    """
    row = conn.execute(
        'SELECT filePath FROM "Asset" WHERE shotId = ? AND type = ? AND status = ? '
        'ORDER BY selected DESC, createdAt DESC LIMIT 1',
        (shot_id, kind, "completed"),
    ).fetchone()
    return row["filePath"] if row else None


def _resolve_character_reference_paths(conn, shot: dict) -> Optional[list[str]]:
    """按 Shot.characterName 去角色库找已经生成好的角色设定图，自动当 reference_images 用，
    不用每次手动填参考图路径——这是解决"角色一致性"的关键一环。找不到就返回 None，
    退化成纯文生图（还是会带风格前缀，但没有图片锚点，一致性没法保证）。
    """
    raw_names = shot.get("characterName")
    if not raw_names:
        return None
    names = [n.strip() for n in _SPLIT_NAMES_RE.split(raw_names) if n.strip()]
    if not names:
        return None

    scene = conn.execute('SELECT storyId FROM "Scene" WHERE id = ?', (shot["sceneId"],)).fetchone()
    if scene is None:
        return None

    paths: list[str] = []
    for name in names:
        row = conn.execute(
            'SELECT refImagePath FROM "Character" WHERE storyId = ? AND name = ? AND status = ?',
            (scene["storyId"], name, "completed"),
        ).fetchone()
        if row and row["refImagePath"]:
            paths.append(row["refImagePath"])

    return paths or None


def _resolve_scene_reference_path(conn, shot: dict) -> Optional[str]:
    """场景环境母版图：跟角色参考图同一个思路，只是锁的是背景/光线/色调而不是人物。
    没生成过（或生成失败）就返回 None，退化成没有环境锚点，不阻塞流程。
    """
    scene = conn.execute(
        'SELECT refImagePath, status FROM "Scene" WHERE id = ?', (shot["sceneId"],)
    ).fetchone()
    if scene is None or scene["status"] != "completed" or not scene["refImagePath"]:
        return None
    return scene["refImagePath"]


def _run_generation(shot_id: str, kind: str, asset_id: str, task_id: str, body: GenerateAssetBody) -> None:
    """在后台线程跑真正的生成调用，独立开 DB 连接，结束后把结果/报错写回 Asset 和 Task。"""
    try:
        with get_connection() as conn:
            shot = dict(_get_shot_or_404(conn, shot_id))

        provider = registry.resolve(kind, "default")

        if kind == "image":
            reference_image_paths = body.referenceImagePaths
            if not reference_image_paths:
                # 手动没传参考图时，自动拼「角色参考图 + 场景环境母版图」一起传给 Seedream：
                # 角色管人物一致性，场景管背景/光线/色调一致性，两者互不冲突，都传上更稳。
                with get_connection() as conn:
                    character_paths = _resolve_character_reference_paths(conn, shot) or []
                    scene_path = _resolve_scene_reference_path(conn, shot)
                reference_image_paths = character_paths + ([scene_path] if scene_path else [])
                reference_image_paths = reference_image_paths or None
            result = provider.generate_image(
                shot_id=shot_id,
                prompt=shot["drawPrompt"],
                reference_image_paths=reference_image_paths,
            )
        elif kind == "video":
            start_image_path = body.startImagePath
            if not start_image_path:
                with get_connection() as conn:
                    start_image_path = _latest_completed_asset_path(conn, shot_id, "image")
            if not start_image_path:
                raise RuntimeError(
                    "生成视频需要先有一张已完成的分镜静态图：先点「生成图片」，或者手动传 startImagePath"
                )
            result = provider.generate_video(
                shot_id=shot_id,
                start_image_path=start_image_path,
                end_image_path=None,
                prompt=shot["motionPrompt"] or shot["drawPrompt"],
            )
        elif kind == "voice":
            text = shot["dialogue"]
            if not text:
                raise RuntimeError("这个分镜的台词(dialogue)是空的，没有内容可配音")
            result = provider.generate_voice(
                shot_id=shot_id,
                text=text,
                reference_audio_path=body.referenceAudioPath,
                voice_id=body.voiceId,
                speed=body.speed,
            )
        else:
            raise RuntimeError(f"不支持的素材类型: {kind}")

        with get_connection() as conn:
            conn.execute(
                'UPDATE "Asset" SET status = ?, filePath = ?, providerId = ?, model = ?, error = NULL WHERE id = ?',
                ("completed", result["filePath"], result.get("providerId"), result.get("model"), asset_id),
            )
            conn.execute(
                'UPDATE "Task" SET status = ?, updatedAt = ? WHERE id = ?', ("completed", now_iso(), task_id)
            )

    except Exception as exc:  # noqa: BLE001 - 故意兜底所有异常，把原因完整透传给前端而不是让线程静默死掉
        with get_connection() as conn:
            conn.execute(
                'UPDATE "Asset" SET status = ?, error = ? WHERE id = ?', ("failed", str(exc), asset_id)
            )
            conn.execute(
                'UPDATE "Task" SET status = ?, error = ?, updatedAt = ? WHERE id = ?',
                ("failed", str(exc), now_iso(), task_id),
            )


@router.patch("/{shot_id}")
def update_shot(shot_id: str, body: UpdateShotBody):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "没有要更新的字段")

    with get_connection() as conn:
        _get_shot_or_404(conn, shot_id)
        set_clause = ", ".join(f'"{k}" = ?' for k in fields)
        conn.execute(
            f'UPDATE "Shot" SET {set_clause} WHERE id = ?',
            (*fields.values(), shot_id),
        )
        row = conn.execute('SELECT * FROM "Shot" WHERE id = ?', (shot_id,)).fetchone()

    return dict(row)


@router.delete("/{shot_id}")
def delete_shot(shot_id: str):
    """删掉一个镜头，连同它已生成的素材一起删(外键限制，得先删 Asset 再删 Shot)。"""
    with get_connection() as conn:
        _get_shot_or_404(conn, shot_id)
        conn.execute('DELETE FROM "Asset" WHERE shotId = ?', (shot_id,))
        conn.execute('DELETE FROM "Shot" WHERE id = ?', (shot_id,))
    return {"deleted": shot_id}


@router.delete("/{shot_id}/{kind}")
def delete_shot_asset(shot_id: str, kind: Literal["image", "video", "voice"]):
    """删掉某个分镜某一类型的素材（含所有候选），不删镜头本身。用于"这一镜暂时不要配音/
    视频先不生成了"这类场景——比如导出时想跳过这一镜，或者配错音了想清掉重来，
    而不是把整个镜头删了重建。"""
    if kind not in ASSET_KINDS:
        raise HTTPException(400, f"不支持的素材类型: {kind}")
    with get_connection() as conn:
        _get_shot_or_404(conn, shot_id)
        conn.execute('DELETE FROM "Asset" WHERE shotId = ? AND type = ?', (shot_id, kind))
    return {"deleted": True}


@router.get("/{shot_id}/assets")
def list_shot_assets(shot_id: str):
    with get_connection() as conn:
        _get_shot_or_404(conn, shot_id)
        rows = conn.execute(
            'SELECT * FROM "Asset" WHERE shotId = ? ORDER BY createdAt DESC', (shot_id,)
        ).fetchall()
    return [{**dict(r), "url": to_static_url(r["filePath"])} for r in rows]


@router.post("/{shot_id}/{kind}")
def trigger_asset_generation(
    shot_id: str, kind: Literal["image", "video", "voice"], body: GenerateAssetBody = GenerateAssetBody()
):
    """
    真正触发生成：落 Asset/Task 记录为 running，起一个后台线程调用对应 Provider
    （Seedream/Seedance/IndexTTS），完成后回写状态。接口本身立刻返回，前端轮询
    GET /shots/{shot_id}/assets 或 GET /tasks/{task_id} 看进度。

    count>1（只对 kind=image 生效）时走批量候选模式：不覆盖旧素材，而是把旧素材和
    新候选都降级成 selected=0，插入 count 条新的 running 候选行，各开一个线程并行生成，
    全部结束后前端展示候选画廊，调用 select 接口挑一张。
    """
    if kind not in ASSET_KINDS:
        raise HTTPException(400, f"不支持的素材类型: {kind}")

    count = body.count if kind == "image" else 1
    if count < 1:
        raise HTTPException(400, "count 必须 >= 1")

    items: list[dict] = []
    with get_connection() as conn:
        _get_shot_or_404(conn, shot_id)

        if count > 1:
            # 批量候选模式：先把这个 shot+kind 下所有旧素材（含之前选中的那条）降级，
            # 避免出现"新候选都还没选、旧素材却还是 selected=1"这种模糊状态。
            conn.execute(
                'UPDATE "Asset" SET selected = 0 WHERE shotId = ? AND type = ?', (shot_id, kind)
            )
            for _ in range(count):
                asset_id = new_id()
                conn.execute(
                    'INSERT INTO "Asset" (id, shotId, type, status, selected) VALUES (?, ?, ?, ?, ?)',
                    (asset_id, shot_id, kind, "running", False),
                )
                task_id = new_id()
                conn.execute(
                    'INSERT INTO "Task" (id, targetType, targetId, kind, status, updatedAt) '
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, kind, shot_id, kind, "running", now_iso()),
                )
                items.append({"assetId": asset_id, "taskId": task_id})
        else:
            # 单素材模式（原有行为不变）：原地覆盖同一行，一直是隐含的"选中"状态。
            existing = conn.execute(
                'SELECT id FROM "Asset" WHERE shotId = ? AND type = ?', (shot_id, kind)
            ).fetchone()
            if existing:
                asset_id = existing["id"]
                conn.execute(
                    'UPDATE "Asset" SET status = ?, error = NULL WHERE id = ?',
                    ("running", asset_id),
                )
            else:
                asset_id = new_id()
                conn.execute(
                    'INSERT INTO "Asset" (id, shotId, type, status) VALUES (?, ?, ?, ?)',
                    (asset_id, shot_id, kind, "running"),
                )
            task_id = new_id()
            conn.execute(
                'INSERT INTO "Task" (id, targetType, targetId, kind, status, updatedAt) '
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, kind, shot_id, kind, "running", now_iso()),
            )
            items.append({"assetId": asset_id, "taskId": task_id})

    for item in items:
        thread = threading.Thread(
            target=_run_generation,
            args=(shot_id, kind, item["assetId"], item["taskId"], body),
            daemon=True,
        )
        thread.start()

    if len(items) == 1:
        return {"assetId": items[0]["assetId"], "taskId": items[0]["taskId"], "status": "running"}
    return {"items": items, "status": "running"}


@router.post("/{shot_id}/{kind}/{asset_id}/select")
def select_asset(shot_id: str, kind: Literal["image", "video", "voice"], asset_id: str):
    """从同一个 shot+kind 的多个候选素材里选定一条生效：这条 selected=1，
    同 shot+kind 的其它候选全部 selected=0。只能选 status=completed 的素材。
    """
    with get_connection() as conn:
        _get_shot_or_404(conn, shot_id)
        asset = conn.execute(
            'SELECT id, status FROM "Asset" WHERE id = ? AND shotId = ? AND type = ?',
            (asset_id, shot_id, kind),
        ).fetchone()
        if asset is None:
            raise HTTPException(404, "候选素材不存在")
        if asset["status"] != "completed":
            raise HTTPException(400, f"只能选择已完成的素材，当前状态: {asset['status']}")

        conn.execute(
            'UPDATE "Asset" SET selected = 0 WHERE shotId = ? AND type = ?', (shot_id, kind)
        )
        conn.execute('UPDATE "Asset" SET selected = 1 WHERE id = ?', (asset_id,))

    return {"selectedAssetId": asset_id}
