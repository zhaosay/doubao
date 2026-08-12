# 短片生成流程说明:从文字剧本到成片素材

本文档记录"雨夜偶遇"这个8镜头短片 demo 的完整生成流程,以及每一步用到的工具、参数和踩过的坑,方便下次复用。

## 整体流程

```
剧本/分镜设计(scenes.json)
        │
        ▼
① 角色设定图(Seedream 文生图)
   林夏(女主) / 陈屿(男主) 各一张参考图
        │
        ▼
② 分镜静态图(Seedream 图生图)
   用①的角色图做 reference_images,逐镜头生成静态画面
        │
        ▼
③ 分镜视频(Seedance 图生视频)
   用②的静态图做首帧,配合运镜描述生成4秒动态视频
        │
        ▼
④ (未做)剪辑合成:把8段视频按 scenes.json 的 duration/dialogue 顺序拼接、配音、加字幕
```

---

## 第0步:剧本/分镜设计 —— `scenes.json`

每个镜头一个对象,字段含义:

| 字段 | 说明 |
|---|---|
| `shot_id` | 镜头编号(1-8) |
| `scene_type` | 景别(远景/中景/特写等),仅供人看,不直接喂给模型 |
| `draw_prompt` | 喂给 Seedream 的静态画面描述,含角色出场提示 `(角色参考：xxx.png)` |
| `motion_prompt` | 喂给 Seedance 的运镜/动态描述,只讲"动"的部分,不重复静态画面细节 |
| `dialogue` | 台词/旁白文字,本次没有接入语音合成,先留空跑图和视频 |
| `duration` | 期望时长(秒),仅供参考——Seedance 最短支持4秒,实际生成统一用了4秒 |

**关键设计**:一开始所有镜头都写的是同一个笼统的 `ref_char.png`,但故事里有两个角色(林夏、陈屿)。后来改成按角色拆分成 `ref_char_female.png` / `ref_char_male.png`,两人同框的镜头(5/7/8)就在 prompt 里同时带上两个文件名提示。这只是**文本提示**,真正起作用的是下一步传给 API 的 `reference_images` 参数。

---

## 第①步:角色设定图(Seedream 文生图)

用 `byted-ark-seedream-skill`,`mode: text-to-image`。

- Prompt 要求生成"角色设定图"排版:正面全身 + 侧面全身 + 2-3个表情特写,纯色背景,方便后续当参考图用。
- `size: "2K"`,`response_format: "png"`。
- 产出:`ref_char_female.png`(林夏)、`ref_char_male.png`(陈屿)。

## 第②步:分镜静态图(Seedream 图生图)

同一个 skill,`mode: image-to-image`。

- `reference_images`:传第①步生成图片的**在线 URL**(火山云存储链接,24小时有效)。单人镜头传1张,双人镜头传2张(女+男)。
- `size: "1440x2560"`(9:16 竖屏,对应手机短视频比例)。
- prompt 用 `draw_prompt` 原文,但**去掉了 `(角色参考：xxx.png)` 这种文件名提示**,避免模型把文件名当文字画到图里。
- 产出:`shot_01.png` ~ `shot_08.png`。

> 踩坑:`reference_images` 既可以传本地文件转的 base64,也可以传在线 URL。本地文件转 base64 在命令行传参时容易超过系统 `ARG_MAX`(约1MB),所以图生图这一步统一用了 Seedream 刚生成完时返回的在线 URL,不用本地文件。

## 第③步:分镜视频(Seedance 图生视频)

用 `byted-ark-seedance-skill`,`--image-file` 传第②步生成的本地图片路径(1张=首帧生视频),这一步**是本地文件路径**,skill 内部自己转 base64 上传,不需要在线 URL。

关键参数:
- `--duration 4`(Seedance 最短支持4秒,原剧本设计的2.5-3.5秒都要向上取整)
- `--ratio "9:16"`
- `--resolution "720p"`
- `--wait true`(前台死等结果,最长20分钟,而不是提交完就撒手)
- prompt 用 `motion_prompt`(只讲运镜/动态,不用重复画面静态描述,画面已经由首帧图决定)

### 模型选择的坑

Skill 默认会"智能路由"自动选模型,不需要手动传 `--model`。但这次账号套餐是 Ark Agent Plan 的 **Medium 套餐**,一开始所有 Seedance 模型(2.0/2.0-fast/2.0-mini/1.5-pro)全部返回:

```
404 UnsupportedModel: The requested model does not support the agent plan feature.
```

验证方法:用一个完全瞎编的模型名发请求,报错**一模一样**,证明不是"模型名选错了",而是**这个套餐等级下视频生成接口整体没开通**。

后来升级套餐后:
- `doubao-seedance-2.0`(标准版)✅ 可用
- `doubao-seedance-1.5-pro` 依然 404 —— 官方标注"即将下线,不支持新增接入",看来是该型号本身停止对外提供,与套餐等级无关。

**结论:这个账号以后固定用 `doubao-seedance-2.0`(skill 默认路由也会选它),不要再尝试 1.5-pro。**

### 服务不稳定的坑

创建视频任务的请求(`POST .../contents/generations/tasks`)偶发性地完全不响应(不是报错,是服务端一直不回包),表现为客户端超时报 `AbortError`。用 `curl` 加 `-w` 打时间戳验证过:DNS/建连/握手都是毫秒级,但服务器迟迟不返回响应头,即使是之前刚成功过的图片也会遇到同样问题——**这是 Ark 视频生成服务端偶发拥堵/不稳定,不是本地代码或参数问题**,重试等待是唯一办法,盲目改超时或换参数没用。

顺手把 `byted-ark-seedance-skill/scripts/seedance.js` 里 `createTask` 请求的客户端超时从默认60秒调到了300秒(原来60秒对于携带几MB图片的请求偏短,容易误判超时),这个改动在 skill 目录里,不影响项目文件。

---

## 本次产出清单

| 文件 | 说明 |
|---|---|
| `ref_char_female.png` | 女主角林夏设定图 |
| `ref_char_male.png` | 男主角陈屿设定图 |
| `shot_01.png` ~ `shot_08.png` | 8个镜头静态图 |
| `shot_01.mp4` `shot_02.mp4` `shot_05.mp4` | 已生成成功的镜头视频(其余5个待服务恢复后重跑) |
| `scenes.json` | 分镜脚本源数据 |

---

## 待办

1. 服务恢复正常后,补跑 `shot_03/04/06/07/08` 的视频(方法同第③步,图片都已就绪)。
2. 8段视频按 `scenes.json` 的顺序和 `dialogue` 剪辑合成,目前没有做这一步。
3. 台词配音(`dialogue` 字段)还没接,项目备忘里提到局域网 IndexTTS 服务地址 `http://10.39.64.13:7860`,可以作为下一步的配音方案。
