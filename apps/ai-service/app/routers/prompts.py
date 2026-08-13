from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, get_settings
from app.routers.projects import _build_story_provider_config
from app.services.ark_client import ArkError, chat_completion
from app.services.story_generator import StoryGenerationError, generate_freeform_text

# 独立的一级路由，不挂在任何具体功能(海报/文生图/图生视频/分镜/角色/场景)下面——
# "优化提示词"是所有画面描述输入框共用的一个小工具，不属于其中任何一个模块。
router = APIRouter(prefix="/prompts", tags=["prompts"])

_OPTIMIZE_SYSTEM_PROMPT = (
    "你是一个专业的 AI 绘画/视频生成提示词优化助手。用户会给你一段现有的画面生成提示词，"
    "你的任务是把它改写得更清晰、更具体、细节更丰富(比如补充构图、光线、材质、氛围等画面"
    "细节)，从而提高 AI 生成图片/视频的质量和准确度。但不要改变原提示词想表达的核心内容，"
    "不要凭空添加与原意无关的新元素或角色。只输出优化后的提示词本身，不要加任何解释、前后缀"
    "或引号。"
)


class OptimizePromptBody(BaseModel):
    prompt: str
    # 可选的场景背景信息(比如"这是一张海报的画面描述"/"这是短剧第3镜的画面描述，
    # 风格是国风水墨")，帮助 AI 优化得更贴合场景，不是必填。
    context: Optional[str] = None


def _build_optimize_instruction(prompt: str, context: Optional[str]) -> str:
    context_line = f"场景背景：{context.strip()}\n\n" if context and context.strip() else ""
    return f"{context_line}原始提示词：\n{prompt.strip()}"


@router.post("/optimize")
def optimize_prompt(body: OptimizePromptBody):
    """优化一段提示词。优先用 Ark 的文本对话模型(如果配置了 arkApiKey + arkTextModel，
    速度快、不依赖本机终端环境)；没配置就自动回退到已经配置好的"剧本生成方式"
    (storyGenProvider: claude_cli 本机终端 / api 第三方 Anthropic 兼容服务)——这两条
    路径复用的是"AI生成剧本"功能已经打通的调用逻辑，用户不需要为这一个小功能单独配一套
    连通性。两条路都没配置好时给出明确的报错，指引去设置页配一个。
    """
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "提示词不能为空")

    instruction = _build_optimize_instruction(prompt, body.context)

    with get_connection() as conn:
        settings = get_settings(conn)

    ark_api_key = settings.get("arkApiKey")
    ark_text_model = settings.get("arkTextModel")
    if ark_api_key and ark_text_model:
        try:
            optimized = chat_completion(
                api_key=ark_api_key,
                prompt=instruction,
                model=ark_text_model,
                system=_OPTIMIZE_SYSTEM_PROMPT,
                base_url=settings.get("arkBaseUrl") or "https://ark.cn-beijing.volces.com/api/plan/v3",
            )
            return {"optimizedPrompt": optimized, "engine": "ark"}
        except ArkError as exc:
            raise HTTPException(400, f"用 Ark 文本模型优化提示词失败：{exc}") from exc

    provider_config = _build_story_provider_config(settings)
    provider = provider_config.get("provider")
    if provider == "api" and not (provider_config.get("baseUrl") and provider_config.get("apiKey") and provider_config.get("model")):
        raise HTTPException(
            400,
            "还没配置任何可用于「AI优化提示词」的能力：去设置页「Ark配置」填 API Key + 文本模型，"
            "或者去「AI生成剧本配置」把第三方 API 配置完整，也可以直接用默认的本机 claude CLI。",
        )

    try:
        optimized = generate_freeform_text(f"{_OPTIMIZE_SYSTEM_PROMPT}\n\n{instruction}", provider_config)
        return {"optimizedPrompt": optimized.strip(), "engine": provider}
    except StoryGenerationError as exc:
        raise HTTPException(400, f"优化提示词失败：{exc}") from exc
