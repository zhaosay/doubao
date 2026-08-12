"""
把一句话 premise 扩展成分镜脚本，支持两种生成方式(Setting.storyGenProvider)：
- claude_cli(默认)：调用本机终端的 `claude`(Claude Code CLI)。要求本机已经装好
  claude 并且能正常鉴权登录 —— ai-service 只是 subprocess 调用它，不管它用的是
  订阅登录还是 API Key。如果 `claude` 不在 PATH 上，这里会直接抛出清晰的错误，
  写进 Story.status=failed 里。这条路径不用配任何 API Key，但依赖本机终端环境，
  Windows 上常见的翻车点是压根没装/没登录 Claude Code。
- api：直连一个 Anthropic Messages API 兼容的第三方中转/代理服务(填 Base URL +
  API Key + 模型名)，不依赖本机终端，Windows 上更稳，但需要用户自己有这么一个
  服务的访问凭证。
两条路径最终都会拿到一段"应该是 JSON 数组"的文本，走同一份 _parse_scenes 解析逻辑。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

import requests

CLAUDE_TIMEOUT_SEC = 300
# claude CLI / 第三方 API 偶尔会超时/输出被截断/吐出非 JSON 内容（不是脚本内容本身有
# 问题，是这次调用运气不好），这些情况重跑一次往往就好了；跟 ark_client.py 的重试思路
# 一样——只重试"看起来是临时性"的失败，不重试"本机没装 claude"/"API Key 没填"这种
# 重试了也不会变的错误。
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SEC = 5

DEFAULT_STORY_GEN_PROVIDER = "claude_cli"
DEFAULT_STORY_GEN_API_MAX_TOKENS = 4096
ANTHROPIC_API_VERSION = "2023-06-01"
API_TIMEOUT_SEC = 300
# 设置页"测试连通性"按钮用的超时：用户在等页面上的按钮转圈，不是后台任务，
# 不能真的等到 API_TIMEOUT_SEC(300秒)才告诉用户失败——30秒内还没响应，
# 基本可以判定是网络/配置问题，没必要陪它等到 5 分钟。
TEST_TIMEOUT_SEC = 30

# 写剧本的画风提示要跟着项目选的美术风格走，不能不管项目选的是漫画还是真人，都硬编码
# "国漫赛璐璐"——不然哪怕后面生图时用了正确的风格前缀，drawPrompt 文字本身已经带上了
# 二次元用词(比如"赛璐璐渲染")，两边打架，画面容易被文字描述带偏，这也是"选真人风格却
# 生成出漫画感画面"的一个常见根源。跟 seedream.py 的 STYLE_PREFIXES 是同一套风格划分，
# 但这里措辞是给写剧本的 LLM 看的"画风参考"提示，不是直接拼进生图 prompt 的硬前缀，
# 所以单独维护一份，没有强耦合到 seedream.py。
STYLE_HINTS = {
    "comic": "国漫赛璐璐（二次元厚涂动画感）",
    "realistic": "真人实拍写实摄影（不要出现动画/漫画/赛璐璐等二次元描述）",
    "render3d": "3D渲染CG动画（皮克斯/迪士尼3D电影质感，不要出现2D手绘或真人摄影描述）",
    "freeform": "不限定，自由发挥",
}
DEFAULT_STYLE_MODE = "comic"

# no_character：这个故事本来就不需要固定人物角色（风光/氛围/产品向内容），不强行编人物
# 出来凑戏——之前不管故事需不需要角色，claude 都会给每一镜编个角色名塞进 characterName，
# 角色库里平白多出一堆用户没打算要的角色，需要手动处理才能清理。
CONTENT_TYPE_HINTS = {
    "character": "这是一部有人物的短剧，正常按角色驱动来写。",
    "no_character": (
        "这个故事不需要固定的人物角色，画面以场景/氛围/产品/风光为主体。"
        "不要虚构人物角色，characterName 一律留空字符串，drawPrompt 里也不要描写人物。"
    ),
}
DEFAULT_CONTENT_TYPE = "character"

PROMPT_TEMPLATE = """你是资深竖屏短剧分镜师。请把下面这句故事简介，扩展成一个6-10镜头的竖屏短剧分镜脚本，画风参考"{style_hint}"，9:16构图。

故事简介：{premise}

{content_type_hint}

严格要求：
1. 只输出一个 JSON 数组，不要 markdown 代码块（不要```），不要任何解释性文字，第一个字符必须是 [。
2. 数组每个元素是一场戏(scene)：{{"summary": "这场戏一句话概括", "shots": [...]}}
3. 每个 shot 字段：
   - sceneType: 景别，如 远景/中景/近景/特写
   - drawPrompt: 画面静态描述（中文），角色出场时标注"（角色参考：角色名）"
   - motionPrompt: 镜头运动/动态描述，不要重复 drawPrompt 里的静态画面细节
   - dialogue: 台词或旁白，没有就是空字符串
   - durationSec: 数字，2.5~4之间
   - characterName: 本镜头登场角色名，多个用顿号分隔，没有就是空字符串
4. 分镜要有起承转合，不要平铺直叙。
"""


class StoryGenerationError(RuntimeError):
    pass


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_scenes(content: str, source_label: str) -> list[dict]:
    """两条生成路径最后都会走到这一步：拿到一段文本，剥掉可能的 ```json 代码块包装，
    解析成 scenes 数组。source_label 只是用来让报错信息说清楚"是哪条路径的输出解析
    失败"（claude CLI 还是第三方 API），方便用户对着报错去改设置。
    """
    content = _strip_code_fence(content)
    try:
        scenes = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StoryGenerationError(f"{source_label}输出不是合法 JSON: {content[:2000]}") from exc

    if not isinstance(scenes, list) or not scenes:
        raise StoryGenerationError(f"{source_label}输出的不是非空数组: {scenes!r}")

    return scenes


def _call_claude_cli(prompt: str) -> str:
    """跑一次 claude CLI，返回剥掉 --output-format json 外层包装之后的原始文本内容
    （还没解析成 scenes，留给 _parse_scenes 统一处理）。"""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
            stdin=subprocess.DEVNULL,  # 后台线程里跑，没有交互式终端；不传这个 claude 会卡住等 stdin
        )
    except subprocess.TimeoutExpired as exc:
        raise StoryGenerationError(f"claude CLI 超过 {CLAUDE_TIMEOUT_SEC} 秒没有返回") from exc
    except OSError as exc:
        # Windows 上常见：claude 命令不在 PATH 里/根本没装，subprocess.run 直接抛
        # FileNotFoundError（是 OSError 的子类），不是走 returncode != 0 这条路。
        raise StoryGenerationError(f"启动 claude 命令失败（本机可能没装/没加进 PATH）: {exc}") from exc

    if proc.returncode != 0:
        raise StoryGenerationError(f"claude CLI 退出码 {proc.returncode}: {proc.stderr[:2000]}")

    raw = proc.stdout.strip()
    if not raw:
        raise StoryGenerationError(f"claude CLI 没有输出内容，stderr: {proc.stderr[:2000]}")

    # --output-format json 包了一层 {"type":"result","result":"...","subtype":"success",...}
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        outer = None

    if isinstance(outer, dict) and "result" in outer:
        if outer.get("subtype") and outer.get("subtype") != "success":
            raise StoryGenerationError(f"claude CLI 返回失败: {outer}")
        content = outer["result"]
    elif isinstance(outer, list):
        content = raw
    else:
        content = raw

    return content if isinstance(content, str) else json.dumps(content)


def call_anthropic_api(
    prompt: str,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    timeout_sec: int = API_TIMEOUT_SEC,
) -> str:
    """调第三方 Anthropic Messages API 兼容服务，返回文本内容（还没解析成 scenes）。
    base_url 约定是"到 /v1/messages 之前"那一段（比如 https://your-proxy.com/api），
    这里统一拼上 /v1/messages——不同中转服务这段前缀不一样，做成整段可配置，
    跟 ark_client.py 里 arkBaseUrl 的处理思路一样。这个函数没有 `_` 前缀，因为
    settings.py 的"测试连通性"接口也要复用同一份调用逻辑，不想在两个地方各写一份。
    """
    url = f"{base_url.rstrip('/')}/v1/messages"
    try:
        resp = requests.post(
            url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout_sec,
        )
    except requests.RequestException as exc:
        raise StoryGenerationError(f"调用第三方 API 失败（网络/连接问题，检查 Base URL 填得对不对）: {exc}") from exc

    if resp.status_code != 200:
        raise StoryGenerationError(f"第三方 API 返回 {resp.status_code}: {resp.text[:2000]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise StoryGenerationError(f"第三方 API 返回的不是合法 JSON: {resp.text[:2000]}") from exc

    blocks = data.get("content") if isinstance(data, dict) else None
    if not isinstance(blocks, list) or not blocks:
        raise StoryGenerationError(f"第三方 API 返回格式不对(缺少 content 字段，不是 Anthropic Messages API 格式?): {data!r}")

    text_parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    content = "".join(text_parts).strip()
    if not content:
        raise StoryGenerationError(f"第三方 API 没有返回文本内容: {data!r}")
    return content


def _attempt_generate(prompt: str, provider_config: dict) -> list[dict]:
    """跑一次生成 + 解析，失败就抛 StoryGenerationError，不在这里做重试判断。
    provider_config 形状: {"provider": "claude_cli"|"api", "baseUrl", "apiKey",
    "model", "maxTokens"}，后 4 个只有 provider="api" 时才会用到。
    """
    provider = provider_config.get("provider", DEFAULT_STORY_GEN_PROVIDER)
    if provider == "api":
        content = call_anthropic_api(
            prompt,
            base_url=provider_config["baseUrl"],
            api_key=provider_config["apiKey"],
            model=provider_config["model"],
            max_tokens=provider_config.get("maxTokens") or DEFAULT_STORY_GEN_API_MAX_TOKENS,
        )
        return _parse_scenes(content, "第三方 API")

    content = _call_claude_cli(prompt)
    return _parse_scenes(content, "claude CLI")


def _strip_characters(scenes: list[dict]) -> list[dict]:
    """content_type=no_character 时的兜底：PROMPT_TEMPLATE 里已经明确告诉 claude
    "不要虚构人物角色/characterName 一律留空"，但这只是喂给 LLM 的一句话提示，不是
    硬约束——claude 有时候还是会自己编个角色名出来(premise 里带"两个人"这种字眼时
    尤其容易)。用户选了"无固定角色"，预期应该是角色库真的是空的，不是"大概率是空的"，
    所以这里在写库之前强制把每一镜的 characterName 清空，不依赖 LLM 是否真的听话。
    """
    for scene in scenes:
        for shot in scene.get("shots", []):
            if shot.get("characterName"):
                shot["characterName"] = ""
    return scenes


def generate_story_scenes(
    premise: str,
    style_mode: str = DEFAULT_STYLE_MODE,
    content_type: str = DEFAULT_CONTENT_TYPE,
    custom_style_hints: dict | None = None,
    custom_content_type_hints: dict | None = None,
    provider_config: dict | None = None,
) -> list[dict]:
    """custom_style_hints / custom_content_type_hints 是设置页里存的自定义提示词
    (Setting.customStyleHints / customContentTypeHints)，按 style_mode / content_type
    的 key 覆盖对应的默认提示语；没有自定义或者这个 key 没填就照样退回 STYLE_HINTS /
    CONTENT_TYPE_HINTS 里写死的默认值。

    provider_config 形状: {"provider": "claude_cli"|"api", "baseUrl", "apiKey",
    "model", "maxTokens"}，缺省(None)时按 claude_cli 走，跟改造之前行为一致。
    """
    provider_config = provider_config or {"provider": DEFAULT_STORY_GEN_PROVIDER}
    provider = provider_config.get("provider", DEFAULT_STORY_GEN_PROVIDER)

    if provider == "api":
        missing = [
            field
            for field in ("baseUrl", "apiKey", "model")
            if not provider_config.get(field)
        ]
        if missing:
            raise StoryGenerationError(
                f"第三方 API 方式缺少配置项：{'、'.join(missing)}，请先在设置页填完整。"
            )
    elif not shutil.which("claude"):
        raise StoryGenerationError(
            "本机没有找到 claude 命令（Claude Code CLI），请先安装并登录，"
            "或者在设置页切换成第三方 API 方式。"
        )

    if custom_style_hints and custom_style_hints.get(style_mode):
        style_hint = custom_style_hints[style_mode]
    else:
        style_hint = STYLE_HINTS.get(style_mode, STYLE_HINTS[DEFAULT_STYLE_MODE])

    if custom_content_type_hints and custom_content_type_hints.get(content_type):
        content_type_hint = custom_content_type_hints[content_type]
    else:
        content_type_hint = CONTENT_TYPE_HINTS.get(content_type, CONTENT_TYPE_HINTS[DEFAULT_CONTENT_TYPE])
    prompt = PROMPT_TEMPLATE.format(premise=premise, style_hint=style_hint, content_type_hint=content_type_hint)

    last_exc: StoryGenerationError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            scenes = _attempt_generate(prompt, provider_config)
            if content_type == "no_character":
                scenes = _strip_characters(scenes)
            return scenes
        except StoryGenerationError as exc:
            last_exc = exc
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_BACKOFF_SEC)
    raise last_exc  # 理论上到不了这里，纯粹让类型检查满意


# 下面两个 test_* 函数专门给设置页的"测试连通性"按钮用：跟 generate_story_scenes
# 不一样，这里不追求生成一份真的分镜脚本，只发一句极短的测试语句，快速确认"这条路径
# 到底通不通"，用 (ok, message) 而不是抛异常，方便路由层直接透传给前端展示，
# 不用套一层 try/except 转换。
_TEST_PROMPT = "请只回复两个字：正常"


def test_claude_cli(timeout_sec: int = TEST_TIMEOUT_SEC) -> tuple[bool, str]:
    """测本机 claude CLI 是否能正常调用。用短超时+一句极简的测试 prompt，
    不走 generate_story_scenes 那套"必须解析出 JSON 数组"的逻辑——测试连通性
    不需要真的生成分镜，claude 随便回句话就算通。
    """
    if not shutil.which("claude"):
        return False, "本机没有找到 claude 命令（Claude Code CLI），请先安装并确认已加入 PATH"

    try:
        proc = subprocess.run(
            ["claude", "-p", _TEST_PROMPT, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, f"claude CLI 超过 {timeout_sec} 秒没有返回，可能是没登录/网络问题，也可能只是这次比较慢"
    except OSError as exc:
        return False, f"启动 claude 命令失败（本机可能没装/没加进 PATH）: {exc}"

    if proc.returncode != 0:
        return False, f"claude CLI 退出码 {proc.returncode}: {(proc.stderr or '').strip()[:500]}"

    raw = proc.stdout.strip()
    if not raw:
        return False, f"claude CLI 没有输出内容，stderr: {(proc.stderr or '').strip()[:500]}"

    try:
        outer = json.loads(raw)
        reply = outer.get("result") if isinstance(outer, dict) else raw
    except json.JSONDecodeError:
        reply = raw

    return True, f"调用成功，claude 回复：{str(reply).strip()[:200]}"


def test_anthropic_api(
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int | None = None,
    timeout_sec: int = TEST_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """测第三方 Anthropic Messages API 兼容服务是否配置正确、能不能连通。
    复用 call_anthropic_api 本体，只是换成短超时 + 极简测试 prompt，出错时把
    StoryGenerationError 的信息转成 (False, message) 而不是抛出去，因为这个
    函数是给"测试连通性"按钮用的，路由层想要的是一个能直接展示的结果，不是异常。
    """
    if not base_url or not base_url.strip():
        return False, "Base URL 没填"
    if not api_key or not api_key.strip():
        return False, "API Key 没填"
    if not model or not model.strip():
        return False, "模型名没填"

    try:
        reply = call_anthropic_api(
            _TEST_PROMPT,
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens or DEFAULT_STORY_GEN_API_MAX_TOKENS,
            timeout_sec=timeout_sec,
        )
    except StoryGenerationError as exc:
        return False, str(exc)

    return True, f"调用成功，模型回复：{reply.strip()[:200]}"
