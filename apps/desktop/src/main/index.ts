import { ChildProcess, spawn } from 'child_process'
import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import { copyFileSync, existsSync, mkdirSync, readFileSync } from 'fs'
import { extname, join } from 'path'

// package.json 的 name 是带 scope 的 "@ai-manju/desktop"——Electron 默认拿它来算
// app.getPath('userData') 之类的路径，斜杠在文件名里不安全，行为也因平台而异。
// 显式定死一个文件系统安全的名字，保证打包后 userData 目录（数据库/生成产物存放处）
// 稳定可预期，不用赌 Electron 对 scoped 包名的兜底行为。
app.setName('ai-manju-mvp')

const AI_SERVICE_PORT = 8000

let aiServiceProcess: ChildProcess | null = null

function resolveAiServiceDir(): string {
  // out/main -> apps/desktop/out/main，往上三级到 apps/，再进 ai-service/
  return join(__dirname, '../../../ai-service')
}

function resolvePythonBin(aiServiceDir: string): string {
  const isWindows = process.platform === 'win32'
  const venvPython = join(aiServiceDir, '.venv', isWindows ? 'Scripts/python.exe' : 'bin/python')
  return existsSync(venvPython) ? venvPython : 'python3'
}

interface AiServiceRuntime {
  pythonBin: string
  aiServiceDir: string
  // 打包成安装包之后传给 ai-service 子进程的环境变量覆盖：数据库文件、生成产物目录
  // 都不能再放在安装目录里（Windows 安装目录通常没写权限，卸载重装也不该丢用户
  // 数据），改成放 app.getPath('userData') 下。开发模式这两个都是 undefined，
  // ai-service（db.py/paths.py）会退回原来仓库相对路径的行为，不受影响。
  extraEnv: Record<string, string>
}

// 首次启动时，把打包进安装包的"种子数据库"(已经跑完所有 Prisma 迁移、只预置了
// 默认海报模版、没有任何用户数据的一份干净 sqlite 文件)拷贝到 userData 目录下，
// 后续启动检测到文件已存在就不会重复拷贝——不会覆盖用户已经生成的数据。
// 后面产品升级如果加了新表/新列，靠 ai-service 的 db.py 自愈迁移逻辑在启动时
// 原地升级，不需要在这里额外处理"升级种子库"的情况。
function ensureUserDataDb(dbPath: string): void {
  if (existsSync(dbPath)) return
  const seedDbPath = join(process.resourcesPath, 'seed-db', 'seed.db')
  if (!existsSync(seedDbPath)) {
    console.error(`[ai-service] 找不到种子数据库: ${seedDbPath}`)
    return
  }
  mkdirSync(join(dbPath, '..'), { recursive: true })
  copyFileSync(seedDbPath, dbPath)
}

function resolveAiServiceRuntime(): AiServiceRuntime {
  // 打包模式：ai-service 源码和种子数据库都来自安装包内的 resources，用户数据放到
  // app.getPath('userData')，避免写安装目录。Windows 额外内置了 python-win；macOS
  // 暂时使用用户机器上的 python3，因此需要先安装 requirements.txt 里的 Python 依赖。
  if (app.isPackaged) {
    const aiServiceDir = join(process.resourcesPath, 'ai-service')
    const pythonBin = process.platform === 'win32'
      ? join(process.resourcesPath, 'python-win', 'python.exe')
      : resolvePythonBin(aiServiceDir)
    const dbPath = join(app.getPath('userData'), 'app.db')
    const outputRoot = join(app.getPath('userData'), 'output')
    ensureUserDataDb(dbPath)
    return { pythonBin, aiServiceDir, extraEnv: { AI_MANJU_DB_PATH: dbPath, AI_MANJU_OUTPUT_ROOT: outputRoot } }
  }

  // 开发模式：走原来的仓库相对路径 + venv，行为完全不变。
  const aiServiceDir = resolveAiServiceDir()
  return { pythonBin: resolvePythonBin(aiServiceDir), aiServiceDir, extraEnv: {} }
}

/**
 * 拉起本地 FastAPI 后端(ai-service)。
 * - 开发模式：用 apps/ai-service/.venv 里的 python（README 里要求先建好这个 venv）
 * - Windows 打包模式：用内置的 python-win 运行时，数据库/生成产物存 userData 目录
 * - 端口固定 8000，和 renderer 里写死的 apiBaseUrl 对应
 * - 子进程输出转发到 electron 的 stdout，方便看后端日志/报错
 */
function startAiService(): void {
  const { pythonBin, aiServiceDir, extraEnv } = resolveAiServiceRuntime()
  if (!existsSync(aiServiceDir)) {
    console.error(`[ai-service] 找不到目录: ${aiServiceDir}，跳过启动后端`)
    return
  }

  aiServiceProcess = spawn(
    pythonBin,
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(AI_SERVICE_PORT)],
    { cwd: aiServiceDir, stdio: 'pipe', env: { ...process.env, ...extraEnv } }
  )

  aiServiceProcess.stdout?.on('data', (chunk) => process.stdout.write(`[ai-service] ${chunk}`))
  aiServiceProcess.stderr?.on('data', (chunk) => process.stderr.write(`[ai-service] ${chunk}`))
  aiServiceProcess.on('exit', (code) => {
    console.log(`[ai-service] 进程退出，code=${code}`)
    aiServiceProcess = null
  })
  aiServiceProcess.on('error', (err) => {
    console.error('[ai-service] 启动失败:', err)
  })
}

function stopAiService(): void {
  if (aiServiceProcess && !aiServiceProcess.killed) {
    aiServiceProcess.kill()
    aiServiceProcess = null
  }
}

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: false,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false
    }
  })

  win.once('ready-to-show', () => win.show())

  const rendererUrl = process.env['ELECTRON_RENDERER_URL']
  if (rendererUrl) {
    win.loadURL(rendererUrl)
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

// 双击生成的图片/视频/音频缩略图，用系统默认程序打开原文件（跟 Finder 里双击一样）。
// 渲染进程拿到的是 http://127.0.0.1:8000/files/... 的 URL，没法直接用来"打开原文件"，
// 所以传本地绝对路径（Asset.filePath）过来，走主进程的 shell.openPath——渲染进程
// 出于安全限制不能直接碰文件系统，这也是唯一一处需要用 IPC 的地方。
ipcMain.handle('open-path', async (_event, filePath: string) => {
  if (!filePath) return '未提供文件路径'
  return shell.openPath(filePath)
})

const IMAGE_MIME_BY_EXT: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp'
}

// 参考图"上传"本质是"选一个本机已有的文件"：原生系统选择框选完直接拿到绝对路径，
// 不用真的把文件字节传进渲染进程再落盘一份。用户自己在系统对话框里选，不算渲染进程
// 拿到了额外的文件系统权限，跟 open-path 一样是"用户主动触发的窄接口"。
//
// 顺带把文件读出来转成 data URI 一起返回，给渲染进程当预览图用——选完图之后渲染进程
// 只有一个本地绝对路径字符串，没法直接 <img src> 出来：页面是 http://localhost(dev)
// 或 file://(打包后)源，Chromium 不允许跨源直接加载任意 file:// 路径当图片资源
// (会报 "Not allowed to load local resource")。读成 data URI 就没有这个限制，
// 反正也只是一张参考图，不大，不用为了省这几十 KB 内存再单独开一条"渲染进程读图"的
// 通道，直接跟着选择结果一起传回来最简单。
ipcMain.handle('pick-image-file', async () => {
  const result = await dialog.showOpenDialog({
    title: '选择参考图',
    properties: ['openFile'],
    filters: [{ name: '图片', extensions: ['png', 'jpg', 'jpeg', 'webp', 'bmp'] }]
  })
  if (result.canceled || result.filePaths.length === 0) return null
  const filePath = result.filePaths[0]
  try {
    const mime = IMAGE_MIME_BY_EXT[extname(filePath).toLowerCase()] ?? 'application/octet-stream'
    const base64 = readFileSync(filePath).toString('base64')
    return { path: filePath, dataUrl: `data:${mime};base64,${base64}` }
  } catch (err) {
    // 读文件失败(权限/文件太大之类)不阻塞流程，路径照样有用，只是没有预览图。
    console.error('[pick-image-file] 读文件生成预览失败:', err)
    return { path: filePath, dataUrl: null }
  }
})

app.whenReady().then(() => {
  startAiService()
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  stopAiService()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  stopAiService()
})
