import { ChildProcess, spawn } from 'child_process'
import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import { autoUpdater } from 'electron-updater'
import { copyFileSync, existsSync, mkdirSync, readFileSync } from 'fs'
import { delimiter, extname, join } from 'path'

// package.json 的 name 是带 scope 的 "@ai-manju/desktop"——Electron 默认拿它来算
// app.getPath('userData') 之类的路径，斜杠在文件名里不安全，行为也因平台而异。
// 显式定死一个文件系统安全的名字，保证打包后 userData 目录（数据库/生成产物存放处）
// 稳定可预期，不用赌 Electron 对 scoped 包名的兜底行为。
app.setName('ai-manju-mvp')

const AI_SERVICE_PORT = 8000
const RELEASES_URL = 'https://github.com/zhaosay/doubao/releases/latest'

let aiServiceProcess: ChildProcess | null = null
let updaterReady = false
let updateDownloaded = false
let startupUpdateCheckScheduled = false
let updateCheckRunning = false
let updateDownloadRunning = false
let updateInstallPromptShown = false

interface AppUpdateStatus {
  currentVersion: string
  packaged: boolean
  state: 'idle' | 'checking' | 'available' | 'not-available' | 'downloading' | 'downloaded' | 'error'
  message: string
  latestVersion?: string
  percent?: number
}

let appUpdateStatus: AppUpdateStatus = {
  currentVersion: app.getVersion(),
  packaged: app.isPackaged,
  state: 'idle',
  message: app.isPackaged ? '启动后会自动检查 GitHub Release 更新' : '开发模式不支持自动更新'
}

function getUpdateErrorMessage(action: string, err: unknown): string {
  const detail = err instanceof Error ? err.message : String(err)
  return `${action}失败：${detail}。请打开 Release 手动下载。`
}

function broadcastUpdateStatus(): void {
  for (const win of BrowserWindow.getAllWindows()) {
    win.webContents.send('app-update-status', appUpdateStatus)
  }
}

function setUpdateStatus(patch: Partial<AppUpdateStatus>): AppUpdateStatus {
  appUpdateStatus = { ...appUpdateStatus, currentVersion: app.getVersion(), packaged: app.isPackaged, ...patch }
  broadcastUpdateStatus()
  return appUpdateStatus
}

function setupAutoUpdater(): void {
  autoUpdater.autoDownload = false
  autoUpdater.autoInstallOnAppQuit = false
  autoUpdater.setFeedURL({
    provider: 'github',
    owner: 'zhaosay',
    repo: 'doubao',
    releaseType: 'release'
  })

  autoUpdater.on('checking-for-update', () => {
    setUpdateStatus({ state: 'checking', message: '正在检查 GitHub Release...' })
  })
  autoUpdater.on('update-available', (info) => {
    updateDownloaded = false
    updateInstallPromptShown = false
    setUpdateStatus({
      state: 'available',
      latestVersion: info.version,
      message: `发现新版本 ${info.version}，正在自动下载`
    })
    void downloadAppUpdate(true)
  })
  autoUpdater.on('update-not-available', (info) => {
    setUpdateStatus({
      state: 'not-available',
      latestVersion: info.version,
      message: `当前已是最新版本 ${app.getVersion()}`
    })
  })
  autoUpdater.on('download-progress', (progress) => {
    setUpdateStatus({
      state: 'downloading',
      percent: progress.percent,
      message: `正在下载更新 ${Math.round(progress.percent)}%`
    })
  })
  autoUpdater.on('update-downloaded', (info) => {
    updateDownloaded = true
    setUpdateStatus({
      state: 'downloaded',
      latestVersion: info.version,
      percent: 100,
      message: `版本 ${info.version} 已下载，重启后安装`
    })
    void promptInstallDownloadedUpdate(info.version)
  })
  autoUpdater.on('error', (err) => {
    setUpdateStatus({
      state: 'error',
      message: getUpdateErrorMessage('更新', err)
    })
  })

  updaterReady = true
}

async function promptInstallDownloadedUpdate(version?: string): Promise<void> {
  if (updateInstallPromptShown) return
  updateInstallPromptShown = true

  const focusedWindow = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
  const options = {
    type: 'info' as const,
    buttons: ['立即重启安装', '稍后'],
    defaultId: 0,
    cancelId: 1,
    title: '更新已下载',
    message: version ? `新版本 ${version} 已下载完成` : '新版本已下载完成',
    detail: '重启应用后会自动安装更新。'
  }
  const result = focusedWindow
    ? await dialog.showMessageBox(focusedWindow, options)
    : await dialog.showMessageBox(options)

  if (result.response === 0) {
    autoUpdater.quitAndInstall(false, true)
  }
}

async function checkForAppUpdate(manual = false): Promise<AppUpdateStatus> {
  if (!app.isPackaged) {
    return setUpdateStatus({ state: 'error', message: '开发模式不支持自动更新，请打包后测试' })
  }
  if (updateCheckRunning || updateDownloadRunning) return appUpdateStatus
  if (!updaterReady) setupAutoUpdater()

  updateCheckRunning = true
  if (manual) setUpdateStatus({ state: 'checking', message: '正在检查 GitHub Release...' })
  try {
    await autoUpdater.checkForUpdates()
    return appUpdateStatus
  } catch (err) {
    return setUpdateStatus({
      state: 'error',
      message: getUpdateErrorMessage('检查更新', err)
    })
  } finally {
    updateCheckRunning = false
  }
}

async function downloadAppUpdate(auto = false): Promise<AppUpdateStatus> {
  if (!app.isPackaged) {
    return setUpdateStatus({ state: 'error', message: '开发模式不支持自动更新，请打包后测试' })
  }
  if (updateDownloadRunning) return appUpdateStatus
  if (!updaterReady) setupAutoUpdater()

  updateDownloadRunning = true
  setUpdateStatus({
    state: 'downloading',
    percent: 0,
    message: auto ? '发现新版本，正在自动下载 0%' : '正在下载更新 0%'
  })
  try {
    await autoUpdater.downloadUpdate()
    return appUpdateStatus
  } catch (err) {
    return setUpdateStatus({
      state: 'error',
      message: getUpdateErrorMessage('下载更新', err)
    })
  } finally {
    updateDownloadRunning = false
  }
}

function scheduleStartupUpdateCheck(): void {
  if (!app.isPackaged || startupUpdateCheckScheduled) return
  startupUpdateCheckScheduled = true
  setTimeout(() => {
    void checkForAppUpdate(false)
  }, 4000)
}

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

function withWindowsCliPath(env: Record<string, string>): Record<string, string> {
  if (process.platform !== 'win32') return env

  const candidateDirs = [
    process.env.APPDATA ? join(process.env.APPDATA, 'npm') : '',
    process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, 'Programs', 'nodejs') : '',
    process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, 'Volta', 'bin') : '',
    process.env.ProgramFiles ? join(process.env.ProgramFiles, 'nodejs') : '',
    process.env['ProgramFiles(x86)'] ? join(process.env['ProgramFiles(x86)'], 'nodejs') : ''
  ].filter(Boolean)

  return {
    ...env,
    PATH: [...candidateDirs, process.env.PATH ?? ''].join(delimiter)
  }
}

function resolveAiServiceRuntime(): AiServiceRuntime {
  // 打包模式：ai-service 源码和种子数据库都来自安装包内的 resources，用户数据放到
  // app.getPath('userData')，避免写安装目录。Windows 额外内置了 python-win；macOS
  // 暂时使用用户机器上的 python3，因此需要先安装 requirements.txt 里的 Python 依赖。
  if (app.isPackaged) {
    const aiServiceDir = join(process.resourcesPath, 'ai-service')
    const pythonRuntimeDir = join(process.resourcesPath, 'python-win')
    const pythonBin = process.platform === 'win32'
      ? join(pythonRuntimeDir, 'python.exe')
      : resolvePythonBin(aiServiceDir)
    const dbPath = join(app.getPath('userData'), 'app.db')
    const outputRoot = join(app.getPath('userData'), 'output')
    const extraEnv: Record<string, string> = {
      AI_MANJU_DB_PATH: dbPath,
      AI_MANJU_OUTPUT_ROOT: outputRoot
    }

    if (process.platform === 'win32') {
      const caBundlePath = join(pythonRuntimeDir, 'Lib', 'site-packages', 'certifi', 'cacert.pem')
      if (existsSync(caBundlePath)) {
        extraEnv.SSL_CERT_FILE = caBundlePath
        extraEnv.REQUESTS_CA_BUNDLE = caBundlePath
      } else {
        console.error(`[ai-service] 找不到 TLS CA 证书: ${caBundlePath}`)
      }
    }

    ensureUserDataDb(dbPath)
    return { pythonBin, aiServiceDir, extraEnv: withWindowsCliPath(extraEnv) }
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

function createWindow(): BrowserWindow {
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

  return win
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

// 读本地图片文件转成 data URI，pick-image-file(刚选完的那张)和 read-image-preview
// (回显已经存在的路径，比如上次生成/上次保存的参考图)共用同一份逻辑。这里改成
// 失败就抛异常(不在这里吞掉)，因为两个调用方对"要不要告诉用户"的需求不一样：
// pick-image-file 是用户刚点了"选择文件"这个主动操作，读失败必须给出看得懂的原因，
// 不然就是"路径填上了但缩略图消失"这种莫名其妙的体验(实际踩过的例子见下面
// describeReadImageError)；read-image-preview 是页面刷新/重新打开时后台批量
// 补一遍已存路径的预览图，失败了静默返回 null 就行，不用每张图都弹提示。
function readImageAsDataUrl(filePath: string): string {
  const mime = IMAGE_MIME_BY_EXT[extname(filePath).toLowerCase()] ?? 'application/octet-stream'
  const base64 = readFileSync(filePath).toString('base64')
  return `data:${mime};base64,${base64}`
}

// 把 Node 的原始报错转成用户能看懂、能照着做的提示，按平台给不同建议——
// macOS 上"桌面/文稿/下载/照片"这几个目录有额外的隐私保护，没有拿到"完全磁盘
// 访问权限"的未签名 App 读取里面的文件会报 EPERM，是实测踩到的真实案例
// (选完图，输入框里路径文字有，但缩略图读不出来)；Windows 上对应的常见坑
// 换成了 OneDrive"仅联机"占位文件(本地根本没有实际字节，读会报错)和杀毒软件/
// 文件占用拦截，报错关键字不一样，给不同的排查建议。
function describeReadImageError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err)
  if (process.platform === 'darwin' && /eperm|not permitted/i.test(message)) {
    return `${message}(可能是 macOS 隐私权限限制：桌面/文稿/下载/照片这几个目录，未获得"完全磁盘访问权限"的 App 读取里面的文件会被系统拦下。可以把图片挪到其他文件夹再选，或者去 系统设置 > 隐私与安全性 > 完全磁盘访问权限 里给这个 App 授权。)`
  }
  if (process.platform === 'win32' && /ebusy|eperm|access is denied/i.test(message)) {
    return `${message}(可能是文件被其他程序占用、被杀毒软件拦截，或者是 OneDrive"仅联机"文件还没同步到本地——右键这个文件选"始终保留在此设备上"再试一次。)`
  }
  return message
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
    return { path: filePath, dataUrl: readImageAsDataUrl(filePath), error: null }
  } catch (err) {
    console.error(`[pick-image-file] 读文件失败(${filePath}):`, err)
    return { path: filePath, dataUrl: null, error: `读取图片失败：${describeReadImageError(err)}` }
  }
})

// 上面那个只在"刚选完文件"那一刻返回预览图——之前的问题是：参考图路径存进后端之后，
// 页面刷新/重新打开项目/切到文生图列表页，渲染进程手里只剩下一串本地绝对路径字符串，
// 没有配套的 data URI，预览区就是空的，看起来像"上传的图片不显示"。这个接口给已经
// 存在的路径补一次回显：传路径进来，读文件转 data URI 回去，跟 pick-image-file
// 是同一份读取逻辑，只是不弹选择框。路径不存在/读取失败时返回 null，渲染进程会
// 显示"预览不可用"而不是报错崩掉。
ipcMain.handle('read-image-preview', async (_event, filePath: string) => {
  if (!filePath || !existsSync(filePath)) return null
  try {
    return readImageAsDataUrl(filePath)
  } catch (err) {
    console.error(`[read-image-preview] 读文件失败(${filePath}):`, err)
    return null
  }
})

ipcMain.handle('app-update-status', async () => appUpdateStatus)

ipcMain.handle('app-update-check', async () => checkForAppUpdate(true))

ipcMain.handle('app-update-download', async () => downloadAppUpdate(false))

ipcMain.handle('app-update-install', async () => {
  if (!updateDownloaded) {
    return setUpdateStatus({ state: 'error', message: '更新包还没下载完成' })
  }
  setImmediate(() => autoUpdater.quitAndInstall(false, true))
  return appUpdateStatus
})

ipcMain.handle('app-update-open-release', async () => {
  await shell.openExternal(RELEASES_URL)
  return true
})

app.whenReady().then(() => {
  setupAutoUpdater()
  startAiService()
  const win = createWindow()
  win.webContents.once('did-finish-load', scheduleStartupUpdateCheck)

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
