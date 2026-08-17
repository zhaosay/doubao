from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.providers.indextts import IndexTTSVoiceProvider
from app.providers.minimax import MiniMaxVideoProvider
from app.providers.registry import registry
from app.providers.seedance import SeedanceVideoProvider
from app.providers.seedream import SeedreamImageProvider
from app.routers import (
    characters,
    export,
    media_ratios,
    poster_templates,
    posters,
    projects,
    prompts,
    scenes,
    settings,
    shots,
    tasks,
    text_images,
    video_generations,
)
from app.services.paths import DEFAULT_OUTPUT_ROOT, resolve_static_file

registry.register("image", "default", SeedreamImageProvider())
# "video" 下注册两个可选实现，运行时按 Setting.videoProvider 选("default" 保留指向
# Seedance，兼容万一还有别处按老写法 resolve("video","default")，实际调用点(shots.py
# 的 _run_generation)已经改成按名字选)。
registry.register("video", "default", SeedanceVideoProvider())
registry.register("video", "seedance", SeedanceVideoProvider())
registry.register("video", "minimax", MiniMaxVideoProvider())
registry.register("voice", "default", IndexTTSVoiceProvider())

app = FastAPI(title="AI视频工作台 ai-service")

# 纯本地桌面应用的后端，不对公网暴露。dev 模式下 Electron 渲染进程跑在 Vite dev
# server 上，端口不固定（5173 被占用时 Vite 会自动换端口，比如 5174/5175），
# 打包后是 file:// 源；所以这里用正则放开任意端口的 localhost/127.0.0.1 +
# file://，而不是写死 5173（写死端口是之前 CORS 400 的原因）。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?|file://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(shots.router)
app.include_router(tasks.router)
app.include_router(settings.router)
app.include_router(export.router)
app.include_router(characters.router)
app.include_router(scenes.router)
app.include_router(posters.router)
app.include_router(poster_templates.router)
app.include_router(video_generations.router)
app.include_router(text_images.router)
app.include_router(media_ratios.router)
app.include_router(prompts.router)

DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


# 生成产物(图片/视频/音频)通过这个路由暴露给渲染进程，避免用 file:// 协议
# （Electron 里从 http(s)/dev-server 源加载 file:// 经常被 Chromium 拦掉）。
# 用手写路由而不是 StaticFiles 挂载，是因为 StaticFiles 的目录在 app 启动时就固定了，
# 用户在设置页改「目录设置」的 outputDir 之后没法立刻生效，得重启 ai-service；
# 这里每次请求都现查 resolve_static_file（内部会读最新设置），改完设置马上就能用。
@app.get("/files/{rel_path:path}")
def serve_generated_file(rel_path: str):
    resolved = resolve_static_file(rel_path)
    if resolved is None:
        raise HTTPException(404, "文件不存在")
    return FileResponse(resolved)


@app.get("/health")
def health():
    return {"status": "ok"}
