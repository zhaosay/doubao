"""
调用本机终端的 `claude`(Claude Code CLI) 把一句话 premise 扩展成分镜脚本。

要求本机已经装好 claude 并且能正常鉴权登录 —— ai-service 只是 subprocess
调用它，不管它用的是订阅登录还是 API Key。如果 `claude` 不在 PATH 上，
这里会直接抛出清晰的错误，写进 Story.status=failed 里。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

CLAUDE_TIMEOUT_SEC = 300
# claude CLI 偶尔会超时/输出被截断/吐出非 JSON 内容（不是脚本内容本身有问题，是这次调用
# 运气不好），这些情况重跑一次往往就好了；跟 ark_client.py 的重试思路一样——只重试
# "看起来是临时性"的失败，不重试"本机没装 claude"这种重试了也不会变的错误。
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SEC = 5

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


def _attempt_generate(prompt: str) -> list[dict]:
    """跑一次 claude CLI + 解析，失败就抛 StoryGenerationError，不在这里做重试判断。"""
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

    content = _strip_code_fence(content if isinstance(content, str) else json.dumps(content))

    try:
        scenes = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StoryGenerationError(f"claude CLI 输出不是合法 JSON: {content[:2000]}") from exc

    if not isinstance(scenes, list) or not scenes:
        raise StoryGenerationError(f"claude CLI 输出的不是非空数组: {scenes!r}")

    return scenes


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
) -> list[dict]:
    """custom_style_hints / custom_content_type_hints 是设置页里存的自定义提示词
    (Setting.customStyleHints / customContentTypeHints)，按 style_mode / content_type
    的 key 覆盖对应的默认提示语；没有自定义或者这个 key 没填就照样退回 STYLE_HINTS /
    CONTENT_TYPE_HINTS 里写死的默认值。
    """
    if not shutil.which("claude"):
        raise StoryGenerationError(
            "本机没有找到 claude 命令（Claude Code CLI），请先安装并登录，"
            "或者改用其他方式填写剧本。"
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
            scenes = _attempt_generate(prompt)
            if content_type == "no_character":
                scenes = _strip_characters(scenes)
            return scenes
        except StoryGenerationError as exc:
            last_exc = exc
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_BACKOFF_SEC)
    raise last_exc  # 理论上到不了这里，纯粹让类型检查满意
