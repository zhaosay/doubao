from app.db import get_connection, get_settings
from app.providers.registry import VideoProvider
from app.services import ark_client
from app.services.paths import asset_dir, video_gen_dir


class SeedanceVideoProvider(VideoProvider):
    """图生视频，参数取自 PIPELINE.md 第③步的经验：
    - duration 固定 4 秒（Seedance 最短支持4秒）
    - ratio 固定 9:16，resolution 固定 720p
    - 模型默认 doubao-seedance-2-0（1.5-pro 已下线）；如果账号在 Ark 控制台开通的是
      具体的推理接入点(ep-xxxxxxxx)而不是裸模型名，去设置页填 arkVideoModel 覆盖默认值
    - 创建任务 + 轮询最长等 20 分钟，服务端偶发不响应是已知问题，重试是唯一办法
    """

    def generate_video(
        self, *, shot_id: str, start_image_path: str, end_image_path: str | None, prompt: str
    ) -> dict:
        with get_connection() as conn:
            settings = get_settings(conn)
        api_key = settings.get("arkApiKey")
        model = settings.get("arkVideoModel") or ark_client.DEFAULT_SEEDANCE_MODEL
        base_url = settings.get("arkBaseUrl") or ark_client.ARK_BASE_URL

        task_id, model_used = ark_client.create_video_task(
            api_key=api_key,
            prompt=prompt,
            start_image_path=start_image_path,
            ratio="9:16",
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
    video_id: str, reference_image_path: str, prompt: str, project_id: str | None = None
) -> dict:
    """无剧本图生视频用：跟 SeedanceVideoProvider.generate_video 是同一个 Ark 接口，
    只是不挂在 shot_id 上——落盘路径改成 video_gen_dir(video_id, project_id)，跟
    posters.py 直接调 generate_poster_background()（不经过 registry/Provider 抽象）
    是同一个模式，这个功能本身就没有"分镜"这个概念，硬套 Provider 接口反而绕。
    参数跟分镜生成视频保持一致(9:16/4秒/720p)——没有让用户挑这些的必要，
    多一层选择只会增加决策成本，先把"能用"做出来。
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
        ratio="9:16",
        duration=4,
        resolution="720p",
        model=model,
        base_url=base_url,
    )
    video_url = ark_client.poll_video_task(api_key=api_key, task_id=task_id, base_url=base_url)

    dest = video_gen_dir(video_id, project_id) / "video.mp4"
    ark_client.download_to_file(video_url, str(dest))

    return {"filePath": str(dest), "providerId": "seedance", "model": model_used}
