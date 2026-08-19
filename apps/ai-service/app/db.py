import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Prisma (apps/desktop/prisma/schema.prisma) 是 schema/迁移的唯一来源。
# 这里只负责连接同一个 SQLite 文件，用原生 SQL 读写，不引入第二套 ORM schema。
APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent.parent.parent
# 打包成 Windows 安装包之后，ai-service 源码跟 Electron 主程序不再共享同一个仓库
# 目录结构（源码被塞进 resources/ai-service/，没有 apps/ai-service/../../data 这种
# monorepo 相对路径可用），数据库也不该放在安装目录里（Windows 上安装目录通常
# 没有写权限，而且卸载重装不该丢用户数据）。所以：Electron 主进程在打包模式下会
# 通过 AI_MANJU_DB_PATH 环境变量传一个 userData 目录下的绝对路径进来；开发模式下
# 不传这个变量，走原来的仓库相对路径，行为完全不变。
_env_db_path = os.environ.get("AI_MANJU_DB_PATH")
DB_PATH = Path(_env_db_path) if _env_db_path else REPO_ROOT / "data" / "app.db"


def new_id() -> str:
    # 不需要和 Prisma 的 cuid() 完全一致，只需要在这张表里唯一。
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_INDEXTTS_BASE_URL = "http://localhost:7860"


# 下面 4 个"自定义提示词"字段落库是 JSON 文本(见 _ensure_startup_migrations 的
# customStylePrefixes/customStyleHints/customContentTypeHints/customProjectTemplates 列)。
# 约定：NULL/空 = 完全没自定义过，用代码里写死的默认值；非空就解析出来，跟默认值按 key
# 合并覆盖(项目模板是整个列表替换，不是按 key 合并，因为它是一份有序列表不是字典)。
_JSON_SETTING_KEYS = ("customStylePrefixes", "customStyleHints", "customContentTypeHints", "customProjectTemplates")


def get_settings(conn: sqlite3.Connection) -> dict:
    """读取单例 Setting 行，不存在则返回默认值（不落库，落库交给 PUT /settings）。"""
    row = conn.execute('SELECT * FROM "Setting" WHERE id = ?', ("singleton",)).fetchone()
    if row is None:
        return {
            "arkApiKey": None,
            "arkBaseUrl": None,
            "arkImageModel": None,
            "arkVideoModel": None,
            "indexTtsBaseUrl": DEFAULT_INDEXTTS_BASE_URL,
            "outputDir": None,
            "exportDir": None,
            "exportBurnSubtitles": True,
            "exportBgmPath": None,
            "exportBgmVolume": 0.2,
            "exportUseBgm": False,
            "customStylePrefixes": None,
            "customStyleHints": None,
            "customContentTypeHints": None,
            "customProjectTemplates": None,
            "posterFontPath": None,
            "arkTextModel": None,
            "storyGenProvider": "claude_cli",
            "storyGenApiBaseUrl": None,
            "storyGenApiKey": None,
            "storyGenApiModel": None,
            "storyGenApiMaxTokens": 4096,
            "storyGenCliPath": None,
            "storyGenPrompt": None,
            "storyGenTemplate": "vertical_short_drama",
            "videoProvider": "seedance",
            "minimaxApiKey": None,
        }
    d = dict(row)
    if not d.get("indexTtsBaseUrl"):
        d["indexTtsBaseUrl"] = DEFAULT_INDEXTTS_BASE_URL
    # SQLite 里 Boolean 落地是 0/1，这里转成真 bool，不然透传到前端 JSON 会变成数字 0/1
    # 而不是 false/true，前端 v-model 绑 checkbox 就会出问题。
    d["exportBurnSubtitles"] = bool(d.get("exportBurnSubtitles", True))
    d["exportUseBgm"] = bool(d.get("exportUseBgm", False))
    if d.get("exportBgmVolume") is None:
        d["exportBgmVolume"] = 0.2
    if not d.get("storyGenProvider"):
        d["storyGenProvider"] = "claude_cli"
    if not d.get("videoProvider"):
        d["videoProvider"] = "seedance"
    if d.get("storyGenApiMaxTokens") is None:
        d["storyGenApiMaxTokens"] = 4096
    if not d.get("storyGenTemplate"):
        d["storyGenTemplate"] = "vertical_short_drama"
    for key in _JSON_SETTING_KEYS:
        raw = d.get(key)
        if raw:
            try:
                d[key] = json.loads(raw)
            except (TypeError, ValueError):
                d[key] = None
        else:
            d[key] = None
    return d


_startup_migration_checked = False


def _ensure_startup_migrations(conn: sqlite3.Connection) -> None:
    """兜底自愈：给已经建好的旧库补几个新加的列，不强制用户先手动跑
    `npm run prisma:migrate`。只在进程生命周期内检查一次，很便宜。
    这里只做"加列"这种不破坏数据的操作，改字段类型/删列还是得走 Prisma 迁移。
    """
    global _startup_migration_checked
    if _startup_migration_checked:
        return
    cols = {r[1] for r in conn.execute('PRAGMA table_info("Shot")').fetchall()}
    if "transitionToNext" not in cols:
        conn.execute('ALTER TABLE "Shot" ADD COLUMN "transitionToNext" TEXT')
        conn.commit()
    if "emotion" not in cols:
        conn.execute('ALTER TABLE "Shot" ADD COLUMN "emotion" TEXT')
        conn.commit()
    project_cols = {r[1] for r in conn.execute('PRAGMA table_info("Project")').fetchall()}
    if "styleMode" not in project_cols:
        conn.execute('ALTER TABLE "Project" ADD COLUMN "styleMode" TEXT NOT NULL DEFAULT \'comic\'')
        conn.commit()
    if "contentType" not in project_cols:
        conn.execute('ALTER TABLE "Project" ADD COLUMN "contentType" TEXT NOT NULL DEFAULT \'character\'')
        conn.commit()
    if "lastExportedAt" not in project_cols:
        # 记一下这个项目最近一次导出成片成功的时间，NULL = 还没导出过。项目列表用它来显示
        # "已导出"标签——之前导出接口只是临时跑一遍 ffmpeg 返回结果，不落库，列表页完全
        # 看不出哪些项目已经出过成片，哪些还没有。
        conn.execute('ALTER TABLE "Project" ADD COLUMN "lastExportedAt" TEXT')
        conn.commit()
    if "aspectRatio" not in project_cols:
        # 生成比例：这部剧所有分镜图片/视频统一用这个比例(见 seedream.py 的
        # _aspect_ratio_for_shot)。默认 9:16 是加这一列之前分镜出图/出视频硬编码用的
        # 比例，老项目自愈迁移出这一列后生成行为完全不变。
        conn.execute('ALTER TABLE "Project" ADD COLUMN "aspectRatio" TEXT NOT NULL DEFAULT \'9:16\'')
        conn.commit()
    character_cols = {r[1] for r in conn.execute('PRAGMA table_info("Character")').fetchall()}
    if "prompt" not in character_cols:
        conn.execute('ALTER TABLE "Character" ADD COLUMN "prompt" TEXT')
        conn.commit()
    if "profile" not in character_cols:
        # 角色设定(身份/性格/背景)，跟 prompt(纯外观描述，喂给 Seedream)分开管理，
        # 见 schema.prisma 里 Character.profile 的注释。
        conn.execute('ALTER TABLE "Character" ADD COLUMN "profile" TEXT')
        conn.commit()
    setting_cols = {r[1] for r in conn.execute('PRAGMA table_info("Setting")').fetchall()}
    for col in _JSON_SETTING_KEYS:
        if col not in setting_cols:
            conn.execute(f'ALTER TABLE "Setting" ADD COLUMN "{col}" TEXT')
            conn.commit()
    if "posterFontPath" not in setting_cols:
        # 海报标题文字是代码(Pillow)渲染叠上去的，不是 AI 画的，需要一个真的支持中文的
        # 字体文件——留空就走 poster_composer.py 里按操作系统猜测的几个常见系统字体路径，
        # 猜不到就会在生成海报时报错，报错信息里会提示来这里手动填一个。
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "posterFontPath" TEXT')
        conn.commit()
    if "exportBgmPath" not in setting_cols:
        # 背景音乐：本地音频文件路径，导出时循环叠加到成片音轨下面(见 exporter.py)。
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "exportBgmPath" TEXT')
        conn.commit()
    if "exportBgmVolume" not in setting_cols:
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "exportBgmVolume" REAL NOT NULL DEFAULT 0.2')
        conn.commit()
    if "exportUseBgm" not in setting_cols:
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "exportUseBgm" BOOLEAN NOT NULL DEFAULT 0')
        conn.commit()
    if "storyGenProvider" not in setting_cols:
        # 剧本生成方式：claude_cli(默认，调本机 Claude Code CLI) | api(直连第三方
        # Anthropic Messages API 兼容服务)，见 story_generator.py。
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "storyGenProvider" TEXT NOT NULL DEFAULT \'claude_cli\'')
        conn.commit()
    if "storyGenApiBaseUrl" not in setting_cols:
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "storyGenApiBaseUrl" TEXT')
        conn.commit()
    if "storyGenApiKey" not in setting_cols:
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "storyGenApiKey" TEXT')
        conn.commit()
    if "storyGenApiModel" not in setting_cols:
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "storyGenApiModel" TEXT')
        conn.commit()
    if "storyGenApiMaxTokens" not in setting_cols:
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "storyGenApiMaxTokens" INTEGER NOT NULL DEFAULT 4096')
        conn.commit()
    if "storyGenCliPath" not in setting_cols:
        # claude_cli 模式下手动覆盖路径，留空 = 走自动检测，见 story_generator.py 的
        # _find_claude_candidates/_npm_global_prefix。
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "storyGenCliPath" TEXT')
        conn.commit()
    if "arkTextModel" not in setting_cols:
        # "AI优化提示词"功能用的纯文本对话模型，留空就回退到 storyGenProvider
        # (claude_cli/api)，见 routers/prompts.py。
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "arkTextModel" TEXT')
        conn.commit()

    if "storyGenPrompt" not in setting_cols:
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "storyGenPrompt" TEXT')
        conn.commit()
    if "storyGenTemplate" not in setting_cols:
        conn.execute(
            'ALTER TABLE "Setting" ADD COLUMN "storyGenTemplate" TEXT NOT NULL DEFAULT \'vertical_short_drama\''
        )
        conn.commit()
    if "videoProvider" not in setting_cols:
        # 视频生成走哪个 provider：seedance(默认，火山方舟) | minimax(MiniMax H3)，
        # 全局唯一一份设置，见 app/providers/minimax.py、app/providers/seedance.py。
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "videoProvider" TEXT DEFAULT \'seedance\'')
        conn.commit()
    if "minimaxApiKey" not in setting_cols:
        conn.execute('ALTER TABLE "Setting" ADD COLUMN "minimaxApiKey" TEXT')
        conn.commit()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    _POSTER_DDL_LEGACY_PRESET = """
        CREATE TABLE "Poster" (
            "id" TEXT NOT NULL PRIMARY KEY,
            "projectId" TEXT,
            "presetId" TEXT NOT NULL,
            "styleMode" TEXT NOT NULL DEFAULT 'comic',
            "title" TEXT NOT NULL,
            "subtitle" TEXT,
            "extraPrompt" TEXT,
            "referenceImagePaths" TEXT,
            "backgroundPath" TEXT,
            "filePath" TEXT,
            "status" TEXT NOT NULL DEFAULT 'pending',
            "error" TEXT,
            "providerId" TEXT,
            "model" TEXT,
            "createdAt" TEXT NOT NULL,
            CONSTRAINT "Poster_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE
        )
    """
    # 中间形态(已废弃)：presetId 拆成 orientation(竖版/横版) + category(医院/地陪/自定义)，
    # 只在这次会话短暂存在过，保留在这里纯粹是为了 _POSTER_DDL_CATEGORY 迁移分支能引用
    # 到旧列名，不会有真实用户数据库停留在这个形态太久。
    _POSTER_DDL_CATEGORY = """
        CREATE TABLE "Poster" (
            "id" TEXT NOT NULL PRIMARY KEY,
            "projectId" TEXT,
            "orientation" TEXT NOT NULL DEFAULT 'portrait',
            "category" TEXT NOT NULL DEFAULT 'hospital',
            "customPrompt" TEXT,
            "styleMode" TEXT NOT NULL DEFAULT 'comic',
            "title" TEXT NOT NULL,
            "subtitle" TEXT,
            "extraPrompt" TEXT,
            "referenceImagePaths" TEXT,
            "backgroundPath" TEXT,
            "filePath" TEXT,
            "status" TEXT NOT NULL DEFAULT 'pending',
            "error" TEXT,
            "providerId" TEXT,
            "model" TEXT,
            "createdAt" TEXT NOT NULL,
            CONSTRAINT "Poster_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE
        )
    """
    # 当前 schema：category(医院/地陪/自定义)写死三选一被开放模版库取代——
    # templateId 可选指向 PosterTemplate，templateLabel/promptText 是生成当下从模版
    # 复制的快照，layoutMode+bodyLines 支持价格表/知识卡片这种多行正文排版。
    _POSTER_DDL = """
        CREATE TABLE "Poster" (
            "id" TEXT NOT NULL PRIMARY KEY,
            "projectId" TEXT,
            "orientation" TEXT NOT NULL DEFAULT 'portrait',
            "templateId" TEXT,
            "templateLabel" TEXT,
            "promptText" TEXT,
            "layoutMode" TEXT NOT NULL DEFAULT 'title',
            "bodyLines" TEXT,
            "styleMode" TEXT NOT NULL DEFAULT 'comic',
            "title" TEXT NOT NULL,
            "subtitle" TEXT,
            "extraPrompt" TEXT,
            "referenceImagePaths" TEXT,
            "backgroundPath" TEXT,
            "filePath" TEXT,
            "status" TEXT NOT NULL DEFAULT 'pending',
            "error" TEXT,
            "providerId" TEXT,
            "model" TEXT,
            "createdAt" TEXT NOT NULL,
            CONSTRAINT "Poster_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE,
            CONSTRAINT "Poster_templateId_fkey" FOREIGN KEY ("templateId") REFERENCES "PosterTemplate" ("id") ON DELETE SET NULL ON UPDATE CASCADE
        )
    """
    # 预置模版文案，跟 prisma migration(20260811150000_poster_template_library)里的种子
    # 数据保持一致——两条腿(Prisma migrate 给真实桌面应用用、这里的自愈迁移给
    # ai-service 单跑/测试用)最终要落到同一份默认数据，用户体验不能因为走哪条路径
    # 而不一样。
    _DEFAULT_POSTER_TEMPLATES = [
        (
            "poster-tpl-hospital", "医院海报",
            "医疗/医院宣传海报主视觉，专业、干净、值得信赖的医疗环境氛围，可以出现医护人员、"
            "现代化医疗设备、明亮的医院环境等元素，色调明亮清爽。",
            "title",
        ),
        (
            "poster-tpl-guide", "地陪翻译海报",
            "海外旅游地陪/医美陪同翻译服务宣传海报主视觉，专业亲切的服务氛围，可以出现地陪/"
            "翻译人员微笑服务、旅游或医美机构场景等元素，色调温暖友好。",
            "title",
        ),
        (
            "poster-tpl-health", "医美科普海报",
            "医美/医疗科普知识主视觉，专业权威、清晰易懂的视觉风格，画面简洁大方，适合承载"
            "科普类图文内容，色调清新明亮。",
            "title",
        ),
        (
            "poster-tpl-hospital-kv", "医院主视觉海报",
            "韩国高端医美医院品牌主视觉，干净明亮的现代医疗空间、自然精致的东亚女性、柔和自然光、浅米灰低饱和背景，"
            "画面留出清晰标题区和人物/环境主视觉区，适合后期叠加中文品牌文案，不要生成任何文字或 logo。",
            "title",
        ),
        (
            "poster-tpl-hospital-service", "医院服务流程图",
            "韩国医美医院服务流程信息图背景，预约、到院、翻译沟通、咨询、术后关怀等服务场景，以人物和医疗空间作为视觉元素，"
            "浅色纸张质感、清晰分区、编号节点、柔和蓝绿橙色点缀，适合后期叠加中文步骤说明，不要生成任何文字。",
            "infographic",
        ),
        (
            "poster-tpl-hospital-compare", "医美项目对比图",
            "医美项目选择对比信息图背景，三个或四个并列卡片区域，东亚女性自然面部、皮肤管理、轮廓美学和抗衰护理等视觉元素，"
            "低饱和医疗高级感，卡片边界清楚，适合后期叠加项目特点、适合人群和注意事项，不要生成任何文字。",
            "infographic",
        ),
        (
            "poster-tpl-hospital-price", "医院项目价格信息图",
            "韩国医美医院项目价格信息图背景，整洁明亮的医院环境、局部人物与护理场景，顶部主视觉加下方多张价格卡片，"
            "专业可信、留白充足、层级清晰，适合后期叠加项目名称和价格，不要生成任何文字、数字或 logo。",
            "infographic",
        ),
        (
            "poster-tpl-price", "价格表海报",
            "医美/医疗项目价格表海报主视觉，简洁大方的背景，画面留白干净适合叠加价格列表"
            "文字，避免复杂花纹干扰阅读，色调专业沉稳。",
            "textBlocks",
        ),
        (
            "poster-tpl-card", "知识卡片",
            "医美知识科普卡片主视觉，清爽简洁的卡片式背景，画面干净适合叠加多条知识点文字，"
            "色调柔和易读。",
            "textBlocks",
        ),
        (
            "poster-tpl-travel-guide", "攻略信息图",
            "旅游/商圈/美食攻略信息图背景，浅色纸张质感，活泼但高级的旅行手账风，顶部主视觉"
            "可出现城市地标、街景、交通、美食小图标、地图路线感元素；画面适合后期叠加大量中文"
            "卡片信息、编号榜单和路线建议，必须留出清晰分区空间，不要生成任何文字。",
            "infographic",
        ),
        (
            "poster-tpl-transport-compare", "交通方式对比",
            "城市出行方式对比信息图背景，现代旅行攻略风，地铁、公交、出租车三类交通元素，"
            "蓝绿橙三色分栏视觉，干净卡片式结构，适合后期叠加价格、优缺点、适合人群等大量中文信息，"
            "不要生成任何文字。",
            "infographic",
        ),
        (
            "poster-tpl-taxi-guide", "出租车科普",
            "韩国出租车/海外打车攻略信息图背景，明亮旅行科普风，出租车、城市天际线、手机叫车、"
            "费用参考、注意事项等视觉元素，适合后期叠加多个卡片模块和编号说明，不要生成任何文字。",
            "infographic",
        ),
    ]

    def _seed_default_poster_templates() -> None:
        for tpl_id, label, prompt_text, layout_mode in _DEFAULT_POSTER_TEMPLATES:
            conn.execute(
                'INSERT OR IGNORE INTO "PosterTemplate" (id, label, promptText, layoutMode, createdAt) '
                "VALUES (?, ?, ?, ?, ?)",
                (tpl_id, label, prompt_text, layout_mode, now_iso()),
            )

    if "PosterTemplate" not in tables:
        # 模版库先于 Poster 建表：新 Poster 表的 templateId 外键要引用它。
        # 类型(医院海报/地陪翻译/科普知识/价格表/知识卡片……)不再写死在代码里，改成
        # 用户能自己增删改的一份清单，这里只是给冷启动预置几条覆盖常见场景。
        conn.execute(
            """
            CREATE TABLE "PosterTemplate" (
                "id" TEXT NOT NULL PRIMARY KEY,
                "label" TEXT NOT NULL,
                "promptText" TEXT NOT NULL,
                "layoutMode" TEXT NOT NULL DEFAULT 'title',
                "createdAt" TEXT NOT NULL
            )
            """
        )
        _seed_default_poster_templates()
        conn.commit()
    else:
        template_cols = {r[1] for r in conn.execute('PRAGMA table_info("PosterTemplate")').fetchall()}
        if "layoutMode" not in template_cols:
            conn.execute('ALTER TABLE "PosterTemplate" ADD COLUMN "layoutMode" TEXT NOT NULL DEFAULT \'title\'')
            conn.commit()
        _seed_default_poster_templates()
        conn.commit()

    if "Poster" not in tables:
        # 海报是独立的一级功能，不需要先建视频项目/写完剧本才能出海报。projectId 可选，
        # 纯粹是"这张海报是照哪个视频项目的调子出的"这种备注性质的关联。
        # backgroundPath 是 Seedream 生成的纯背景图(不含文字)，filePath 是叠了标题/副标题
        # 文字之后的最终成品图；只改文字重新排版不用重新调 AI，靠这两个字段分开存。
        conn.execute(_POSTER_DDL)
        conn.execute('CREATE INDEX "Poster_projectId_idx" ON "Poster"("projectId")')
        conn.execute('CREATE INDEX "Poster_templateId_idx" ON "Poster"("templateId")')
        conn.commit()
    else:
        poster_cols = {r[1] for r in conn.execute('PRAGMA table_info("Poster")').fetchall()}
        if "styleMode" not in poster_cols:
            # 兼容这个功能刚上线那几天创建的旧 Poster 表(projectId 是必填外键，没有
            # styleMode 列)——先原地升级到"带 presetId + styleMode"的中间形态，
            # 不丢已经生成过的海报记录，下面的迁移会接着把它升到最新形态。
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(_POSTER_DDL_LEGACY_PRESET.replace('"Poster"', '"new_Poster"', 1))
            common_cols = ", ".join(
                f'"{c}"' for c in [
                    "id", "projectId", "presetId", "title", "subtitle", "extraPrompt",
                    "referenceImagePaths", "backgroundPath", "filePath", "status", "error",
                    "providerId", "model", "createdAt",
                ]
                if c in poster_cols
            )
            conn.execute(f'INSERT INTO "new_Poster" ({common_cols}) SELECT {common_cols} FROM "Poster"')
            conn.execute('DROP TABLE "Poster"')
            conn.execute('ALTER TABLE "new_Poster" RENAME TO "Poster"')
            conn.execute('CREATE INDEX "Poster_projectId_idx" ON "Poster"("projectId")')
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()
            poster_cols = {r[1] for r in conn.execute('PRAGMA table_info("Poster")').fetchall()}
        if "orientation" not in poster_cols:
            # presetId -> orientation/category 迁移(中间形态)：旧的 poster_landscape 预设
            # 映射成 landscape，其余映射成 portrait；category 老数据统一给 'hospital'。
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(_POSTER_DDL_CATEGORY.replace('"Poster"', '"new_Poster"', 1))
            has_preset = "presetId" in poster_cols
            orientation_expr = (
                "CASE WHEN \"presetId\" = 'poster_landscape' THEN 'landscape' ELSE 'portrait' END"
                if has_preset else "'portrait'"
            )
            keep_cols = [
                "id", "projectId", "styleMode", "title", "subtitle", "extraPrompt",
                "referenceImagePaths", "backgroundPath", "filePath", "status", "error",
                "providerId", "model", "createdAt",
            ]
            common_cols = ", ".join(f'"{c}"' for c in keep_cols if c in poster_cols)
            select_cols = ", ".join(f'"{c}"' for c in keep_cols if c in poster_cols)
            conn.execute(
                f'INSERT INTO "new_Poster" ("orientation", "category", {common_cols}) '
                f"SELECT {orientation_expr}, 'hospital', {select_cols} FROM \"Poster\""
            )
            conn.execute('DROP TABLE "Poster"')
            conn.execute('ALTER TABLE "new_Poster" RENAME TO "Poster"')
            conn.execute('CREATE INDEX "Poster_projectId_idx" ON "Poster"("projectId")')
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()
            poster_cols = {r[1] for r in conn.execute('PRAGMA table_info("Poster")').fetchall()}
        if "templateId" not in poster_cols:
            # category/customPrompt -> templateId/templateLabel/promptText/layoutMode/
            # bodyLines 迁移：老数据没有模版可关联(templateId 留空)，但 promptText 尽量
            # 保留原本的内容提示语，不让老海报"重新生成"时突然变成空提示词。
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(_POSTER_DDL.replace('"Poster"', '"new_Poster"', 1))
            has_category = "category" in poster_cols
            template_label_expr = (
                "CASE \"category\" WHEN 'hospital' THEN '医院海报' WHEN 'guide' THEN '地陪翻译海报' ELSE '自定义' END"
                if has_category else "NULL"
            )
            prompt_text_expr = (
                "CASE \"category\" "
                "WHEN 'hospital' THEN '医疗/医院宣传海报主视觉，专业、干净、值得信赖的医疗环境氛围，"
                "可以出现医护人员、现代化医疗设备、明亮的医院环境等元素，色调明亮清爽。' "
                "WHEN 'guide' THEN '海外旅游地陪/医美陪同翻译服务宣传海报主视觉，专业亲切的服务氛围，"
                "可以出现地陪/翻译人员微笑服务、旅游或医美机构场景等元素，色调温暖友好。' "
                "ELSE \"customPrompt\" END"
                if has_category else "NULL"
            )
            keep_cols = [
                "id", "projectId", "orientation", "styleMode", "title", "subtitle", "extraPrompt",
                "referenceImagePaths", "backgroundPath", "filePath", "status", "error",
                "providerId", "model", "createdAt",
            ]
            common_cols = ", ".join(f'"{c}"' for c in keep_cols if c in poster_cols)
            select_cols = ", ".join(f'"{c}"' for c in keep_cols if c in poster_cols)
            conn.execute(
                f'INSERT INTO "new_Poster" ("templateLabel", "promptText", "layoutMode", {common_cols}) '
                f"SELECT {template_label_expr}, {prompt_text_expr}, 'title', {select_cols} FROM \"Poster\""
            )
            conn.execute('DROP TABLE "Poster"')
            conn.execute('ALTER TABLE "new_Poster" RENAME TO "Poster"')
            conn.execute('CREATE INDEX "Poster_projectId_idx" ON "Poster"("projectId")')
            conn.execute('CREATE INDEX "Poster_templateId_idx" ON "Poster"("templateId")')
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()

    if "VideoGeneration" not in tables:
        # 无剧本图生视频：独立一级功能，跟 Poster 一样不挂在任何 Project 下也能用，
        # projectId 只是可选的备注性关联。单张参考图 + 一段描述，直接调 Seedance
        # 出一条视频，不经过 Story/Scene/Shot 那一整套结构。
        conn.execute(
            """
            CREATE TABLE "VideoGeneration" (
                "id" TEXT NOT NULL PRIMARY KEY,
                "projectId" TEXT,
                "referenceImagePath" TEXT NOT NULL,
                "prompt" TEXT NOT NULL,
                "ratio" TEXT NOT NULL DEFAULT '9:16',
                "filePath" TEXT,
                "status" TEXT NOT NULL DEFAULT 'pending',
                "error" TEXT,
                "providerId" TEXT,
                "model" TEXT,
                "createdAt" TEXT NOT NULL,
                CONSTRAINT "VideoGeneration_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        conn.execute('CREATE INDEX "VideoGeneration_projectId_idx" ON "VideoGeneration"("projectId")')
        conn.commit()
    else:
        video_gen_cols = {r[1] for r in conn.execute('PRAGMA table_info("VideoGeneration")').fetchall()}
        if "ratio" not in video_gen_cols:
            # 生成比例：老库这一列加之前一直是硬编码 9:16，自愈迁移出来默认值保持一致，
            # 不影响老记录"重新生成"时的比例。
            conn.execute('ALTER TABLE "VideoGeneration" ADD COLUMN "ratio" TEXT NOT NULL DEFAULT \'9:16\'')
            conn.commit()

    if "TextImage" not in tables:
        # 独立文生图：同样不挂在 Project 下，纯"写描述 -> 出图"，跟海报共用出图风格
        # 前缀(styleMode)和画幅(orientation)概念，但不做标题文字合成。
        # referenceImagePaths 是老字段(不分类的参考图，早期版本用)，character/scene
        # 两个新字段上线后新建的记录不再往这个老字段写，只是保留兼容老数据。
        conn.execute(
            """
            CREATE TABLE "TextImage" (
                "id" TEXT NOT NULL PRIMARY KEY,
                "projectId" TEXT,
                "prompt" TEXT NOT NULL,
                "orientation" TEXT NOT NULL DEFAULT 'portrait',
                "styleMode" TEXT NOT NULL DEFAULT 'comic',
                "referenceImagePaths" TEXT,
                "characterReferenceImagePaths" TEXT,
                "sceneReferenceImagePaths" TEXT,
                "filePath" TEXT,
                "status" TEXT NOT NULL DEFAULT 'pending',
                "error" TEXT,
                "providerId" TEXT,
                "model" TEXT,
                "createdAt" TEXT NOT NULL,
                CONSTRAINT "TextImage_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        conn.execute('CREATE INDEX "TextImage_projectId_idx" ON "TextImage"("projectId")')
        conn.commit()
    else:
        # 老库已经有 TextImage 表(referenceImagePaths 是唯一一个不分类的参考图字段)，
        # 这里补两个新列，把"角色参考图"和"环境参考图"拆开管理，不影响老数据
        # (老数据留在 referenceImagePaths 里，_run_text_image_generation 仍会读它兜底)。
        text_image_cols = {r[1] for r in conn.execute('PRAGMA table_info("TextImage")').fetchall()}
        if "characterReferenceImagePaths" not in text_image_cols:
            conn.execute('ALTER TABLE "TextImage" ADD COLUMN "characterReferenceImagePaths" TEXT')
            conn.commit()
        if "sceneReferenceImagePaths" not in text_image_cols:
            conn.execute('ALTER TABLE "TextImage" ADD COLUMN "sceneReferenceImagePaths" TEXT')
            conn.commit()

    _startup_migration_checked = True


@contextmanager
def get_connection():
    if not DB_PATH.exists():
        raise RuntimeError(
            f"数据库文件不存在: {DB_PATH}。请先在 apps/desktop 下运行 "
            "`npm run prisma:migrate` 完成一次迁移，再启动 ai-service。"
        )
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 注意：特意不开 WAL。WAL 需要共享内存锁，SQLite 官方文档明确说明它在网络文件系统上
    # 不可靠；本机磁盘上没问题，但如果 data/ 目录被放进了 iCloud/OneDrive 之类的同步盘，
    # WAL 也可能出问题。用默认 rollback journal + busy_timeout 更稳。
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        _ensure_startup_migrations(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
