"""
MiniMax(海螺) H3 视频生成 HTTP 客户端封装，跟 ark_client.py 是同一个层级——
一个纯 HTTP 调用模块，不关心 Provider/Registry 那一层的事情。

接口来自官方 OpenAPI 文档（编写时抓取）：
  - 创建任务: https://platform.minimax.io/docs/api-reference/video-generation-v2-create
  - 查询任务: https://platform.minimax.io/docs/api-reference/video-generation-v2-query
POST https://api.minimax.io/v2/video_generation，Bearer 鉴权，异步任务制
（创建后拿 task_id，轮询查询接口拿结果，跟 Seedance 的"创建任务+轮询"是同一个模式）。

跟 Seedance 的几个关键差异：
  - model 目前公开 API 只支持 "MiniMax-H3" 一个值，没有降级模型链可选，
    所以这里没有 ark_client.py 里 SEEDANCE_FALLBACK_MODELS 那一套逻辑。
  - 图生视频时文档明确写了"只要 content 里有图片，ratio 就必须是 adaptive"，
    所以这里不接受外部传入的 ratio，直接硬编码，不像 Seedance 那样按项目
    aspectRatio 算一个具体的宽高比传过去——这是 MiniMax 这条路径的已知限制。
  - 错误响应是 OpenAI 风格的 {type, error: {type, message, http_code}, request_id}，
    跟 Ark 那种"随便一段文本/JSON"不一样，这里单独写了解析逻辑。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import requests

from app.services.ark_client import file_to_data_uri  # 参考图压缩逻辑是通用的，不重复造

MINIMAX_BASE_URL = "https://api.minimax.io"
DEFAULT_MINIMAX_MODEL = "MiniMax-H3"

CREATE_TASK_TIMEOUT_SEC = 300
POLL_INTERVAL_SEC = 5
POLL_MAX_WAIT_SEC = 20 * 60

MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SEC = 5

# OaiErrorDetail.type 里明确"临时性、值得重试"的几种：限流/欠费额度类波动/服务端错误。
# 认证错误(401)、参数错误(400)、内容审核类(422)重试没有意义。
_RETRYABLE_ERROR_TYPES = {"rate_limit_error", "server_error", "overloaded_error"}


class MiniMaxError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _raise_minimax_error(action: str, resp: "requests.Response") -> None:
    status = resp.status_code
    error_type = None
    message = resp.text
    try:
        data = resp.json()
        detail = data.get("error") or {}
        error_type = detail.get("type")
        message = detail.get("message") or message
    except ValueError:
        pass
    retryable = error_type in _RETRYABLE_ERROR_TYPES if error_type else status == 429 or status >= 500
    raise MiniMaxError(f"{action}失败 HTTP {status}: {message}", status_code=status, retryable=retryable)


def _with_retry(action: str, fn):
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except MiniMaxError as exc:
            last_exc = exc
            if not exc.retryable or attempt == MAX_RETRY_ATTEMPTS:
                raise
        except requests.exceptions.RequestException as exc:
            last_exc = MiniMaxError(f"{action}网络请求异常: {exc}", retryable=True)
            if attempt == MAX_RETRY_ATTEMPTS:
                raise last_exc from exc
        delay = RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
        time.sleep(delay)
    if last_exc:
        raise last_exc
    raise MiniMaxError(f"{action}失败：未知错误")


def _headers(api_key: str) -> dict:
    if not api_key:
        raise MiniMaxError("未配置 MiniMax API Key，请先在设置页填写")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def create_video_task(
    *,
    api_key: str,
    prompt: str,
    start_image_path: str,
    duration: int = 4,
    resolution: str = "768P",
    model: str = DEFAULT_MINIMAX_MODEL,
    base_url: str = MINIMAX_BASE_URL,
) -> str:
    """提交图生视频任务，返回 task_id。首帧图片走本地文件转 data URI（跟 Ark 那边共用
    同一份压缩逻辑）。ratio 固定传 "adaptive"——文档要求只要有图片输入就必须这么传。
    """
    image_data_uri = file_to_data_uri(start_image_path)
    body = {
        "model": model,
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "role": "first_frame", "image_url": {"url": image_data_uri}},
        ],
        "resolution": resolution,
        "duration": duration,
        "ratio": "adaptive",
    }

    def _submit() -> str:
        resp = requests.post(
            f"{base_url.rstrip('/')}/v2/video_generation",
            headers=_headers(api_key),
            json=body,
            timeout=CREATE_TASK_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            _raise_minimax_error("MiniMax 创建视频任务", resp)
        data = resp.json()
        task = data.get("task") or {}
        task_id = task.get("id") or data.get("task_id")
        if not task_id:
            raise MiniMaxError(f"MiniMax 任务创建返回里没有 task id: {data}")
        return task_id

    return _with_retry("MiniMax 创建视频任务", _submit)


def poll_video_task(*, api_key: str, task_id: str, base_url: str = MINIMAX_BASE_URL) -> str:
    """轮询任务直到成功/失败，返回视频 URL（时效性链接，需尽快下载落盘）。最长等待 20 分钟。"""
    deadline = time.time() + POLL_MAX_WAIT_SEC
    headers = {"Authorization": f"Bearer {api_key}"}
    consecutive_poll_errors = 0

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{base_url.rstrip('/')}/v2/query/video_generation/{task_id}", headers=headers, timeout=30
            )
        except requests.exceptions.RequestException as exc:
            consecutive_poll_errors += 1
            if consecutive_poll_errors >= 5:
                raise MiniMaxError(f"查询 MiniMax 任务连续失败: {exc}", retryable=False) from exc
            time.sleep(POLL_INTERVAL_SEC)
            continue

        if resp.status_code != 200:
            consecutive_poll_errors += 1
            if consecutive_poll_errors >= 5:
                raise MiniMaxError(f"查询 MiniMax 任务失败 HTTP {resp.status_code}: {resp.text}")
            time.sleep(POLL_INTERVAL_SEC)
            continue
        consecutive_poll_errors = 0

        data = resp.json()
        task = data.get("task") or {}
        status = task.get("status")

        if status == "succeeded":
            content = task.get("content") or {}
            video_url = content.get("url")
            if not video_url:
                raise MiniMaxError(f"任务成功但取不到视频 url: {data}")
            return video_url

        if status in ("failed", "cancelled"):
            raise MiniMaxError(f"MiniMax 任务失败: {task.get('error') or data}")

        time.sleep(POLL_INTERVAL_SEC)

    raise MiniMaxError(f"MiniMax 任务 {task_id} 等待超过20分钟仍未完成")


def download_to_file(url: str, dest_path: str) -> None:
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=120)
    if resp.status_code != 200:
        raise MiniMaxError(f"下载生成产物失败 HTTP {resp.status_code}: {url}")
    dest.write_bytes(resp.content)
