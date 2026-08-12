"""
局域网 IndexTTS 配音 provider。

之前这里写的是猜的 POST /tts JSON 接口，实测直接翻车
（ConnectionResetError / 120s read timeout）——原因是 IndexTTS 官方
(github.com/index-tts/index-tts) 根本没有独立 REST API，就是一个纯
Gradio demo(webui.py)，7860 端口跑的是 Gradio 自己的 UI + 队列协议，不是
一个能直接 POST JSON 过去的普通接口。正确姿势是用 gradio_client 库，它懂
怎么跟 Gradio 的 session/队列协议打交道。

版本差异：IndexTTS2 的 webui.py 里 gen_single 函数有24个位置参数(带情绪
控制)；更早的 IndexTTS1 参数少很多，大概率只有(参考音频, 文本)两个。这里
先按 IndexTTS2 的参数表尝试，不行就退化成最简单的两参数调用。如果部署的是
介于两者之间的版本、两种都报错，错误信息会原样透传到 Asset.error——那时候
在能连到局域网的机器上跑一下：
    python3 -c "from gradio_client import Client; Client('http://<地址>:7860/').view_api()"
看真实参数列表，照着改这个文件。

另外：IndexTTS 是纯 zero-shot 音色克隆，没有"选音色"这种下拉框/音色库，
所以 reference_audio_path 是必填的；voice_id / speed 这两个参数它用不上，
传了也会被忽略。
"""

from __future__ import annotations

import shutil

from app.db import get_connection, get_settings
from app.providers.registry import VoiceProvider
from app.services.paths import asset_dir

GEN_TIMEOUT_SEC = 180


class IndexTTSVoiceProvider(VoiceProvider):
    def generate_voice(
        self,
        *,
        shot_id: str,
        text: str,
        reference_audio_path: str | None = None,
        voice_id: str | None = None,  # noqa: ARG002 - IndexTTS 没有音色库，这个参数用不上
        speed: float = 1.0,  # noqa: ARG002 - IndexTTS 官方 demo 没暴露语速控制
    ) -> dict:
        if not reference_audio_path:
            raise RuntimeError(
                "IndexTTS 是纯 zero-shot 音色克隆，必须在「参考音频路径」里填一个本地 wav 文件，"
                "它没有可选的音色库"
            )

        with get_connection() as conn:
            base_url = get_settings(conn).get("indexTtsBaseUrl")
        if not base_url:
            raise RuntimeError("未配置 IndexTTS 服务地址，请先在设置页填写")

        try:
            from gradio_client import Client, file
        except ImportError as exc:
            raise RuntimeError(
                "缺少 gradio_client 依赖：在 apps/ai-service 的 venv 里重新跑一遍 "
                "`pip install -r requirements.txt`（已经加进去了，重启一下 start.sh 就会自动装）"
            ) from exc

        client = Client(base_url.rstrip("/"), httpx_kwargs={"timeout": GEN_TIMEOUT_SEC}, verbose=False)

        attempts_error: list[str] = []
        result = None

        # 尝试1：IndexTTS2 webui.py 的 gen_single 全量参数（带情绪控制，24个位置参数）
        try:
            result = client.predict(
                0,  # emo_control_method: 0 = 情绪跟音色参考走
                file(reference_audio_path),
                text,
                None,
                0.65,
                0, 0, 0, 0, 0, 0, 0, 0,  # 情绪向量，method=0 时不生效
                "",
                False,
                120,
                True, 0.8, 30, 0.8,
                0.0, 3, 10.0, 1500,
                api_name="/gen_single",
            )
        except Exception as exc:  # noqa: BLE001
            attempts_error.append(f"按 IndexTTS2 参数表调用失败: {exc}")

        # 尝试2：更早/更简单版本的 webui，大概率只有(参考音频, 文本)两个参数
        if result is None:
            try:
                result = client.predict(file(reference_audio_path), text, api_name="/gen_single")
            except Exception as exc:  # noqa: BLE001
                attempts_error.append(f"按简化参数表调用失败: {exc}")

        if result is None:
            raise RuntimeError(
                "调用 IndexTTS 失败，两种参数表都对不上。在能访问局域网的机器上跑 "
                f"python3 -c \"from gradio_client import Client; "
                f"Client('{base_url}').view_api()\" 看真实参数列表再改代码。"
                f"具体报错：{' | '.join(attempts_error)}"
            )

        # gen_single 只有一个 gr.Audio(type='filepath') 输出，predict 直接返回那个本地路径；
        # 如果 Blocks 里输出更多，result 会是个 tuple，这里兜底取第一个。
        result_path = result[0] if isinstance(result, (list, tuple)) else result

        dest = asset_dir(shot_id) / "voice.wav"
        shutil.copy(result_path, dest)

        # IndexTTS 没有多模型/多音色可选（纯 zero-shot 克隆），这里没有真正意义上的
        # "model" 概念，用服务地址标注一下调的是哪台机器，UI 上跟 Ark 那些统一按
        # provider+model 的样式展示，不用再单独判断这个字段有没有意义。
        return {"filePath": str(dest), "providerId": "indextts", "model": base_url}
