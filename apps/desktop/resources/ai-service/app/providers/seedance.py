from app.db import get_connection, get_settings
from app.providers.registry import VideoProvider
from app.providers.seedream import DEFAULT_IMAGE_RATIO, IMAGE_RATIOS
from app.services import ark_client
from app.services.paths import asset_dir, video_gen_dir


def _aspect_ratio_for_shot(conn, shot_id: str) -> str:
    """跟 seedream.py 的 _aspect_ratio_for_shot 同一个查法(独立复制一份，两个 provider
    模块不互相依赖对方的私有查询函数，跟 seedream.py 里 _style_mode_for_character/
    _style_mode_for_scene/_style_mode_for_shot 三个近似重复的小函数是同一个取舍)。"""
    row = conn.execute(
        'SELECT p.aspectRatio AS aspectRatio FROM "Shot" sh '
        'JOIN "Scene" sc ON sh.sceneId = sc.id JOIN "Story" s ON sc.storyId = s.id '
        'JOIN "Project" p ON s.projectId = p.id WHERE sh.id = ?',
        (shot_id,),
    ).fetchone()
    return row["aspectRatio"] if row and row["aspectRatio"] else DEFAULT_IMAGE_RATIO


def _ratio_string(aspect_ratio: str) -> str:
    """把我们内部的比例 key(可能是老的 portrait/landscape，也可能是新的 9:16/1:1/4:3)
    转成 Seedance --ratio 参数认的真实"宽:高"字符串(比如 portrait -> 3:4)。"""
    return IMAGE_RATIOS.get(aspect_ratio, IMAGE_RATIOS[DEFAULT_IMAGE_RATIO])["ratio"]


class SeedanceVideoProvider(VideoProvider):
    """图生视频，参数取自 PIPELINE.md 第③步的经验：
    - duration 固定 4 秒（Seedance 最短支持4秒）
    - ratio 默认 9:16，实际取这一镜所在项目的 Project.aspectRatio(见 _aspect_ratio_for_shot)，
      跟分镜出图用同一个项目级比例设置，保证一部剧所有镜头能拼到一起；resolution 固定 720p
    - 模型默认 doubao-seedance-2-0（1.5-pro 已下线）；如果账号在 Ark 控制台开通的是
      具体的推理接入点(ep-xxxxxxxx)而不是裸模型名，去设置页填 arkVideoModel 覆盖默认值
    - 创建任务 + 轮询最长等 20 分钟，服务端偶发不响应是已知问题，重试是唯一办法
    """

    def generate_video(
        self, *, shot_id: str, start_image_path: str, end_image_path: str | None, prompt: str
    ) -> dict:
        with get_connection() as conn:
            settings = get_settings(conn)
            aspect_ratio = _aspect_ratio_for_shot(conn, shot_id)
        api_key = settings.get("arkApiKey")
        model = settings.get("arkVideoModel") or ark_client.DEFAULT_SEEDANCE_MODEL
        base_url = settings.get("arkBaseUrl") or ark_client.ARK_BASE_URL

        task_id, model_used = ark_client.create_video_task(
            api_key=api_key,
            prompt=prompt,
            start_image_path=start_image_path,
            ratio=_ratio_string(aspect_ratio),
            duration=4,
            resolution="720p",
            model=model,
            base_url=base_url,
        )
        video_url = ark_client.poll_video_task(api_key=api_key, task_id=task_id, base_url=base_url)

        dest = asset_dir(shot_id) / "video.mp4"
        ark_client.download_to_file(video_url, str(dest))

        # model_used 可能跟设置页配的 model 不一样（配额打满自动降级过），
        # 存实际用的这个，UI 上显示才准确。
        return {"filePath": str(dest), "providerId": "seedance", "model": model_used}


def generate_video_from_image(
    video_id: str,
    reference_image_path: str,
    prompt: str,
    ratio: str = DEFAULT_IMAGE_RATIO,
    project_id: str | None = None,
) -> dict:
    """无剧本图生视频用：跟 SeedanceVideoProvider.generate_video 是同一个 Ark 接口，
    只是不挂在 shot_id 上——落盘路径改成 video_gen_dir(video_id, project_id)，跟
    posters.py 直接调 generate_poster_background()（不经过 registry/Provider 抽象）
    是同一个模式，这个功能本身就没有"分镜"这个概念，硬套 Provider 接口反而绕。
    duration/resolution 跟分镜生成视频保持一致(4秒/720p)——没有让用户挑这些的必要；
    但比例(ratio)现在开放成每次生成单独选，跟海报/独立文生图的"画幅"是同一套选项
    (VideoGeneration.ratio 字段，见 video_generations.py)，默认值维持原来硬编码的 9:16。
    """
    with get_connection() as conn:
        settings = get_settings(conn)
    api_key = settings.get("arkApiKey")
    model = settings.get("arkVideoModel") or ark_client.DEFAULT_SEEDANCE_MODEL
    base_url = settings.get("arkBaseUrl") or ark_client.ARK_BASE_URL

    task_id, model_used = ark_client.create_video_task(
        api_key=api_key,
        prompt=prompt,
        start_image_path=reference_image_path,
        ratio=_ratio_string(ratio),
        duration=4,
        resolution="720p",
        model=model,
        base_url=base_url,
    )
    video_url = ark_client.poll_video_task(api_key=api_key, task_id=task_id, base_url=base_url)

    dest = video_gen_dir(video_id, project_id) / "video.mp4"
    ark_client.download_to_file(video_url, str(dest))

    return {"filePath": str(dest), "providerId": "seedance", "model": model_used}
