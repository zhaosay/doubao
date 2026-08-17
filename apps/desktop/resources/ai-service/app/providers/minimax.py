from app.db import get_connection, get_settings
from app.providers.registry import VideoProvider
from app.services import minimax_client
from app.services.paths import asset_dir, video_gen_dir


class MiniMaxVideoProvider(VideoProvider):
    """图生视频的另一个可选 provider，跟 SeedanceVideoProvider 是同一层——用户在设置页
    选了 videoProvider=minimax 之后，分镜视频生成走这里而不是 Ark/Seedance。

    跟 Seedance 的已知差异（都是 MiniMax 公开 API 本身的限制，不是这边偷懒）：
    - 没有配额自动降级模型链，公开 API 目前只支持 "MiniMax-H3" 一个模型值。
    - 不支持像 Seedance 那样按项目 aspectRatio 精确指定宽高比——只要 content 里带了
      图片，官方就要求 ratio 必须是 "adaptive"，所以这条路径下画面比例由模型自己按
      输入图决定，没法跟 Seedream 出的图强制对齐到项目设置的比例。
    - duration 固定 4 秒，跟 Seedance 保持一致，方便两条路径生成的素材时长可预期。
    """

    def generate_video(
        self, *, shot_id: str, start_image_path: str, end_image_path: str | None, prompt: str
    ) -> dict:
        with get_connection() as conn:
            settings = get_settings(conn)
        api_key = settings.get("minimaxApiKey")

        task_id = minimax_client.create_video_task(
            api_key=api_key,
            prompt=prompt,
            start_image_path=start_image_path,
            duration=4,
            resolution="768P",
        )
        video_url = minimax_client.poll_video_task(api_key=api_key, task_id=task_id)

        dest = asset_dir(shot_id) / "video.mp4"
        minimax_client.download_to_file(video_url, str(dest))

        return {"filePath": str(dest), "providerId": "minimax", "model": minimax_client.DEFAULT_MINIMAX_MODEL}


def generate_video_from_image(
    video_id: str,
    reference_image_path: str,
    prompt: str,
    ratio: str = "9:16",
    project_id: str | None = None,
) -> dict:
    """无剧本图生视频用的 MiniMax 版本，跟 seedance.py 里同名函数是同一个模式——不挂在
    shot_id 上，直接落盘到 video_gen_dir。ratio 参数保留只是为了跟 Seedance 版本签名一致
    （video_generations.py 按 videoProvider 设置在两者之间派发），实际不会传给 MiniMax
    （见 minimax_client.create_video_task 的说明：有图片输入时 ratio 恒为 adaptive）。
    """
    with get_connection() as conn:
        settings = get_settings(conn)
    api_key = settings.get("minimaxApiKey")

    task_id = minimax_client.create_video_task(
        api_key=api_key,
        prompt=prompt,
        start_image_path=reference_image_path,
        duration=4,
        resolution="768P",
    )
    video_url = minimax_client.poll_video_task(api_key=api_key, task_id=task_id)

    dest = video_gen_dir(video_id, project_id) / "video.mp4"
    minimax_client.download_to_file(video_url, str(dest))

    return {"filePath": str(dest), "providerId": "minimax", "model": minimax_client.DEFAULT_MINIMAX_MODEL}
