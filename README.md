# AI视频工作台（ai-manju-mvp）

用 Seedream(文生图/图生图) + Seedance(图生视频) + IndexTTS(配音) + 本机 claude
CLI(剧本生成)，把一句话故事简介做成竖屏短剧素材的本地桌面工具。Electron +
Vue3 渲染进程，FastAPI(Python) 做后端，SQLite(Prisma 管 schema/迁移) 存数据。

## 一键启动（推荐）

不想手动敲下面那几条命令的话，双击仓库根目录下对应系统的启动脚本，
会自动装 Node 依赖、跑数据库迁移、建 Python 虚拟环境并装依赖、最后拉起应用：

- **macOS**：双击 `启动.command`（内部调用 `start.sh`）。第一次双击如果被
  macOS 拦下说"未知开发者"，右键选"打开"确认一次即可。如果报
  `Operation not permitted`，通常是项目放在了 `~/Desktop`（桌面）、
  `~/Documents`、`~/Downloads` 这几个被系统隐私保护的目录下，
  终端没有访问权限——去"系统设置 → 隐私与安全性 → 完全磁盘访问权限"里把
  「终端」加进去，或者干脆把项目挪到别的目录（比如 `~/project`）。
- **Windows**：双击 `启动.bat`。需要提前装好
  [Node.js](https://nodejs.org/)（自带 npm）和
  [Python 3](https://www.python.org/)（安装时记得勾选
  "Add python.exe to PATH"，脚本会优先用 `py -3` 启动器，没有的话退回 `python`）。

两个脚本都是"能跳过就跳过"的幂等逻辑：`node_modules`/`.venv` 已经存在就不会
重装，可以放心重复运行；失败时会在窗口里打印报错原因，按任意键再关闭。

## 首次运行前的准备（手动，不想用一键脚本的话）

1. **Node 依赖**（仓库根目录）：
   ```
   npm install
   ```
2. **数据库**（在 `apps/desktop` 下，Prisma 是 schema/迁移的唯一来源）：
   ```
   cd apps/desktop
   npx prisma migrate dev
   ```
3. **Python 后端虚拟环境**（在 `apps/ai-service` 下）：
   ```
   cd apps/ai-service
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   Electron 主进程启动时会自动用 `apps/ai-service/.venv` 里的 python 拉起
   FastAPI(`uvicorn app.main:app`)，端口固定 8000。如果 `.venv` 不存在会退回
   系统的 `python3`（大概率跑不起来，因为没装 fastapi/uvicorn），所以这一步
   不能跳过。
   > **注意**：Python 虚拟环境内部会把创建时的绝对路径写死进去，不能整个
   > 文件夹挪动/复制到别的路径（包括跨系统同步网盘）。如果项目目录挪动过，
   > 删掉 `apps/ai-service/.venv` 重新建一个就好。

## 运行

```
npm run dev:desktop
```

Electron 窗口打开后，顶部会显示"后端 ok/error"——如果一直是 error，看终端
里 `[ai-service]` 开头的日志找原因（通常是 venv 没建好，或者端口 8000 被占）。

## 用法

1. **设置页**填两个东西：
   - 火山方舟(Ark) API Key —— Seedream/Seedance 用，去 <https://console.volcengine.com/ark> 申请
   - IndexTTS 服务地址 —— 默认填的是 PIPELINE.md 里记的局域网地址
     `http://10.39.64.13:7860`，按需改成你自己的
2. **项目**页新建一个项目，写一句话故事简介
3. 进入项目详情，点「生成剧本」——这一步会在**本机终端**跑
   `claude -p ...`，需要你的机器上已经装好 Claude Code 并且登录过
   （跑 `claude` 能正常对话就说明没问题）
4. 剧本生成完会列出分镜，每个分镜可以改画面描述/运镜描述/台词，改完点
   一下别的地方会自动保存(PATCH)
5. 每个分镜三个生成按钮：
   - **生成图片**：文生图/图生图，双人同框的镜头在"参考图本地路径"里填两张
     角色设定图路径（逗号分隔）
   - **生成视频**：图生视频，默认自动用上面刚生成的图片当首帧，不用填
   - **生成配音**：调 IndexTTS，需要那台服务真的在你能访问的网络里
6. 全部分镜视频都生成完之后，页面底部「导出成片」按 scenes 顺序拼接 + 烧字幕
7. **海报**是独立的一级功能（侧栏「新建海报」/「海报列表」），不用先建视频
   项目。创建时选朝向（竖版/横版）+ 一个类型模版（医院海报/地陪翻译/医美科普/
   价格表/知识卡片，预置了这几个，也可以自己新增/删除），或者不选模版自己写
   一次性提示词。价格表/知识卡片这类需要精确文字内容的类型，排版方式是"多行
   正文"——每行一条，价格类可以写"项目名|价格"，价格会自动右对齐；其它类型是
   常规的"标题+副标题"。AI 只画不含文字的背景图，所有文字都是程序真实渲染叠
   上去的——不依赖 AI 把中文字画对。第一次用之前建议去设置页确认下"海报字体"
   能不能自动找到（见下面系统依赖里的说明），不然生成会报错。

## 打包成 Windows 安装包

不想让最终用户自己装 Node/Python/建 venv，可以打一个独立的 Windows 安装包出来，
双击安装、不需要目标机器上有 Python 环境：

```
cd apps/desktop
npm run build:win
```

这一步做了什么：
- `apps/desktop/resources/python-win/` 是内置的 Windows 版 Python 运行时（官方
  embeddable 发行版 + 提前下载好的 win_amd64 依赖包），打包后 ai-service 用这个
  跑，不依赖用户机器装没装 Python。
- `apps/desktop/resources/ai-service/` 是 ai-service 源码的一份拷贝（不含
  `.venv`），跟 `python-win` 配套使用。
- `apps/desktop/resources/seed-db/seed.db` 是一份已经跑完所有数据库迁移、但没有
  任何用户数据的"种子数据库"，首次启动会自动拷贝到系统的应用数据目录
  （`%APPDATA%\ai-manju-mvp\app.db`），后续版本加表加字段走 ai-service 自带的
  自愈迁移逻辑原地升级，不需要用户重新安装。
- 生成的数据库和生成产物（图片/视频/海报）都存在 `%APPDATA%\ai-manju-mvp\` 下，
  不在安装目录里，卸载重装不会丢用户数据。

打出来的安装包在 `apps/desktop/dist/` 下（`*.exe` 是 NSIS 安装程序，双击安装；
如果目标环境跑不出 NSIS 安装程序，也会同时产出一个 `*-win.zip`，解压后直接
双击里面的 `.exe` 就能用，只是没有开始菜单快捷方式和卸载程序）。

> **注意**：如果在 macOS/Linux 上跑 `npm run build:win`（交叉编译 Windows 包），
> 电脑上没装 Wine 的话，NSIS 安装程序这一步有可能失败（`.zip` 产物不受影响，
> 一定能生成）——直接在 Windows 机器上跑这条命令就不会有这个问题，最稳妥。
> 首次打包前记得先在仓库根目录跑一次 `npm install`。

> **限制**：这套打包目前只覆盖了 ai-service 自身的 Python 依赖，`ffmpeg`（导出
> 成片要用）没有一起打进去，用户还是要自己装（见下面「系统依赖」）——后续要做成
> 真正意义上的"零依赖安装包"可以再把 ffmpeg 的 Windows 版可执行文件也塞进
> `resources/` 里，让代码优先找这个路径。

## 系统依赖

- **ffmpeg / ffprobe**：导出成片（拼接视频+烧字幕）会调本机命令行的 `ffmpeg`/
  `ffprobe`，需要提前装好并加进 PATH（`ffmpeg -version` 能跑通就行）。
  macOS 可以 `brew install ffmpeg`；Windows 去
  [ffmpeg.org](https://ffmpeg.org/download.html) 下载，解压后把 `bin` 目录
  加进系统环境变量 PATH。这一步跟 Node/Python 依赖无关，一键启动脚本不会
  帮你装这个。
- **海报字体**：海报标题/副标题是用 Pillow 代码渲染的，需要一个真的支持中文
  字形的字体文件。大多数装了中文系统的 macOS/Windows 机器会自动找到系统自带
  的字体（PingFang / 微软雅黑），不用额外配置；如果生成海报报"找不到可用的
  中文字体文件"，去「设置」页的「海报字体」里手动填一个字体文件路径
  （`.ttf`/`.ttc`/`.otf` 都行）。

## 已知限制 / 踩坑记录

- **Ark 接口字段名是最佳猜测**：官方 REST 文档公开的示例不全，
  `app/ai-service/app/services/ark_client.py` 里的请求体是照着能找到的文档
  和 `PIPELINE.md` 里记录的实际调用经验拼的。如果调用报 400/参数错误，
  报错原文会原样显示在分镜卡片上，照着改那个文件就行，不会是静默失败。
- **IndexTTS 是局域网服务**：只有当运行 ai-service 的机器能访问那个地址时
  配音才会成功；云端/沙盒环境天然连不到。
- **剧本生成依赖本机 claude CLI**：必须是能交互登录、能在终端正常用的
  `claude` 命令，ai-service 只是 subprocess 调用它。
- **数据库文件放在会被同步的目录里可能不稳定**：如果 `data/app.db` 所在的
  目录开了 iCloud Drive / OneDrive 之类的云同步，SQLite 的文件锁在这类目录
  下有概率出问题（表现为 "disk I/O error"）。如果你的项目目录在 Desktop 下
  且开了"苹果账户 → iCloud → 桌面与文档"同步，建议要么关掉这个文件夹的同步，
  要么后续把 `data/` 迁到不被同步的位置。
- 开发过程中在 `data/` 目录留下了几个 `_old_*` 开头的临时文件（测试产生的
  db 备份，不影响运行），手动删掉就行。

## 目录结构

- `apps/desktop` —— Electron + Vue3 桌面壳
- `apps/ai-service` —— FastAPI 后端，业务逻辑和三个 Provider(Seedream/
  Seedance/IndexTTS)都在这
- `packages/shared` —— 渲染进程和后端约定的类型/常量
- `output/` —— 所有生成产物(图片/视频/配音/导出成片)，不进 git
- `PIPELINE.md` —— 最早手动跑通8镜头demo(雨夜偶遇)的完整记录，里面的踩坑
  经验(模型选型、超时、参数传法)是上面 provider 代码的依据
