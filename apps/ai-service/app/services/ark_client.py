"""
火山方舟(Ark) HTTP 客户端封装：图片生成 + 视频生成任务。

⚠️ 字段名说明：官方 REST 文档对 images/generations 和
contents/generations/tasks 的公开示例并不完整（截至编写时只找到部分参数
的文档页面），这里的请求体是根据以下信息拼出来的最佳猜测：
  - https://www.volcengine.com/docs/82379/1541523 (Seedream API 参考)
  - https://doubao.apifox.cn/265914813e0 (创建视频生成任务 API)
  - PIPELINE.md 里记录的实际调用经验(模型名、超时、参数传法)
如果实际调用返回 400/InvalidParameter，把 Asset.error 里的原始响应贴出来，
照着报错改这个文件里的请求体构造就行——已经做了"把原始响应体透传到
error 字段"的处理，不会静默失败。

⚠️ Base URL 说明：普通按量付费的 Ark 账号用 /api/v3/，但用户反馈他们的账号是
"Ark Plan"套餐，实际能调用的路径是 /api/plan/v3/，模型名也是点号格式
（doubao-seedance-2.0）而不是带日期后缀的快照 ID（doubao-seedance-2-0-260128）。
两种账号类型都可能存在，所以 base_url/model 都做成参数，默认给标准 /api/v3/，
Plan 账号在设置页填 arkBaseUrl / arkVideoModel / arkImageModel 覆盖。
"""

from __future__ import annotations

import base64
import io
import mimetypes
import time
from pathlib import Path
from typing import Any, Optional

import requests

try:
    from PIL import Image

    _PIL_AVAILABLE = True
except ImportError:  # 理论上 requirements.txt 里已经加了 Pillow，这里只是兜底，
    # 万一某台机器的 venv 没装上，别因为"参考图压缩"这种优化项把整条生成链路搞挂——
    # 退化成原图直传，仍然能用，只是大图可能会慢/容易超时。
    _PIL_AVAILABLE = False

# 确认过了：/api/plan/v3 是官方真实存在的路径，对应"方舟 Agent Plan"套餐
# （文档：docs.volcengine.com/docs/82379/2373740，计费单位叫"Agent 燃料值/AFP"）。
# 之前用这个路径报 401 AK/SK missing/invalid，不是路径错了，是当时设置里存的
# 那把 key 不是 Agent Plan 专属的那把——Agent Plan 需要一把单独发的专属 key，
# 跟"开通管理"里给普通按量付费用的 API Key 不是同一把，两者不能混用。
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"

# 已有用户曾把 beijing.volces 中间的点漏掉。该拼写会连到不存在的 TLS 主机，
# 表现成容易误导人的 SSLEOFError；在客户端边界统一修正，旧的本地设置也能继续使用。
ARK_BASE_URL_TYPOS = {
    "ark.cn-bejing.volces.com": "ark.cn-beijing.volces.com",
    "ark.cn-bejingvolces.com": "ark.cn-beijing.volces.com",
    "ark.cn-beijingwolces.com": "ark.cn-beijing.volces.com",
    "ark.cn-beijingvolces.com": "ark.cn-beijing.volces.com",
}


def normalize_base_url(base_url: str | None) -> str:
    normalized = (base_url or ARK_BASE_URL).strip().rstrip("/")
    for typo, correct in ARK_BASE_URL_TYPOS.items():
        normalized = normalized.replace(typo, correct)
    return normalized

# 用户确认过账号(Large 套餐)配置文件里点名支持的模型，直接用这两个当默认值：
# doubao-seedance-2.0 / doubao-seedance-2.0-fast / doubao-seedance-2.0-mini 三选一，
# 1.5-pro 已经是"即将下线"禁止新项目接入；模型名必须显式指定，不支持 auto。
# 如果换了别的账号这两个默认值不对，去设置页填 arkVideoModel/arkImageModel 覆盖。
DEFAULT_SEEDANCE_MODEL = "doubao-seedance-2.0"
DEFAULT_SEEDREAM_MODEL = "doubao-seedream-5.0-lite"

# PIPELINE.md 踩坑结论：createTask 请求默认60秒超时对带图片的请求偏短，调到300秒。
CREATE_TASK_TIMEOUT_SEC = 300
# 配合 _file_to_data_uri 的参考图压缩一起用：压缩已经把常见的大图砍掉了，这里再放宽到
# 180秒纯粹是兜底，防止极端情况(网络本身很差/超大图片压缩后还是不小)依然写超时。
IMAGE_GEN_TIMEOUT_SEC = 180
POLL_INTERVAL_SEC = 5
POLL_MAX_WAIT_SEC = 20 * 60  # PIPELINE.md: 前台死等最长20分钟

# 失败自动重试参数：只对"可能是临时性"的错误重试，认证/参数类错误重试没有意义，
# 只会让用户多等几十秒看到同一个错误，所以必须先分类再决定要不要重试。
MAX_RETRY_ATTEMPTS = 3  # 含首次尝试，即最多重试 2 次
RETRY_BACKOFF_BASE_SEC = 5  # 指数退避：5s / 10s

# Seedance 配额打满(403 quota)时按顺序尝试的降级模型；只在"配额不足"这一种错误上做，
# 因为 -fast/-mini 大概率消耗的 AFP 更少，换个型号有机会跑通。认证错误(401)、
# 模型未开通(404 ModelNotOpen)这类换模型也没用，不在这个降级逻辑里重试。
SEEDANCE_FALLBACK_MODELS = ["doubao-seedance-2.0-fast", "doubao-seedance-2.0-mini"]


class ArkError(RuntimeError):
    """携带原始响应体 + 错误分类，方便直接展示在 Asset.error 里定位问题，
    也方便调用方判断要不要自动重试/降级。
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.is_quota_error = status_code == 403


def _raise_ark_error(action: str, resp: "requests.Response") -> None:
    """quota 相关的 403 单独提示一下：Large 套餐是 5小时/7天/自然月三层 AFP 限额，
    任意一层打满、或者这一次请求预估消耗超过当前剩余额度，都会是这个错误，
    不代表套餐不支持这个模型。原始响应体还是会带着，方便对着 requestId 去后台查。

    同时判断这个错误算不算"可重试"：
    - 401 (认证错误)、400/404 (参数或模型未开通) —— 重试也是同样的结果，不重试
    - 403 quota / 429 限流 / 5xx 服务端错误 —— 大概率是临时的，值得自动重试
    """
    text = resp.text
    status = resp.status_code
    hint = ""
    is_quota = status == 403 and "quota" in text.lower()
    if is_quota:
        hint = (
            "\n（提示：这是 AFP 配额问题，不是模型没开通——Large 套餐有5小时/7天/自然月三层限额，"
            "任意一层打满，或者这次请求预估消耗超过当前剩余额度都会报这个。"
            "拿响应里的 Request id 去 Ark 控制台查一下三层剩余额度。）"
        )
    retryable = is_quota or status == 429 or status >= 500
    raise ArkError(f"{action}失败 HTTP {status}: {text}{hint}", status_code=status, retryable=retryable)


def _with_retry(action: str, fn):
    """对可重试的 ArkError / 网络异常做指数退避重试，非可重试错误直接抛出不浪费时间。"""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except ArkError as exc:
            last_exc = exc
            if not exc.retryable or attempt == MAX_RETRY_ATTEMPTS:
                raise
        except requests.exceptions.RequestException as exc:
            # 网络层错误(超时/连接重置)本身就是临时性的，也纳入重试
            hint = ""
            if isinstance(exc, requests.exceptions.SSLError):
                hint = "（已尝试绕过系统代理直连且仍失败，请检查 Windows 代理、杀毒软件 HTTPS 扫描或网络防火墙。）"
            last_exc = ArkError(f"{action}网络请求异常: {exc}{hint}", retryable=True)
            if attempt == MAX_RETRY_ATTEMPTS:
                raise last_exc from exc
        delay = RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
        time.sleep(delay)
    if last_exc:
        raise last_exc
    raise ArkError(f"{action}失败：未知错误")


def _headers(api_key: str) -> dict:
    if not api_key:
        raise ArkError("未配置火山方舟 API Key，请先在设置页填写")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _ark_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """先按用户的网络设置访问；TLS 握手被错误代理中断时，再安全地直连一次。

    不会关闭证书验证。仅忽略代理环境变量，避免某些 Windows 代理/安全软件对
    ark.cn-beijing.volces.com 的 HTTPS 检查在 ClientHello 阶段直接断开连接。
    """
    try:
        return requests.request(method, url, **kwargs)
    except requests.exceptions.SSLError as proxy_error:
        with requests.Session() as direct_session:
            direct_session.trust_env = False
            try:
                return direct_session.request(method, url, **kwargs)
            except requests.exceptions.SSLError as direct_error:
                raise direct_error from proxy_error


# 参考图直接塞进 JSON 请求体里传给 Ark(base64)，不是走文件上传接口。用户现在可以用
# 桌面原生选择框挑任意本地文件当参考图，手机相册直出的照片随便就是十几 MB，base64
# 编码还要再涨 1/3 体积——这么大的请求体在稍微差一点的网络下，光是"把请求体写到 socket
# 里"这一步就可能超过 IMAGE_GEN_TIMEOUT_SEC，抛出 "Connection aborted... write operation
# timed out"，还没到服务端处理阶段就已经失败了。而参考图本来就只是给模型一个视觉锚点，
# 不需要原图分辨率——我们自己生成的图最大也就 2048x2048，所以超过这个尺寸/文件明显偏大
# 时统一缩小 + 转成 JPEG 压缩一遍再传，体积能降一个量级；本来就不大的文件(比如我们自己
# 生成的参考图，本来就在合理尺寸内)原样直传，不做没必要的二次压缩。
MAX_REFERENCE_DIMENSION = 2048
MAX_REFERENCE_BYTES_BEFORE_COMPRESS = 3 * 1024 * 1024  # 3MB


def _file_to_data_uri(path: str) -> str:
    p = Path(path)
    raw = p.read_bytes()
    mime = mimetypes.guess_type(p.name)[0] or "image/png"

    if not _PIL_AVAILABLE:
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"

    try:
        with Image.open(io.BytesIO(raw)) as im:
            too_big_pixels = max(im.size) > MAX_REFERENCE_DIMENSION
            too_big_bytes = len(raw) > MAX_REFERENCE_BYTES_BEFORE_COMPRESS
            if not too_big_pixels and not too_big_bytes:
                b64 = base64.b64encode(raw).decode("ascii")
                return f"data:{mime};base64,{b64}"

            if too_big_pixels:
                im.thumbnail((MAX_REFERENCE_DIMENSION, MAX_REFERENCE_DIMENSION), Image.LANCZOS)
            rgb = im.convert("RGB")  # 参考图不需要透明通道，统一转 RGB 才能存 JPEG
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=88)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
    except Exception:  # noqa: BLE001
        # 压缩这步本身出问题(损坏文件/不支持的格式之类)不该拖垮整个生成流程，
        # 退回最原始的"直接传原文件"行为，跟没加这层优化之前一样。
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"


def file_to_data_uri(path: str) -> str:
    """参考图压缩 + base64 data URI 编码，供 minimax_client.py 复用——这个逻辑跟 Ark
    本身无关，纯粹是"本地文件转 data URI 前先压缩一下"的通用优化，不该在两个 provider
    的客户端模块里各写一份。"""
    return _file_to_data_uri(path)


def generate_image(
    *,
    api_key: str,
    prompt: str,
    reference_image_urls: Optional[list[str]] = None,
    size: str = "1440x2560",
    model: str = DEFAULT_SEEDREAM_MODEL,
    base_url: str = ARK_BASE_URL,
) -> str:
    """调用 images/generations，返回生成图片的 URL（火山云存储链接，有效期24小时，调用方需尽快下载落盘）。"""
    base_url = normalize_base_url(base_url)
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": False,
    }
    if reference_image_urls:
        # 单图传字符串、多图传数组，两种官方示例都出现过，这里统一传数组更保险。
        body["image"] = reference_image_urls if len(reference_image_urls) > 1 else reference_image_urls[0]

    def _do_request() -> str:
        resp = _ark_request(
            "POST",
            f"{base_url.rstrip('/')}/images/generations",
            headers=_headers(api_key),
            json=body,
            timeout=IMAGE_GEN_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            _raise_ark_error("Seedream 图片生成", resp)

        data = resp.json()
        try:
            return data["data"][0]["url"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ArkError(f"Seedream 返回结构不符合预期: {data}") from exc

    return _with_retry("Seedream 图片生成", _do_request)


def chat_completion(
    *,
    api_key: str,
    prompt: str,
    model: str,
    system: Optional[str] = None,
    base_url: str = ARK_BASE_URL,
    timeout_sec: int = 60,
) -> str:
    """调用 Ark 的 chat/completions（OpenAI 兼容格式），给"AI优化提示词"这种纯文本
    任务用——跟 images/generations、contents/generations/tasks 是同一个 base_url
    下的另一个端点，账号类型(/api/v3/ 还是 /api/plan/v3/)一致，不需要单独配置。
    这里不重试/不降级：优化提示词是一次性、用户主动点按钮触发的操作，失败了让用户
    自己决定要不要再点一次，不像图片/视频生成那样值得自动重试。
    """
    base_url = normalize_base_url(base_url)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages}

    resp = _ark_request(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        headers=_headers(api_key),
        json=body,
        timeout=timeout_sec,
    )
    if resp.status_code != 200:
        _raise_ark_error("Ark 文本生成", resp)

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ArkError(f"Ark 文本生成返回结构不符合预期: {data}") from exc
    if not isinstance(content, str) or not content.strip():
        raise ArkError(f"Ark 文本生成返回了空内容: {data}")
    return content.strip()


def create_video_task(
    *,
    api_key: str,
    prompt: str,
    start_image_path: str,
    ratio: str = "9:16",
    duration: int = 4,
    resolution: str = "720p",
    model: str = DEFAULT_SEEDANCE_MODEL,
    base_url: str = ARK_BASE_URL,
) -> tuple[str, str]:
    """提交图生视频任务，返回 (task_id, 实际生效的模型)。首帧图用本地文件转 data URI 传给 image_url。

    第二个返回值很重要：配额打满时会自动降级模型，最终成功的模型不一定是传进来的
    `model` 参数——调用方(SeedanceVideoProvider)要把这个"实际用的模型"存进 Asset.model，
    UI 上显示的才是准确的，不然配置页写的是 2.0，实际这条视频其实是 -fast 出的，误导用户。
    """
    base_url = normalize_base_url(base_url)
    prompt_with_flags = f"{prompt} --ratio {ratio} --dur {duration} --rs {resolution}"
    image_data_uri = _file_to_data_uri(start_image_path)

    def _submit(m: str) -> str:
        # generate_audio 显式设成 False：doubao-seedance-2.0 这代模型是"音画联合生成"，
        # 会自动配一段背景音乐/音效上去，不是配音(IndexTTS 单独生成)，也不是我们要的。
        # 之前完全没传这个参数，等于听 Ark 那边的默认值——用户反馈导出视频有莫名其妙的
        # 背景音乐，最可能就是这个默认值在服务端其实是开着的。不管官方文档写的默认是什么，
        # 显式关掉最保险，不用赌它到底是不是真的默认关闭。
        body = {
            "model": m,
            "content": [
                {"type": "text", "text": prompt_with_flags},
                {"type": "image_url", "image_url": {"url": image_data_uri}},
            ],
            "generate_audio": False,
        }
        resp = _ark_request(
            "POST",
            f"{base_url.rstrip('/')}/contents/generations/tasks",
            headers=_headers(api_key),
            json=body,
            timeout=CREATE_TASK_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            _raise_ark_error("Seedance 创建视频任务", resp)

        data = resp.json()
        task_id = data.get("id")
        if not task_id:
            raise ArkError(f"Seedance 任务创建返回里没有 id: {data}")
        return task_id

    # 先在当前模型上重试；如果最终还是配额类错误(403 quota)，按顺序尝试降级模型
    # （-fast/-mini 大概率更省 AFP）。认证错误/模型未开通类错误不会走到这里
    # （_with_retry 里非 retryable 直接抛出），所以不会浪费时间在这些模型上重试。
    candidates = [model] + [m for m in SEEDANCE_FALLBACK_MODELS if m != model]
    last_exc: ArkError | None = None
    for i, candidate in enumerate(candidates):
        try:
            task_id = _with_retry(f"Seedance 创建视频任务(model={candidate})", lambda c=candidate: _submit(c))
            return task_id, candidate
        except ArkError as exc:
            last_exc = exc
            if not exc.is_quota_error or i == len(candidates) - 1:
                raise
            # 还有下一个降级模型可以试，继续循环
    raise last_exc if last_exc else ArkError("Seedance 创建视频任务失败：未知错误")


def poll_video_task(*, api_key: str, task_id: str, base_url: str = ARK_BASE_URL) -> str:
    """轮询任务直到成功/失败，返回视频 URL（有效期24小时）。最长等待 20 分钟。"""
    base_url = normalize_base_url(base_url)
    deadline = time.time() + POLL_MAX_WAIT_SEC
    headers = {"Authorization": f"Bearer {api_key}"}
    consecutive_poll_errors = 0

    while time.time() < deadline:
        try:
            resp = _ark_request(
                "GET",
                f"{base_url.rstrip('/')}/contents/generations/tasks/{task_id}", headers=headers, timeout=30
            )
        except requests.exceptions.RequestException as exc:
            # 轮询请求本身偶发网络抖动很常见，不该直接判整个视频任务失败——
            # 连续失败到一定次数才真正放弃，避免因为一次网络波动前功尽弃。
            consecutive_poll_errors += 1
            if consecutive_poll_errors >= 5:
                raise ArkError(f"查询 Seedance 任务连续失败: {exc}", retryable=False) from exc
            time.sleep(POLL_INTERVAL_SEC)
            continue

        if resp.status_code != 200:
            consecutive_poll_errors += 1
            if consecutive_poll_errors >= 5:
                raise ArkError(f"查询 Seedance 任务失败 HTTP {resp.status_code}: {resp.text}")
            time.sleep(POLL_INTERVAL_SEC)
            continue
        consecutive_poll_errors = 0

        data = resp.json()
        status = data.get("status")

        if status in ("succeeded", "completed"):
            content = data.get("content") or {}
            video_url = content.get("video_url") or data.get("video_url")
            if not video_url:
                raise ArkError(f"任务成功但取不到 video_url: {data}")
            return video_url

        if status in ("failed", "cancelled"):
            raise ArkError(f"Seedance 任务失败: {data.get('error') or data}")

        time.sleep(POLL_INTERVAL_SEC)

    raise ArkError(f"Seedance 任务 {task_id} 等待超过20分钟仍未完成")


def download_to_file(url: str, dest_path: str) -> None:
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=120)
    if resp.status_code != 200:
        raise ArkError(f"下载生成产物失败 HTTP {resp.status_code}: {url}")
    dest.write_bytes(resp.content)
