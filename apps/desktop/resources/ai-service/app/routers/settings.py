import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, get_settings
from app.services import ark_client
from app.services.story_generator import detect_claude_cli, test_anthropic_api, test_claude_cli

router = APIRouter(prefix="/settings", tags=["settings"])

StoryGenProvider = Literal["claude_cli", "api", "ark"]
VideoGenProvider = Literal["seedance", "minimax"]

# 跟 story_generator.py 的 STYLE_HINTS/CONTENT_TYPE_HINTS、seedream.py 的 STYLE_PREFIXES
# 用同一套 key，设置页存的自定义值是"按 key 覆盖"，不是整份替换，所以这里要知道
# 合法的 key 有哪些，防止存进去一个 claude/seedream 那边永远用不到的死 key。
_STYLE_MODE_KEYS = {"comic", "realistic", "render3d", "freeform"}
_CONTENT_TYPE_KEYS = {"character", "no_character"}


class UpdateSettingsBody(BaseModel):
    arkApiKey: Optional[str] = None
    arkBaseUrl: Optional[str] = None
    arkImageModel: Optional[str] = None
    arkVideoModel: Optional[str] = None
    # 纯文本对话模型，给"AI优化提示词"功能用，跟出图/出视频模型是两个不同的模型 ID。
    # 留空 = 不走 Ark，这个功能自动回退到 storyGenProvider(claude_cli/api)。
    arkTextModel: Optional[str] = None
    indexTtsBaseUrl: Optional[str] = None
    # 分镜/图生视频用哪个 provider：seedance(默认) | minimax。切到 minimax 时要求
    # minimaxApiKey 必填，校验逻辑见 update_settings 里的 video_provider 分支。
    videoProvider: Optional[VideoGenProvider] = None
    minimaxApiKey: Optional[str] = None
    # 目录设置：留空字符串表示"清空自定义值，恢复默认目录"，跟 None(不修改这个字段)是两回事，
    # 所以下面统一用「传了空字符串就存 None」而不是「空字符串当成没传」。
    outputDir: Optional[str] = None
    exportDir: Optional[str] = None
    exportBurnSubtitles: Optional[bool] = None
    # 背景音乐：本地音频文件路径，留空 = 不设置(即使 exportUseBgm 开着也不会加，因为
    # 没有素材)。跟 outputDir 一样的约定，传空字符串是"清空自定义值"。
    exportBgmPath: Optional[str] = None
    # 0~1 之间，成片原本的音轨保持原音量，背景音乐按这个系数降低。
    exportBgmVolume: Optional[float] = None
    exportUseBgm: Optional[bool] = None
    # 海报标题/副标题文字渲染用的字体文件路径，留空 = 用 poster_composer.py 按操作系统
    # 猜测的几个常见系统字体路径；跟 outputDir 一样的约定，传空字符串是"清空自定义值"。
    posterFontPath: Optional[str] = None
    # 下面 4 个是自定义提示词，前端传的是 JSON 字符串（跟 outputDir 一样的约定）：
    # None = 不改这个字段；"" = 清空自定义，恢复代码里写死的默认值；非空字符串 = 新的
    # JSON 内容，这里会校验一遍格式再存，不是随便一段文本都收。
    customStylePrefixes: Optional[str] = None
    customStyleHints: Optional[str] = None
    customContentTypeHints: Optional[str] = None
    customProjectTemplates: Optional[str] = None
    # 剧本生成方式：claude_cli(默认，本机 Claude Code CLI) | api(第三方 Anthropic
    # Messages API 兼容服务)。切到 api 时下面三个字段(baseUrl/apiKey/model)必填，
    # 校验逻辑见 update_settings 里的 story_gen_provider 分支。
    storyGenProvider: Optional[StoryGenProvider] = None
    storyGenApiBaseUrl: Optional[str] = None
    storyGenApiKey: Optional[str] = None
    storyGenApiModel: Optional[str] = None
    storyGenApiMaxTokens: Optional[int] = None
    # Ark 剧本使用通用方舟配置里的 API Key + 文本模型；这两个字段只负责剧本内容本身。
    storyGenPrompt: Optional[str] = None
    storyGenTemplate: Optional[str] = None
    # claude_cli 模式下的手动覆盖路径：留空(None 或空字符串) = 走自动检测(PATH 查找+
    # Windows 常见安装目录扫描+npm 全局 prefix 动态查询)；填了就只认这一个路径，
    # 给"自动检测都找不到/找到的是错的"这种情况一个逃生舱口，不用等我们再加新的
    # 检测规则。跟 outputDir 一样的约定：传空字符串是"清空自定义值，恢复自动检测"。
    storyGenCliPath: Optional[str] = None


class TestStoryApiBody(BaseModel):
    # 全部留空 = 用已保存的设置去测；填了哪个字段就用哪个字段覆盖已保存的值——
    # 这样用户改完 Base URL/API Key 但还没点保存，也能先测一下填得对不对，
    # 不用先保存再测、测完发现错了再改一遍。
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    model: Optional[str] = None
    maxTokens: Optional[int] = None


class TestStoryCliBody(BaseModel):
    # 留空 = 用已保存的 storyGenCliPath(没保存过就走自动检测)；填了就只测这一个路径——
    # 跟 TestStoryApiBody 一样，方便用户填完还没点保存就先测一下对不对。
    cliPath: Optional[str] = None


def _mask(key: Optional[str]) -> Optional[str]:
    """API Key 只在保存时接收明文，读取时脱敏展示，避免整段明文被前端日志/截图带出去。"""
    if not key:
        return None
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


def _validate_dir(label: str, raw: Optional[str]) -> Optional[str]:
    """目录设置存进去之前先验证一下能不能真的写进去，不然要等到下次生成/导出才报错，
    定位起来很绕。空字符串/None 都当"不设置自定义目录"处理，返回 None。
    """
    if not raw or not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise HTTPException(400, f"{label}不可用（{path}）：{exc}") from exc
    return str(path)


def _validate_font_path(raw: Optional[str]) -> Optional[str]:
    """海报字体路径：跟目录设置不同，这里不要求"能写入"，而是要求"这个文件真的存在，
    且 Pillow 真的能把它当字体加载"——填错路径/填了个不是字体的文件，等到生成海报那一刻
    才报错就太晚了，保存设置的时候就先验证一遍。
    """
    if not raw or not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_file():
        raise HTTPException(400, f"海报字体文件不存在: {path}")
    try:
        from PIL import ImageFont  # noqa: PLC0415 只有校验这一下需要，不想让整个路由强依赖 Pillow

        ImageFont.truetype(str(path), 32)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"这个文件不是 Pillow 能识别的字体格式: {path}（{exc}）") from exc
    return str(path)


def _validate_bgm_path(raw: Optional[str]) -> Optional[str]:
    """背景音乐文件：只要求文件存在，不像海报字体那样再校验格式——音频格式一大堆
    (mp3/wav/aac/m4a...)，靠 ffmpeg 自己去读，读不了的话混音那一步会失败，
    exporter.py 已经做了"混音失败就回退用没有背景音乐的版本"，不会拖垮整个导出。
    """
    if not raw or not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_file():
        raise HTTPException(400, f"背景音乐文件不存在: {path}")
    return str(path)


def _validate_cli_path(raw: Optional[str]) -> Optional[str]:
    """claude CLI 手动覆盖路径：只要求文件存在，不校验"是不是真的能跑通"——
    真正能不能调用得靠"测试连通性"按钮实际跑一次(网络/登录状态这些静态校验查不出来)，
    这里只挡"明显填错路径"这种低级错误，保存的时候就先提醒，不用等到生成剧本才报错。
    """
    if not raw or not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_file():
        raise HTTPException(400, f"claude CLI 路径不存在: {path}")
    return str(path)


def _validate_bgm_volume(value: Optional[float]) -> float:
    if value is None:
        return 0.2
    if not (0 <= value <= 1):
        raise HTTPException(400, "背景音乐音量必须在 0~1 之间")
    return value


def _validate_story_gen_max_tokens(value: Optional[int]) -> int:
    if value is None:
        return 4096
    if not (1 <= value <= 200000):
        raise HTTPException(400, "最大输出 Token 必须在 1~200000 之间")
    return value


def _validate_key_map(label: str, raw: str, allowed_keys: set[str]) -> str:
    """校验自定义提示词字段(comic/realistic/... -> 一段提示文字)：必须是 JSON 对象，
    key 只能是 allowed_keys 里的，value 必须是字符串。不要求填满所有 key——
    没填的 key 生成时照样会退回代码里的默认值，只是这几个具体的 key 被覆盖了。
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"{label}不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, f"{label}必须是一个 JSON 对象（key -> 提示文字）")
    for key, value in data.items():
        if key not in allowed_keys:
            raise HTTPException(400, f"{label}里的 key「{key}」不认识，只支持：{sorted(allowed_keys)}")
        if not isinstance(value, str):
            raise HTTPException(400, f"{label}里 key「{key}」的值必须是字符串")
    return json.dumps(data, ensure_ascii=False)


def _validate_project_templates(raw: str) -> str:
    """自定义项目模板是一份有序列表，整份替换(不是按 key 合并)——因为它本质是"新建项目"
    页那几张模板卡片，用户想要的是"我自己排的这几张卡"，不是跟内置的合并去重。
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"项目模板不是合法的 JSON：{exc}") from exc
    if not isinstance(data, list) or not data:
        raise HTTPException(400, "项目模板必须是一个非空的 JSON 数组")
    seen_ids: set[str] = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise HTTPException(400, f"项目模板第 {i + 1} 项必须是一个对象")
        item_id = item.get("id")
        label = item.get("label")
        content_type = item.get("contentType")
        style_mode = item.get("styleMode")
        if not item_id or not isinstance(item_id, str):
            raise HTTPException(400, f"项目模板第 {i + 1} 项缺少 id")
        if item_id in seen_ids:
            raise HTTPException(400, f"项目模板里 id「{item_id}」重复了")
        seen_ids.add(item_id)
        if not label or not isinstance(label, str):
            raise HTTPException(400, f"项目模板第 {i + 1} 项缺少 label（卡片标题）")
        if content_type not in _CONTENT_TYPE_KEYS:
            raise HTTPException(400, f"项目模板第 {i + 1} 项 contentType 必须是 {sorted(_CONTENT_TYPE_KEYS)} 之一")
        if style_mode not in _STYLE_MODE_KEYS:
            raise HTTPException(400, f"项目模板第 {i + 1} 项 styleMode 必须是 {sorted(_STYLE_MODE_KEYS)} 之一")
        if not isinstance(item.get("description", ""), str):
            raise HTTPException(400, f"项目模板第 {i + 1} 项 description 必须是字符串")
    return json.dumps(data, ensure_ascii=False)


@router.get("")
def read_settings():
    with get_connection() as conn:
        s = get_settings(conn)
    return {
        "arkApiKey": _mask(s.get("arkApiKey")),
        "arkApiKeySet": bool(s.get("arkApiKey")),
        "arkBaseUrl": s.get("arkBaseUrl"),
        "arkImageModel": s.get("arkImageModel"),
        "arkVideoModel": s.get("arkVideoModel"),
        "arkTextModel": s.get("arkTextModel"),
        "indexTtsBaseUrl": s.get("indexTtsBaseUrl"),
        "outputDir": s.get("outputDir"),
        "exportDir": s.get("exportDir"),
        "exportBurnSubtitles": s.get("exportBurnSubtitles", True),
        "exportBgmPath": s.get("exportBgmPath"),
        "exportBgmVolume": s.get("exportBgmVolume", 0.2),
        "exportUseBgm": s.get("exportUseBgm", False),
        "posterFontPath": s.get("posterFontPath"),
        # 这 4 个 get_settings 已经从 JSON 文本解析成 dict/list 了，null 就是没自定义过。
        "customStylePrefixes": s.get("customStylePrefixes"),
        "customStyleHints": s.get("customStyleHints"),
        "customContentTypeHints": s.get("customContentTypeHints"),
        "customProjectTemplates": s.get("customProjectTemplates"),
        "storyGenProvider": s.get("storyGenProvider", "claude_cli"),
        "storyGenApiBaseUrl": s.get("storyGenApiBaseUrl"),
        "storyGenApiKey": _mask(s.get("storyGenApiKey")),
        "storyGenApiKeySet": bool(s.get("storyGenApiKey")),
        "storyGenApiModel": s.get("storyGenApiModel"),
        "storyGenApiMaxTokens": s.get("storyGenApiMaxTokens", 4096),
        "storyGenCliPath": s.get("storyGenCliPath"),
        "storyGenPrompt": s.get("storyGenPrompt"),
        "storyGenTemplate": s.get("storyGenTemplate", "vertical_short_drama"),
        "videoProvider": s.get("videoProvider") or "seedance",
        "minimaxApiKey": _mask(s.get("minimaxApiKey")),
        "minimaxApiKeySet": bool(s.get("minimaxApiKey")),
    }


@router.put("")
def update_settings(body: UpdateSettingsBody):
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        # 这里特意用 SELECT * 拿原始行，而不是 get_settings()——get_settings 会把
        # customXxx 几个字段从 JSON 文本解析成 dict/list，但下面"没传就保留原值"这一步
        # 需要的是原始 JSON 文本本身，拿解析后的对象没法直接存回 TEXT 列。
        existing = conn.execute('SELECT * FROM "Setting" WHERE id = ?', ("singleton",)).fetchone()
        existing_dict = dict(existing) if existing else {}
        current = get_settings(conn)

        ark_api_key = body.arkApiKey if body.arkApiKey is not None else current.get("arkApiKey")
        ark_base_url = ark_client.normalize_base_url(
            body.arkBaseUrl if body.arkBaseUrl is not None else current.get("arkBaseUrl")
        )
        ark_image_model = (
            body.arkImageModel if body.arkImageModel is not None else current.get("arkImageModel")
        )
        ark_video_model = (
            body.arkVideoModel if body.arkVideoModel is not None else current.get("arkVideoModel")
        )
        ark_text_model = (
            body.arkTextModel if body.arkTextModel is not None else current.get("arkTextModel")
        )
        indextts_base_url = (
            body.indexTtsBaseUrl if body.indexTtsBaseUrl is not None else current.get("indexTtsBaseUrl")
        )
        video_provider = body.videoProvider if body.videoProvider is not None else current.get("videoProvider", "seedance")
        minimax_api_key = body.minimaxApiKey if body.minimaxApiKey is not None else current.get("minimaxApiKey")
        # 切到 minimax 就必须填了 API Key，不然存进去一个"选了 minimax 但没配置 key"的
        # 半吊子状态，等到真正生成视频那一刻才报错，跟 storyGenProvider=api 的校验是同一个思路。
        if video_provider == "minimax" and not (minimax_api_key or "").strip():
            raise HTTPException(400, "选了 MiniMax 生成视频，还差 API Key 没填")
        output_dir = (
            _validate_dir("目录设置", body.outputDir) if body.outputDir is not None else current.get("outputDir")
        )
        export_dir = (
            _validate_dir("导出设置里的导出目录", body.exportDir)
            if body.exportDir is not None
            else current.get("exportDir")
        )
        export_burn_subtitles = (
            body.exportBurnSubtitles
            if body.exportBurnSubtitles is not None
            else current.get("exportBurnSubtitles", True)
        )
        poster_font_path = (
            _validate_font_path(body.posterFontPath)
            if body.posterFontPath is not None
            else current.get("posterFontPath")
        )
        export_bgm_path = (
            _validate_bgm_path(body.exportBgmPath) if body.exportBgmPath is not None else current.get("exportBgmPath")
        )
        export_bgm_volume = (
            _validate_bgm_volume(body.exportBgmVolume)
            if body.exportBgmVolume is not None
            else current.get("exportBgmVolume", 0.2)
        )
        export_use_bgm = body.exportUseBgm if body.exportUseBgm is not None else current.get("exportUseBgm", False)

        story_gen_cli_path = (
            _validate_cli_path(body.storyGenCliPath)
            if body.storyGenCliPath is not None
            else current.get("storyGenCliPath")
        )

        story_gen_provider = (
            body.storyGenProvider if body.storyGenProvider is not None else current.get("storyGenProvider", "claude_cli")
        )
        story_gen_api_base_url = (
            body.storyGenApiBaseUrl if body.storyGenApiBaseUrl is not None else current.get("storyGenApiBaseUrl")
        )
        story_gen_api_key = (
            body.storyGenApiKey if body.storyGenApiKey is not None else current.get("storyGenApiKey")
        )
        story_gen_api_model = (
            body.storyGenApiModel if body.storyGenApiModel is not None else current.get("storyGenApiModel")
        )
        story_gen_api_max_tokens = (
            _validate_story_gen_max_tokens(body.storyGenApiMaxTokens)
            if body.storyGenApiMaxTokens is not None
            else current.get("storyGenApiMaxTokens", 4096)
        )
        story_gen_prompt = body.storyGenPrompt if body.storyGenPrompt is not None else current.get("storyGenPrompt")
        story_gen_template = (
            body.storyGenTemplate.strip()
            if body.storyGenTemplate is not None and body.storyGenTemplate.strip()
            else current.get("storyGenTemplate", "vertical_short_drama")
        )
        # 切到"第三方 API"就必须把三个字段都填完整，不然存进去一个"选了 api 但没配置全"
        # 的半吊子状态，等到真正生成剧本那一刻才报错，体验比现在保存时就拦下来更差。
        if story_gen_provider == "api":
            missing = [
                label
                for label, value in [
                    ("Base URL", story_gen_api_base_url),
                    ("API Key", story_gen_api_key),
                    ("模型名", story_gen_api_model),
                ]
                if not (value or "").strip()
            ]
            if missing:
                raise HTTPException(400, f"选了第三方 API 生成剧本，还差这些没填：{'、'.join(missing)}")
        elif story_gen_provider == "ark":
            if not (ark_api_key or "").strip() or not (ark_text_model or "").strip():
                raise HTTPException(400, "选了火山方舟生成剧本，还差方舟 API Key 或 Ark 文本模型 ID")

        def _resolve_json_field(body_value: Optional[str], column: str, validator) -> Optional[str]:
            if body_value is None:
                return existing_dict.get(column)
            if not body_value.strip():
                return None
            return validator(body_value)

        custom_style_prefixes = _resolve_json_field(
            body.customStylePrefixes,
            "customStylePrefixes",
            lambda raw: _validate_key_map("出图风格前缀", raw, _STYLE_MODE_KEYS),
        )
        custom_style_hints = _resolve_json_field(
            body.customStyleHints,
            "customStyleHints",
            lambda raw: _validate_key_map("剧本写作风格提示", raw, _STYLE_MODE_KEYS),
        )
        custom_content_type_hints = _resolve_json_field(
            body.customContentTypeHints,
            "customContentTypeHints",
            lambda raw: _validate_key_map("内容类型提示", raw, _CONTENT_TYPE_KEYS),
        )
        custom_project_templates = _resolve_json_field(
            body.customProjectTemplates, "customProjectTemplates", _validate_project_templates
        )

        if existing is None:
            conn.execute(
                'INSERT INTO "Setting" (id, arkApiKey, arkBaseUrl, arkImageModel, arkVideoModel, arkTextModel, '
                "indexTtsBaseUrl, outputDir, exportDir, exportBurnSubtitles, exportBgmPath, "
                "exportBgmVolume, exportUseBgm, "
                "customStylePrefixes, customStyleHints, customContentTypeHints, customProjectTemplates, "
                "posterFontPath, storyGenProvider, storyGenApiBaseUrl, storyGenApiKey, storyGenApiModel, "
                "storyGenApiMaxTokens, storyGenCliPath, storyGenPrompt, storyGenTemplate, videoProvider, minimaxApiKey, updatedAt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "singleton",
                    ark_api_key,
                    ark_base_url,
                    ark_image_model,
                    ark_video_model,
                    ark_text_model,
                    indextts_base_url,
                    output_dir,
                    export_dir,
                    export_burn_subtitles,
                    export_bgm_path,
                    export_bgm_volume,
                    export_use_bgm,
                    custom_style_prefixes,
                    custom_style_hints,
                    custom_content_type_hints,
                    custom_project_templates,
                    poster_font_path,
                    story_gen_provider,
                    story_gen_api_base_url,
                    story_gen_api_key,
                    story_gen_api_model,
                    story_gen_api_max_tokens,
                    story_gen_cli_path,
                    story_gen_prompt,
                    story_gen_template,
                    video_provider,
                    minimax_api_key,
                    now,
                ),
            )
        else:
            conn.execute(
                'UPDATE "Setting" SET arkApiKey = ?, arkBaseUrl = ?, arkImageModel = ?, arkVideoModel = ?, '
                "arkTextModel = ?, "
                "indexTtsBaseUrl = ?, outputDir = ?, exportDir = ?, exportBurnSubtitles = ?, "
                "exportBgmPath = ?, exportBgmVolume = ?, exportUseBgm = ?, "
                "customStylePrefixes = ?, customStyleHints = ?, customContentTypeHints = ?, "
                "customProjectTemplates = ?, posterFontPath = ?, storyGenProvider = ?, "
                'storyGenApiBaseUrl = ?, storyGenApiKey = ?, storyGenApiModel = ?, storyGenApiMaxTokens = ?, '
                'storyGenCliPath = ?, storyGenPrompt = ?, storyGenTemplate = ?, videoProvider = ?, minimaxApiKey = ?, updatedAt = ? WHERE id = ?',
                (
                    ark_api_key,
                    ark_base_url,
                    ark_image_model,
                    ark_video_model,
                    ark_text_model,
                    indextts_base_url,
                    output_dir,
                    export_dir,
                    export_burn_subtitles,
                    export_bgm_path,
                    export_bgm_volume,
                    export_use_bgm,
                    custom_style_prefixes,
                    custom_style_hints,
                    custom_content_type_hints,
                    custom_project_templates,
                    poster_font_path,
                    story_gen_provider,
                    story_gen_api_base_url,
                    story_gen_api_key,
                    story_gen_api_model,
                    story_gen_api_max_tokens,
                    story_gen_cli_path,
                    story_gen_prompt,
                    story_gen_template,
                    video_provider,
                    minimax_api_key,
                    now,
                    "singleton",
                ),
            )

    return read_settings()


@router.post("/test-story-cli")
def test_story_cli(body: TestStoryCliBody = TestStoryCliBody()):
    """测本机 claude CLI（Claude Code CLI）是否能正常调用。用户在 Windows 上反馈
    "AI生成剧本"一直出错，但不确定是没装/没登录/网络问题——这个按钮不改任何设置，
    只是跑一次真实的 subprocess 调用，把结果原样返回，让用户自己看出到底卡在哪。
    body.cliPath 有值就只测这一个路径(设置页填了手动覆盖但还没点保存也能先测)，
    没填就用已保存的 storyGenCliPath(没保存过就走自动检测)。
    """
    with get_connection() as conn:
        current = get_settings(conn)
    cli_path = body.cliPath if body.cliPath is not None else current.get("storyGenCliPath")
    ok, message = test_claude_cli(cli_path)
    return {"ok": ok, "message": message}


@router.post("/detect-story-cli-path")
def detect_story_cli_path():
    """给设置页"自动检测"按钮用：忽略任何已保存的手动覆盖值，纯跑一遍自动检测逻辑
    (PATH 查找 + Windows 常见安装目录扫描 + npm 全局 prefix 动态查询 + 僵尸 shim 识别)，
    检测到就把路径返回给前端回填进输入框，用户看一眼确认后再点"保存设置"——
    不改任何已保存的设置，纯只读探测。
    """
    path = detect_claude_cli()
    if path:
        return {"found": True, "path": path}
    return {"found": False, "path": None, "message": "自动检测未找到 claude CLI，请手动填写完整路径"}


@router.post("/test-story-api")
def test_story_api(body: TestStoryApiBody):
    """测第三方 Anthropic Messages API 兼容服务是否配置正确、能不能连通。
    body 里的字段是可选的覆盖值：填了就用填的值测，没填就用已保存在 Setting 表里的值——
    这样用户在设置页改完 Base URL/API Key 还没点保存，也能先测一下再决定要不要保存。
    这个接口是设置页按钮直接调用的，任何配置/网络/返回格式问题都应该变成
    {ok:false,message}，不要让 FastAPI 返回 500。
    """
    try:
        with get_connection() as conn:
            current = get_settings(conn)

        base_url = body.baseUrl if body.baseUrl is not None else current.get("storyGenApiBaseUrl")
        api_key = body.apiKey if body.apiKey is not None else current.get("storyGenApiKey")
        model = body.model if body.model is not None else current.get("storyGenApiModel")
        max_tokens = body.maxTokens if body.maxTokens is not None else current.get("storyGenApiMaxTokens")

        ok, message = test_anthropic_api(base_url, api_key, model, max_tokens)
        return {"ok": ok, "message": message}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"测试第三方 API 失败：{exc}"}
