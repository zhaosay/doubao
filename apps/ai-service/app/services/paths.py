import os
from pathlib import Path
from typing import Optional

APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent.parent.parent
# 打包成安装包之后没有 monorepo 目录结构可用，生成产物也不该写进安装目录——
# Electron 主进程在打包模式下会通过 AI_MANJU_OUTPUT_ROOT 传 userData 目录下的路径
# 进来；开发模式不传，行为跟以前一样(仓库根目录下的 output/)。跟 db.py 的
# AI_MANJU_DB_PATH 是同一个模式。
_env_output_root = os.environ.get("AI_MANJU_OUTPUT_ROOT")
DEFAULT_OUTPUT_ROOT = Path(_env_output_root) if _env_output_root else REPO_ROOT / "output"

# 兼容起见保留这个名字（旧代码/文档里可能还引用它），永远指向默认目录，
# 不随设置里的 outputDir 变化——谁需要"当前生效的"输出目录，去调 get_output_root()。
OUTPUT_ROOT = DEFAULT_OUTPUT_ROOT
OUTPUT_DIR = OUTPUT_ROOT / "generated"


def get_output_root() -> Path:
    """当前生效的生成产物根目录：设置页填了 outputDir 就用那个，没填就用默认的
    <repo>/output。放函数里而不是模块级常量，是因为要支持"改了设置马上生效、
    不用重启 ai-service"——数据库 import 时机太早，得每次用的时候现查。
    延迟 import app.db 避免模块加载顺序上的循环 import。
    """
    from app.db import get_connection, get_settings  # noqa: PLC0415 延迟导入避免循环依赖

    try:
        with get_connection() as conn:
            settings = get_settings(conn)
    except Exception:  # noqa: BLE001 - 数据库还没初始化好之类的情况，直接退化成默认目录
        return DEFAULT_OUTPUT_ROOT

    raw = (settings.get("outputDir") or "").strip()
    if not raw:
        return DEFAULT_OUTPUT_ROOT
    return Path(raw).expanduser()


def project_dir(project_id: Optional[str]) -> Path:
    """项目专属子目录：output/projects/{projectId}/。project_id 查不到（理论上不该发生，
    防御性兜底）就退化成 output/_unknown_project/，不让文件彻底没地方放。
    exporter.py 也复用这个函数，保证角色图/场景图/分镜产物/导出成片都在同一个项目文件夹下。
    """
    return get_output_root() / "projects" / (project_id or "_unknown_project")


def _project_id_for_shot(shot_id: str) -> Optional[str]:
    from app.db import get_connection  # noqa: PLC0415 延迟导入避免循环依赖

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.id AS project_id
            FROM "Project" p
            JOIN "Story" st ON st.projectId = p.id
            JOIN "Scene" sc ON sc.storyId = st.id
            JOIN "Shot" s ON s.sceneId = sc.id
            WHERE s.id = ?
            """,
            (shot_id,),
        ).fetchone()
    return row["project_id"] if row else None


def _project_id_for_character(character_id: str) -> Optional[str]:
    from app.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.id AS project_id
            FROM "Project" p
            JOIN "Story" st ON st.projectId = p.id
            JOIN "Character" c ON c.storyId = st.id
            WHERE c.id = ?
            """,
            (character_id,),
        ).fetchone()
    return row["project_id"] if row else None


def _project_id_for_scene(scene_id: str) -> Optional[str]:
    from app.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.id AS project_id
            FROM "Project" p
            JOIN "Story" st ON st.projectId = p.id
            JOIN "Scene" sc ON sc.storyId = st.id
            WHERE sc.id = ?
            """,
            (scene_id,),
        ).fetchone()
    return row["project_id"] if row else None


def asset_dir(shot_id: str) -> Path:
    """分镜生成产物存在 output/projects/{projectId}/generated/{shotId}/ 下，
    不再是所有项目的分镜混在同一个 output/generated/ 里——项目一多，
    在 Finder 里想手动找某个项目的文件根本分不清是哪个。
    旧数据（这次改动之前生成的文件）路径不变，不受影响，只是新生成的文件走新布局。
    """
    project_id = _project_id_for_shot(shot_id)
    d = project_dir(project_id) / "generated" / shot_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def character_dir(character_id: str) -> Path:
    project_id = _project_id_for_character(character_id)
    d = project_dir(project_id) / "characters" / character_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def scene_dir(scene_id: str) -> Path:
    project_id = _project_id_for_scene(scene_id)
    d = project_dir(project_id) / "scenes" / scene_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def poster_dir(poster_id: str, project_id: Optional[str] = None) -> Path:
    """海报是独立功能，project_id 是可选的备注性关联，大多数情况下是 None——
    这时候就放到 output/posters/{posterId}/ 下，不挂在任何项目文件夹里；
    传了 project_id 就还是放进那个项目的子目录，方便手动整理。"""
    if project_id:
        d = project_dir(project_id) / "posters" / poster_id
    else:
        d = get_output_root() / "posters" / poster_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def video_gen_dir(video_id: str, project_id: Optional[str] = None) -> Path:
    """无剧本图生视频是独立功能，project_id 大多数情况下是 None——这时候放到
    output/video_generations/{id}/ 下，不挂在任何项目文件夹里；传了 project_id
    就还是放进那个项目的子目录，方便手动整理，跟 poster_dir 同一个约定。"""
    if project_id:
        d = project_dir(project_id) / "video_generations" / video_id
    else:
        d = get_output_root() / "video_generations" / video_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def text_image_dir(image_id: str, project_id: Optional[str] = None) -> Path:
    """独立文生图，跟 video_gen_dir/poster_dir 同一个约定。"""
    if project_id:
        d = project_dir(project_id) / "text_images" / image_id
    else:
        d = get_output_root() / "text_images" / image_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def to_static_url(file_path: Optional[str]) -> Optional[str]:
    """把生成产物目录下的绝对路径转成 /files/... 相对 URL，配合 main.py 里
    /files/{path} 的动态读取路由，这样渲染进程能直接用 http://127.0.0.1:8000/files/...
    加载图片/视频，不用碰 file:// 协议（Electron 渲染进程从 http(s) 源加载 file://
    资源经常被 Chromium 拦掉）。

    同时兼容目录设置改过之后的旧文件：先按当前生效目录算相对路径，算不出来
    （说明这个文件是在旧目录设置下生成的）再退回默认目录试一次，都不行才判 None。
    这样改了 outputDir 之后，之前生成的图片/视频缩略图不会突然全部裂图。
    """
    if not file_path:
        return None
    resolved = Path(file_path).resolve()
    for root in {get_output_root().resolve(), DEFAULT_OUTPUT_ROOT.resolve()}:
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        return "/files/" + rel.as_posix()
    return None


def resolve_static_file(rel_path: str) -> Optional[Path]:
    """/files/{rel_path} 路由用：按当前生效目录找文件，找不到再退回默认目录，
    跟 to_static_url 的兼容逻辑对称。返回真实存在的文件路径，都找不到返回 None。
    """
    for root in {get_output_root(), DEFAULT_OUTPUT_ROOT}:
        candidate = (root / rel_path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue  # 路径穿越(../)保护，不允许跳出根目录
        if candidate.is_file():
            return candidate
    return None
