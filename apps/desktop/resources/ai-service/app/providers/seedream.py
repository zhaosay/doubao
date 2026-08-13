from app.db import get_connection, get_settings
from app.providers.registry import ImageProvider
from app.services import ark_client
from app.services.paths import asset_dir, character_dir, poster_dir, scene_dir, text_image_dir

# 统一注入的风格前缀：不再只指望 claude 写剧本时每一镜都记得重复"国漫赛璐璐风格"，
# 那样只要漏写一次，Seedream 在没有参考图兜底的情况下就容易飘去写实/真人方向。
# 现在不管 drawPrompt 里写没写，每次生图都会带上对应 styleMode 的这句话。
# styleMode 是项目级别的设置(Project.styleMode)，一部剧通常从头到尾风格一致，
# 所以角色设定图/场景参考图/镜头画面统一按同一个项目的设置来注入，不是每次生图单独选。
# freeform(AI自由发挥) 的前缀是空字符串，故意不注入任何风格描述——完全交给模型自己
# 发挥，适合还没想清楚要什么风格、想先看看模型默认长什么样的场景。
STYLE_PREFIXES = {
    "comic": "国漫赛璐璐风格，二次元厚涂动画质感，禁止写实摄影/真人风格。",
    "realistic": "真实摄影质感，真人实拍风格，自然光影，电影级写实感，禁止动画/漫画/卡通风格。",
    "render3d": "3D渲染动画质感，CG角色建模，皮克斯/迪士尼3D动画电影级渲染，柔和全局光照，"
    "合成质感，禁止2D手绘/真人摄影/赛璐璐风格。",
    "freeform": "",
}
DEFAULT_STYLE_MODE = "comic"


def _with_style(
    prompt: str, style_mode: str = DEFAULT_STYLE_MODE, custom_prefixes: dict | None = None
) -> str:
    """custom_prefixes 是设置页里存的「出图风格前缀」自定义值(Setting.customStylePrefixes,
    按 styleMode 的 key 覆盖)，没有自定义就退回代码里写死的 STYLE_PREFIXES。
    跟 freeform 一样，如果自定义值本身是空字符串，也当成"不注入风格前缀"处理。
    """
    if custom_prefixes and style_mode in custom_prefixes:
        prefix = custom_prefixes[style_mode]
    else:
        prefix = STYLE_PREFIXES.get(style_mode, STYLE_PREFIXES[DEFAULT_STYLE_MODE])
    if not prefix:
        return prompt
    return f"{prefix} {prompt}"


def _style_mode_for_character(conn, character_id: str) -> str:
    row = conn.execute(
        'SELECT p.styleMode AS styleMode FROM "Character" c '
        'JOIN "Story" s ON c.storyId = s.id JOIN "Project" p ON s.projectId = p.id WHERE c.id = ?',
        (character_id,),
    ).fetchone()
    return row["styleMode"] if row and row["styleMode"] else DEFAULT_STYLE_MODE


def _style_mode_for_scene(conn, scene_id: str) -> str:
    row = conn.execute(
        'SELECT p.styleMode AS styleMode FROM "Scene" sc '
        'JOIN "Story" s ON sc.storyId = s.id JOIN "Project" p ON s.projectId = p.id WHERE sc.id = ?',
        (scene_id,),
    ).fetchone()
    return row["styleMode"] if row and row["styleMode"] else DEFAULT_STYLE_MODE


def _style_mode_for_shot(conn, shot_id: str) -> str:
    row = conn.execute(
        'SELECT p.styleMode AS styleMode FROM "Shot" sh '
        'JOIN "Scene" sc ON sh.sceneId = sc.id JOIN "Story" s ON sc.storyId = s.id '
        'JOIN "Project" p ON s.projectId = p.id WHERE sh.id = ?',
        (shot_id,),
    ).fetchone()
    return row["styleMode"] if row and row["styleMode"] else DEFAULT_STYLE_MODE


def _aspect_ratio_for_shot(conn, shot_id: str) -> str:
    """分镜出图/出视频用的比例，来自这一镜所在项目的 Project.aspectRatio——一部剧
    所有镜头必须用同一个比例才能在导出时拼到一起，所以这是项目级设置，不是每一镜
    单独选(跟 styleMode 同一个道理，见 _style_mode_for_shot)。"""
    row = conn.execute(
        'SELECT p.aspectRatio AS aspectRatio FROM "Shot" sh '
        'JOIN "Scene" sc ON sh.sceneId = sc.id JOIN "Story" s ON sc.storyId = s.id '
        'JOIN "Project" p ON s.projectId = p.id WHERE sh.id = ?',
        (shot_id,),
    ).fetchone()
    return row["aspectRatio"] if row and row["aspectRatio"] else DEFAULT_IMAGE_RATIO


class SeedreamImageProvider(ImageProvider):
    """文生图 / 图生图，参数取自 PIPELINE.md 第①②步的经验：
    - size 默认 "1440x2560"（9:16 竖屏，对应手机短视频比例），实际取这一镜所在项目的
      Project.aspectRatio(见 _aspect_ratio_for_shot)——一部剧所有镜头必须同一个比例
      才能在导出时拼到一起，所以是项目级设置，不是每一镜单独选。
    - reference_image_paths 有值时走图生图，本地文件转 data URI 直接传给 Ark（不需要额外上传拿在线URL）
    - prompt 统一加风格前缀，避免没有参考图时风格漂移成真人/写实
    """

    def generate_image(self, *, shot_id: str, prompt: str, reference_image_paths: list[str] | None = None) -> dict:
        with get_connection() as conn:
            settings = get_settings(conn)
            style_mode = _style_mode_for_shot(conn, shot_id)
            aspect_ratio = _aspect_ratio_for_shot(conn, shot_id)
        api_key = settings.get("arkApiKey")
        model = settings.get("arkImageModel") or ark_client.DEFAULT_SEEDREAM_MODEL
        base_url = settings.get("arkBaseUrl") or ark_client.ARK_BASE_URL
        size = IMAGE_RATIOS.get(aspect_ratio, IMAGE_RATIOS[DEFAULT_IMAGE_RATIO])["size"]

        reference_urls = None
        if reference_image_paths:
            reference_urls = [ark_client._file_to_data_uri(p) for p in reference_image_paths]

        image_url = ark_client.generate_image(
            api_key=api_key,
            prompt=_with_style(prompt, style_mode, settings.get("customStylePrefixes")),
            reference_image_urls=reference_urls,
            size=size,
            model=model,
            base_url=base_url,
        )

        dest = asset_dir(shot_id) / "image.png"
        ark_client.download_to_file(image_url, str(dest))

        return {"filePath": str(dest), "providerId": "seedream", "model": model}


CHARACTER_SHEET_PROMPT = (
    "角色设定图排版：正面全身 + 侧面全身 + 2-3个表情特写，纯色背景，方便后续当参考图用。"
    "角色：{name}。{appearance}"
)


def generate_character_reference(character_id: str, name: str, appearance: str | None = None) -> dict:
    """生成角色设定图（PIPELINE.md 第①步），纯文生图，不带参考图。
    appearance 是用户在角色库里填的外观描述/自定义提示词(Character.prompt)，比如
    "黑色长发，校服，温柔笑容"——留空就只靠角色名让模型自由发挥长相。
    """
    with get_connection() as conn:
        settings = get_settings(conn)
        style_mode = _style_mode_for_character(conn, character_id)
    api_key = settings.get("arkApiKey")
    model = settings.get("arkImageModel") or ark_client.DEFAULT_SEEDREAM_MODEL
    base_url = settings.get("arkBaseUrl") or ark_client.ARK_BASE_URL

    sheet_prompt = CHARACTER_SHEET_PROMPT.format(name=name, appearance=(appearance or "").strip())
    image_url = ark_client.generate_image(
        api_key=api_key,
        prompt=_with_style(sheet_prompt, style_mode, settings.get("customStylePrefixes")),
        size="2048x2048",
        model=model,
        base_url=base_url,
    )

    dest = character_dir(character_id) / "ref.png"
    ark_client.download_to_file(image_url, str(dest))

    return {"filePath": str(dest), "providerId": "seedream", "model": model}


SCENE_SHEET_PROMPT = (
    "场景环境定妆图：只画环境/背景本身，不出现任何角色人物，纯净的空间、光线、色调参考，"
    "方便后续同一场戏的每个镜头都照着这个环境和光线来画。场景描述：{summary}。"
)


def generate_scene_reference(
    scene_id: str, summary: str, reference_image_paths: list[str] | None = None
) -> dict:
    """生成场景环境母版图（多镜头一致性第一步：把背景/光线/色调先锁定，
    而不是让每个镜头各自发挥）。跟角色设定图同一套调用方式；传了 reference_image_paths
    就是图生图——比如有一张实拍的场地照片/参考画面，想让 Seedream 照着这个环境画，
    而不是纯靠文字描述让它自己发挥。
    """
    with get_connection() as conn:
        settings = get_settings(conn)
        style_mode = _style_mode_for_scene(conn, scene_id)
    api_key = settings.get("arkApiKey")
    model = settings.get("arkImageModel") or ark_client.DEFAULT_SEEDREAM_MODEL
    base_url = settings.get("arkBaseUrl") or ark_client.ARK_BASE_URL

    reference_urls = None
    if reference_image_paths:
        reference_urls = [ark_client._file_to_data_uri(p) for p in reference_image_paths]

    image_url = ark_client.generate_image(
        api_key=api_key,
        prompt=_with_style(
            SCENE_SHEET_PROMPT.format(summary=summary), style_mode, settings.get("customStylePrefixes")
        ),
        reference_image_urls=reference_urls,
        size="2048x2048",
        model=model,
        base_url=base_url,
    )

    dest = scene_dir(scene_id) / "ref.png"
    ark_client.download_to_file(image_url, str(dest))

    return {"filePath": str(dest), "providerId": "seedream", "model": model}


# 海报的画幅方向：跟出图风格前缀(STYLE_PREFIXES)不是一回事——那个是"整部剧的美术
# 风格"，这个纯粹是"这张图/这张海报的构图/画幅比例"。size 是 Ark 支持的分辨率字符串，
# 跟分镜生图同一个格式；ratio 是这个画幅对应的真实"宽:高"比例字符串，给 Seedance
# (视频)的 --ratio 参数用——size 是给图片用的具体像素，两者要对应同一个比例。
# 这份配置起初只给海报用(key 是 portrait/landscape)，现在短剧项目分镜、独立图生视频
# 也复用同一套"比例词典"，所以后面又追加了 3 个直接以比例命名的 key(9:16/1:1/4:3)；
# 已有数据库里存的 orientation="portrait"/"landscape" 完全不受影响，两个旧 key 原样保留，
# 只是新增，不是替换，不用担心老海报/文生图记录"重新生成"时比例跑掉。
# 构图提示语里都明确要求"不要出现任何文字"，标题/副标题是后面 poster_composer.py 用
# Pillow 叠上去的，不指望 Seedream 把中文字画对(这条提示语只有生成海报时会用到，
# 独立文生图/分镜出图/图生视频不会拼这句话)。
POSTER_ORIENTATIONS = {
    "portrait": {
        "label": "竖版 3:4",
        "size": "1536x2048",
        "ratio": "3:4",
        "composition": (
            "竖版宣传海报构图，电影感大场景/大氛围，画面留出足够的负空间"
            "（尤其是下方1/3区域）给后期叠加标题文字。"
        ),
    },
    "landscape": {
        "label": "横版 16:9",
        "size": "2048x1152",
        "ratio": "16:9",
        "composition": (
            "横版宣传图构图，适合做社交媒体封面/横幅，构图大气，下方或一侧留出干净区域"
            "给后期叠加标题文字。"
        ),
    },
    "9:16": {
        "label": "竖版 9:16（手机短视频）",
        "size": "1440x2560",
        "ratio": "9:16",
        "composition": (
            "9:16竖屏短视频构图，主体居中偏上，画面下方留出空间给后期叠加标题文字。"
        ),
    },
    "1:1": {
        "label": "方形 1:1",
        "size": "2048x2048",
        "ratio": "1:1",
        "composition": "方形构图，主体居中，四周留出干净的负空间给后期叠加标题文字。",
    },
    "4:3": {
        "label": "横版 4:3",
        "size": "2048x1536",
        "ratio": "4:3",
        "composition": "4:3横版构图，画面下方或一侧留出干净区域给后期叠加标题文字。",
    },
}
DEFAULT_POSTER_ORIENTATION = "portrait"
# 短剧项目分镜/独立图生视频复用这份配置时，用这两个更准确的别名，代码读起来别再是
# "POSTER_xxx"了；默认值特意保持跟分镜生图之前硬编码的尺寸(1440x2560/9:16)一致，
# 加这个字段之前建的老项目自愈迁移出来的 aspectRatio 也是这个默认值，行为完全不变。
IMAGE_RATIOS = POSTER_ORIENTATIONS
DEFAULT_IMAGE_RATIO = "9:16"

# 海报"类型"(医院海报/地陪翻译/科普知识/价格表/知识卡片……)不再写死在代码里——
# 类型本质上就是"一段预置的内容提示语 + 一种排版方式"，这两样现在都存在
# PosterTemplate 表里(见 app/routers/poster_templates.py)，用户能自己增删改，
# 不用每加一种新场景就改代码。db.py 的 _ensure_startup_migrations 会在这张表首次
# 建表时预置几条默认模版(医院海报/地陪翻译/医美科普/价格表/知识卡片)覆盖冷启动体验。
_POSTER_NO_TEXT_INSTRUCTION = "不要出现任何文字、字母、数字、水印、logo。"


def generate_poster_background(
    poster_id: str,
    orientation: str,
    content_prompt: str,
    style_mode: str = DEFAULT_STYLE_MODE,
    project_id: str | None = None,
    extra_prompt: str | None = None,
    reference_image_paths: list[str] | None = None,
) -> dict:
    """只生成海报的背景图，不含文字——标题/副标题/正文由 posters.py 调
    poster_composer.compose_poster 在这张背景图基础上叠加。海报是独立功能，
    style_mode 直接来自 Poster.styleMode(海报自己选的)，不再从某个 Project 上现查——
    project_id 现在只是可选的落盘位置参数，传了就存到那个项目的子目录下。

    orientation(竖版/横版)决定构图提示语和出图尺寸；content_prompt 是调用方已经解析好
    的内容提示语(来自选中的 PosterTemplate.promptText，或者用户没选模版时临时写的
    一次性提示词)，这里不再关心它具体是"医院"还是"价格表"还是别的什么类型。
    """
    orientation_cfg = POSTER_ORIENTATIONS.get(orientation, POSTER_ORIENTATIONS[DEFAULT_POSTER_ORIENTATION])
    with get_connection() as conn:
        settings = get_settings(conn)

    api_key = settings.get("arkApiKey")
    model = settings.get("arkImageModel") or ark_client.DEFAULT_SEEDREAM_MODEL
    base_url = settings.get("arkBaseUrl") or ark_client.ARK_BASE_URL

    prompt = f"{orientation_cfg['composition']} {(content_prompt or '').strip()} {_POSTER_NO_TEXT_INSTRUCTION}".strip()
    if extra_prompt and extra_prompt.strip():
        prompt = f"{prompt} {extra_prompt.strip()}"

    reference_urls = None
    if reference_image_paths:
        reference_urls = [ark_client._file_to_data_uri(p) for p in reference_image_paths]

    image_url = ark_client.generate_image(
        api_key=api_key,
        prompt=_with_style(prompt, style_mode, settings.get("customStylePrefixes")),
        reference_image_urls=reference_urls,
        size=orientation_cfg["size"],
        model=model,
        base_url=base_url,
    )

    dest = poster_dir(poster_id, project_id) / "background.png"
    ark_client.download_to_file(image_url, str(dest))

    return {"filePath": str(dest), "providerId": "seedream", "model": model}


def generate_standalone_image(
    image_id: str,
    prompt: str,
    style_mode: str = DEFAULT_STYLE_MODE,
    orientation: str = DEFAULT_POSTER_ORIENTATION,
    project_id: str | None = None,
    reference_image_paths: list[str] | None = None,
) -> dict:
    """独立文生图用：跟海报背景图共用画幅(orientation->size)配置，但不加"不要出现
    任何文字"的约束、生成结果也不会再被 Pillow 二次加工——用户想要的就是 AI 直接画出来
    的这张图，可能就是想要图里带字（比如海报草稿/表情包），不该强行禁止。
    reference_image_paths 可选：传了就是图生图（参考构图/风格），不传就是纯文生图。
    """
    orientation_cfg = POSTER_ORIENTATIONS.get(orientation, POSTER_ORIENTATIONS[DEFAULT_POSTER_ORIENTATION])
    with get_connection() as conn:
        settings = get_settings(conn)

    api_key = settings.get("arkApiKey")
    model = settings.get("arkImageModel") or ark_client.DEFAULT_SEEDREAM_MODEL
    base_url = settings.get("arkBaseUrl") or ark_client.ARK_BASE_URL

    reference_urls = None
    if reference_image_paths:
        reference_urls = [ark_client._file_to_data_uri(p) for p in reference_image_paths]

    image_url = ark_client.generate_image(
        api_key=api_key,
        prompt=_with_style(prompt, style_mode, settings.get("customStylePrefixes")),
        reference_image_urls=reference_urls,
        size=orientation_cfg["size"],
        model=model,
        base_url=base_url,
    )

    dest = text_image_dir(image_id, project_id) / "image.png"
    ark_client.download_to_file(image_url, str(dest))

    return {"filePath": str(dest), "providerId": "seedream", "model": model}
