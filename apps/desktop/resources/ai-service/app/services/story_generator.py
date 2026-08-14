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
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests

# Windows 上 claude 是 npm 装的 .cmd shim（不是原生 PE 可执行文件），subprocess.run
# 不带 shell=True 直接传 [command, ...] 列表调用会走 CreateProcess 硬调用那个文件，
# 拿到 WinError 193 "%1 不是有效的 Win32 应用程序"——.cmd/.bat 必须交给 cmd.exe 解释
# 执行，不能像 macOS/Linux 上的原生可执行文件/带 shebang 脚本那样直接跑。这三处调用
# 都要带上这个开关：mac 上开发/测试时用的是真身可执行文件，没暴露过这个问题。
_IS_WINDOWS = platform.system() == "Windows"

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


def _npm_global_prefix() -> Path | None:
    """查一下 npm 自己配置的全局安装目录(`npm config get prefix`)，而不是死认
    %APPDATA%\\npm 这一个位置——用户可能用 nvm/volta 切换过 Node 版本，或者手动
    改过 `npm config set prefix`，claude-code 实际装的目录跟默认位置对不上，
    是"装到别的目录"最常见的成因之一。查不到就返回 None，不阻塞后面兜底的固定路径。
    """
    npm_cmd = "npm.cmd" if platform.system() == "Windows" else "npm"
    npm_path = shutil.which(npm_cmd)
    if not npm_path:
        return None
    try:
        result = subprocess.run(
            [npm_path, "config", "get", "prefix"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=_IS_WINDOWS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    prefix = result.stdout.strip()
    return Path(prefix) if prefix else None


# .cmd/.bat/.ps1 shim 脚本自己只是个跳板，真正的可执行文件是脚本内部拼出来的一段路径
# (比如 `"%~dp0\node_modules\@anthropic-ai\claude-code\bin\claude.exe" %*`)。
# npm 全局目录搬过家、用 nvm 切过 Node 版本、或者重装到了别的地方之后，PATH 里常常
# 留着一个"物理文件还在，但内部引用的真身已经被移走/删掉"的僵尸 shim——shutil.which
# 和候选目录扫描都只检查 shim 文件本身存不存在，检查不出这种情况，直接拿去跑只会得到
# Windows cmd.exe 那句不知所云的"不是内部或外部命令"，比"压根没装"更让人摸不着头脑。
_CLAUDE_SHIM_TARGET_RE = re.compile(r'"([^"]+\.(?:exe|js|cjs|mjs))"')


def _shim_target(path: str) -> str | None:
    """读一下 shim 脚本内容，抠出它内部引用的真正可执行文件路径。传进来的不是
    .cmd/.bat/.ps1(比如已经是真身 .exe，或者是 PATH 上的 POSIX shell 脚本)，
    或者读不出引用路径，统一返回 None——意味着"没法验证，姑且相信它"，不阻塞。
    npm 生成的 shim 里这段路径通常是用 `%~dp0`(批处理变量，代表"这个 .cmd 文件自己
    所在的目录")拼出来的相对写法，不展开这个变量的话，就算真身文件确实还在，
    也会被误判成"路径不存在"——所以这里手动把 %~dp0 换成 shim 自己的父目录。
    """
    if not path.lower().endswith((".cmd", ".bat", ".ps1")):
        return None
    try:
        content = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _CLAUDE_SHIM_TARGET_RE.search(content)
    if not match:
        return None
    target = match.group(1)
    shim_dir = str(Path(path).resolve().parent)
    # 传函数当 repl，而不是直接传 shim_dir 字符串：re.sub 会把字符串形式的 repl 当模板解析，
    # 反斜杠+字母会被当成转义序列(比如 Windows 上几乎所有用户目录都是 C:\Users\...，
    # 这里的 \U 会被解析成非法转义)，导致 re.error: bad escape \U——传函数则原样使用返回值，
    # 不会做任何转义解析，能正确处理任意 Windows 路径。
    target = re.sub(r"%~dp0", lambda _m: shim_dir + os.sep, target, flags=re.IGNORECASE)
    return target


def _is_usable(path: str) -> bool:
    """候选路径本身要先存在；如果它是个 shim，还要再确认它内部引用的真身也存在，
    不然就是前面说的"僵尸 shim"。"""
    if not Path(path).exists():
        return False
    target = _shim_target(path)
    return Path(target).exists() if target else True


def _find_claude_candidates() -> list[str]:
    """收集所有"看起来像 claude 命令"的候选路径，不做可用性验证——
    _claude_command() 会在这份列表上再筛一遍 _is_usable。"""
    candidates: list[str] = []
    for name in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    if platform.system() == "Windows":
        candidate_dirs = [
            _npm_global_prefix(),
            Path(os.environ.get("APPDATA", "")) / "npm",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Volta" / "bin",
            Path(os.environ.get("PROGRAMFILES", "")) / "nodejs",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "nodejs",
            Path(os.environ.get("USERPROFILE", "")) / "scoop" / "shims",
        ]
        for directory in candidate_dirs:
            if not directory:
                continue
            for name in ("claude.cmd", "claude.exe", "claude"):
                candidate = directory / name
                if candidate.exists():
                    candidates.append(str(candidate))

    # 去重但保持顺序：shutil.which 找到的排最前面，优先级最高。
    seen: set[str] = set()
    deduped = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _claude_command(cli_path_override: str | None = None) -> str | None:
    """Find Claude Code CLI in GUI-launched packaged apps as well as normal shells.

    cli_path_override 是设置页里"Claude CLI 路径"手动填的值(Setting.storyGenCliPath)——
    自动检测在有些机器上确实猜不准(装到了非标准目录、公司电脑改过 npm 全局前缀等)，
    与其一直加候选目录猜下去，不如让用户直接把路径贴上来，明确优先于自动检测。
    填了就只验证这一个路径可用(见 _is_usable，同样会识别僵尸 shim)，不再去扫别的候选；
    没填(None/空字符串)才走原来的自动检测逻辑。
    """
    if cli_path_override and cli_path_override.strip():
        candidate = cli_path_override.strip()
        return candidate if _is_usable(candidate) else None

    for candidate in _find_claude_candidates():
        if _is_usable(candidate):
            return candidate
    return None


def missing_claude_message(cli_path_override: str | None = None) -> str:
    if cli_path_override and cli_path_override.strip():
        candidate = cli_path_override.strip()
        if not Path(candidate).exists():
            return (
                f"设置页里填的 Claude CLI 路径不存在：{candidate}。检查一下路径有没有打错"
                "（可以在文件管理器里打开确认），或者留空交给自动检测。"
            )
        target = _shim_target(candidate)
        return (
            f"设置页里填的 Claude CLI 路径（{candidate}）指向的实际程序已经不存在"
            f"（这是个跳板脚本，内部引用的真身路径是 {target}，但这个文件不在了）。"
            if target
            else f"设置页里填的路径（{candidate}）看起来不是一个能正常运行的可执行文件。"
        )
    # 找到过候选(比如 PATH 上有 claude.cmd)，但验证下来都是僵尸 shim(内部引用的
    # claude.exe/claude.js 已经不存在)——大概率是重装到了别的目录、或者用 nvm/volta
    # 切换过 Node 版本，留下了旧的 shim 残留，这跟"压根没装"是两个问题，分开提示。
    broken = _find_claude_candidates()
    if broken:
        broken_list = "、".join(broken[:3])
        return (
            f"找到了 claude 命令（{broken_list}），但它指向的实际程序已经不存在——"
            "大概率是 Claude Code 重装到了别的目录、或者切换过 Node 版本后留下的旧文件。"
            "建议删掉这个文件重新安装 Claude Code（npm install -g "
            "@anthropic-ai/claude-code），或者去设置页手动填一下 Claude CLI 的真实路径。"
            "也可以切换成第三方 API 方式。"
        )
    return (
        "本机没有找到 claude 命令（Claude Code CLI）。如果终端里能运行 claude，"
        "请重启本应用；Windows 用户还要确认 Claude Code 的安装目录已加入系统 PATH，"
        "或者去设置页手动填一下 Claude CLI 的真实路径。也可以切换成第三方 API 方式。"
    )


def _format_cli_failure(returncode: int, stdout: str, stderr: str, cli_path_override: str | None) -> str:
    """claude CLI 非 0 退出码时拼报错信息。之前这里只看 stderr——但不少版本的 claude
    CLI(尤其是"直接调用手动填的 .exe 路径，跳过了 npm 装的 .cmd shim"这种场景，shim
    脚本里可能做了一些环境变量/配置路径的准备工作，直接调 .exe 会跳过这些)会把真正的
    报错信息(登录过期/网络问题/配置缺失)打到 stdout 而不是 stderr，或者两边都是空的
    （常见于"这个可执行文件根本不是 claude CLI 本体，或者被杀毒软件/权限拦截，一启动
    就直接退出"）。用户反馈"退出码1"后面什么都没有，就是卡在这个信息丢失上，这里
    把 stdout 也纳入进来，尽量不让用户两眼一抹黑。
    """
    stderr_text = stderr.strip()
    stdout_text = stdout.strip()
    detail = stderr_text
    if not detail and stdout_text:
        try:
            parsed = json.loads(stdout_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            detail = str(parsed.get("result") or parsed.get("error") or parsed)
        else:
            detail = stdout_text
    if not detail:
        override_hint = (
            f"当前用的路径是设置页手动填的「{cli_path_override}」，"
            if cli_path_override
            else "当前是自动检测到的路径，"
        )
        detail = (
            "stdout/stderr 都是空的，claude 进程启动后立刻退出，没有留下任何报错信息。"
            f"{override_hint}"
            "常见原因：①这个可执行文件其实不是真正的 claude CLI 本体（比如直接填了 npm 装的 "
            ".cmd/.bat shim 内部指向的那个真身文件，但那个真身其实还需要 shim 脚本准备的环境变量/"
            "参数，直接单独调用反而跑不起来）——建议改填 shim 文件本身的路径（比如 claude.cmd），"
            "而不是它内部指向的 .exe；②杀毒软件/系统权限拦截了这个进程；③在自己的终端里手动运行"
            "一遍同样的命令（把这里的路径原样粘贴到 PowerShell/CMD 里加上 --version 试一下），"
            "看终端里到底提示什么，比这里的报错更直接。"
        )
    return f"claude CLI 退出码 {returncode}: {detail[:2000]}"


def _call_claude_cli(prompt: str, cli_path_override: str | None = None) -> str:
    """跑一次 claude CLI，返回剥掉 --output-format json 外层包装之后的原始文本内容
    （还没解析成 scenes，留给 _parse_scenes 统一处理）。"""
    command = _claude_command(cli_path_override)
    if not command:
        raise StoryGenerationError(missing_claude_message(cli_path_override))

    try:
        proc = subprocess.run(
            # prompt 通过 stdin 传，不再拼进命令行参数——claude -p 不带参数时就是标准
            # Unix filter 用法(echo "..." | claude -p)，官方文档写明的用法。之前把 prompt
            # 直接塞进 argv，在 Windows 上 .cmd 只能通过 cmd.exe 转发执行，中文/长文本
            # 提示词过这一层命令行转发时可能因为系统代码页问题被截断/乱码，claude 收到
            # 损坏的参数后直接退出且不留任何报错(用户实测复现：手动在终端跑通，但走
            # 我们这条调用链却"退出码1+stdout/stderr全空")。走 stdin 管道传输是二进制
            # 精确的，不经过命令行解析这一层，从根上绕开这类编码问题，长 prompt 也不用
            # 担心 Windows 命令行长度上限(约 8191 字符)。
            [command, "-p", "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CLAUDE_TIMEOUT_SEC,
            shell=_IS_WINDOWS,
        )
    except subprocess.TimeoutExpired as exc:
        raise StoryGenerationError(f"claude CLI 超过 {CLAUDE_TIMEOUT_SEC} 秒没有返回") from exc
    except OSError as exc:
        # Windows 上常见：claude 命令不在 PATH 里/根本没装，subprocess.run 直接抛
        # FileNotFoundError（是 OSError 的子类），不是走 returncode != 0 这条路。
        raise StoryGenerationError(f"启动 claude 命令失败（本机可能没装/没加进 PATH）: {exc}") from exc

    if proc.returncode != 0:
        raise StoryGenerationError(_format_cli_failure(proc.returncode, proc.stdout, proc.stderr, cli_path_override))

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

    鉴权同时带 x-api-key 和 Authorization: Bearer 两种头：官方 Anthropic API 认
    x-api-key(对应 ANTHROPIC_API_KEY)，但不少国内"中转/代理"服务(比如 Claude
    Code 自己认的 ANTHROPIC_AUTH_TOKEN 那一套)是走网关鉴权，只认 Authorization:
    Bearer，不认 x-api-key。两个头都带上、服务端各取所需，不用让用户自己判断
    "我这个中转服务到底吃哪种鉴权方式"。
    """
    url = f"{base_url.rstrip('/')}/v1/messages"
    try:
        resp = requests.post(
            url,
            headers={
                "x-api-key": api_key,
                "authorization": f"Bearer {api_key}",
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

    if not isinstance(data, dict):
        raise StoryGenerationError(f"第三方 API 返回格式不对(顶层不是 JSON object): {data!r}")

    # Anthropic Messages API: {"content": [{"type":"text", "text":"..."}]}
    blocks = data.get("content")
    if isinstance(blocks, list) and blocks:
        text_parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        content = "".join(text_parts).strip()
        if content:
            return content

    # Some third-party gateways expose an OpenAI-compatible response shape even when the
    # request endpoint is compatible enough for our payload. Accept it to avoid a 500-like UX.
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else first.get("text")
        if isinstance(content, str) and content.strip():
            return content.strip()

    raise StoryGenerationError(
        "第三方 API 返回格式不兼容：没有找到 Anthropic content 文本，"
        f"也没有找到 OpenAI choices 文本。返回内容：{str(data)[:2000]}"
    )


def generate_freeform_text(prompt: str, provider_config: dict) -> str:
    """跟 _attempt_generate 复用同一套 claude_cli/api 调用逻辑，但不做"必须解析出
    JSON 场次数组"这一步——给"AI优化提示词"这种只要一段纯文本回复的场景用。
    provider_config 形状跟 _attempt_generate 一致：{"provider", "baseUrl", "apiKey",
    "model", "maxTokens", "cliPath"}。
    """
    provider = provider_config.get("provider", DEFAULT_STORY_GEN_PROVIDER)
    if provider == "api":
        return call_anthropic_api(
            prompt,
            base_url=provider_config["baseUrl"],
            api_key=provider_config["apiKey"],
            model=provider_config["model"],
            max_tokens=provider_config.get("maxTokens") or DEFAULT_STORY_GEN_API_MAX_TOKENS,
        )
    return _call_claude_cli(prompt, provider_config.get("cliPath"))


def _attempt_generate(prompt: str, provider_config: dict) -> list[dict]:
    """跑一次生成 + 解析，失败就抛 StoryGenerationError，不在这里做重试判断。
    provider_config 形状: {"provider": "claude_cli"|"api", "baseUrl", "apiKey",
    "model", "maxTokens", "cliPath"}，"baseUrl"/"apiKey"/"model"/"maxTokens" 只有
    provider="api" 时才会用到，"cliPath" 只有 provider="claude_cli" 时才会用到
    (Setting.storyGenCliPath，用户手动指定的 CLI 路径，覆盖自动检测)。
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

    content = _call_claude_cli(prompt, provider_config.get("cliPath"))
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
    elif not _claude_command(provider_config.get("cliPath")):
        raise StoryGenerationError(missing_claude_message(provider_config.get("cliPath")))

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


def detect_claude_cli() -> str | None:
    """纯自动检测，忽略 Setting.storyGenCliPath 手动覆盖值——给设置页"自动检测"按钮用：
    检测到就把路径回填进输入框，用户看一眼确认后点"保存设置"，不用自己去翻文件系统
    抄这个路径(尤其是装在自定义 npm 全局目录、或者用了 nvm/volta 的情况下，路径经常
    很长很绕，手动抄很容易抄错一个字符)。
    """
    return _claude_command(None)


def test_claude_cli(cli_path_override: str | None = None, timeout_sec: int = TEST_TIMEOUT_SEC) -> tuple[bool, str]:
    """测本机 claude CLI 是否能正常调用。用短超时+一句极简的测试 prompt，
    不走 generate_story_scenes 那套"必须解析出 JSON 数组"的逻辑——测试连通性
    不需要真的生成分镜，claude 随便回句话就算通。
    cli_path_override 有值就只测这一个路径(设置页填的手动覆盖值，测试时还没保存
    也能先测)，没有就走自动检测。
    """
    command = _claude_command(cli_path_override)
    if not command:
        return False, missing_claude_message(cli_path_override)

    try:
        proc = subprocess.run(
            # 跟 _call_claude_cli 同一个理由：prompt 走 stdin，不拼进命令行参数，
            # 绕开 Windows 上 .cmd 转发导致的中文/编码问题。
            [command, "-p", "--output-format", "json"],
            input=_TEST_PROMPT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
            shell=_IS_WINDOWS,
        )
    except subprocess.TimeoutExpired:
        return False, f"claude CLI 超过 {timeout_sec} 秒没有返回，可能是没登录/网络问题，也可能只是这次比较慢"
    except OSError as exc:
        return False, f"启动 claude 命令失败（本机可能没装/没加进 PATH）: {exc}"

    if proc.returncode != 0:
        return False, _format_cli_failure(proc.returncode, proc.stdout, proc.stderr, cli_path_override)

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
