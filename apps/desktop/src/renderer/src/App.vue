<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  BookOpen,
  Clapperboard,
  FileVideo2,
  Images,
  ListVideo,
  Settings,
  Wand2
} from '@lucide/vue'
import { CINEMATOGRAPHY_MANUAL, type ManualEntry } from './cinematography'
import appLogoUrl from './assets/logo.png'

interface AiManjuBridge {
  apiBaseUrl: string
  openPath?: (filePath: string) => Promise<string>
  pickImageFile?: () => Promise<{ path: string; dataUrl: string | null; error?: string | null } | null>
  readImagePreview?: (filePath: string) => Promise<string | null>
  getUpdateStatus?: () => Promise<AppUpdateStatus>
  checkForUpdates?: () => Promise<AppUpdateStatus>
  downloadUpdate?: () => Promise<AppUpdateStatus>
  installUpdate?: () => Promise<AppUpdateStatus>
  openLatestRelease?: () => Promise<boolean>
  onUpdateStatus?: (callback: (status: AppUpdateStatus) => void) => () => void
}

interface AppUpdateStatus {
  currentVersion: string
  packaged: boolean
  state: 'idle' | 'checking' | 'available' | 'not-available' | 'downloading' | 'downloaded' | 'error'
  message: string
  latestVersion?: string
  percent?: number
}

const aiManjuBridge = (window as unknown as { aiManju?: AiManjuBridge }).aiManju
const apiBaseUrl = aiManjuBridge?.apiBaseUrl ?? 'http://127.0.0.1:8000'

// V1 界面已经下线，不再需要切换/记住用户选择——固定用 V2。之前这里读 localStorage
// 判断要不要沿用用户上次选的版本，现在只保留这一个值，`.ui-v1` 相关的旧样式/开关
// 逻辑不再触发，但 CSS 里 V1 的基础样式规则没有删（V2 的 `.ui-v2` 选择器是叠在这些
// 基础规则上面的覆盖层，不是完全独立的一套样式，删了 V1 基础规则会连带把 V2 弄坏）。
type UiVersion = 'v2'
const uiVersion = ref<UiVersion>('v2')

// 双击缩略图用系统默认程序打开原文件（跟 Finder 里双击一样）。openPath 走 Electron
// IPC，只有跑在真正的 Electron 壳里才有；纯浏览器打开这个页面调试时 aiManjuBridge
// 会是 undefined，直接跳过不报错。shell.openPath 成功时 resolve 空字符串，失败时
// resolve 一段错误文字（不是 throw），所以这里要手动判断返回值决定要不要报错。
const openFileError = ref<string | null>(null)
const updateStatus = ref<AppUpdateStatus>({
  currentVersion: '0.1.8',
  packaged: false,
  state: 'idle',
  message: '正在读取更新状态...'
})
let removeUpdateStatusListener: (() => void) | null = null

const updateActionDisabled = computed(() => {
  return updateStatus.value.state === 'checking' || updateStatus.value.state === 'downloading'
})
const updatePrimaryActionLabel = computed(() => {
  if (updateStatus.value.state === 'checking') return '检查中…'
  if (updateStatus.value.state === 'downloading') return `下载中 ${Math.round(updateStatus.value.percent ?? 0)}%`
  if (updateStatus.value.state === 'available') return '下载更新'
  if (updateStatus.value.state === 'downloaded') return '重启安装'
  return '检查更新'
})

async function refreshUpdateStatus(): Promise<void> {
  if (!aiManjuBridge?.getUpdateStatus) {
    updateStatus.value = {
      currentVersion: 'dev',
      packaged: false,
      state: 'error',
      message: '当前环境不支持应用内更新'
    }
    return
  }
  updateStatus.value = await aiManjuBridge.getUpdateStatus()
}

async function checkForAppUpdate(): Promise<void> {
  if (!aiManjuBridge?.checkForUpdates) return
  updateStatus.value = await aiManjuBridge.checkForUpdates()
}

async function downloadAppUpdate(): Promise<void> {
  if (!aiManjuBridge?.downloadUpdate) return
  updateStatus.value = await aiManjuBridge.downloadUpdate()
}

async function installAppUpdate(): Promise<void> {
  if (!aiManjuBridge?.installUpdate) return
  updateStatus.value = await aiManjuBridge.installUpdate()
}

async function openLatestRelease(): Promise<void> {
  await aiManjuBridge?.openLatestRelease?.()
}

async function runUpdatePrimaryAction(): Promise<void> {
  if (updateStatus.value.state === 'available') {
    await downloadAppUpdate()
    return
  }
  if (updateStatus.value.state === 'downloaded') {
    await installAppUpdate()
    return
  }
  await checkForAppUpdate()
}

async function openInSystemViewer(filePath: string | null | undefined): Promise<void> {
  if (!filePath) return
  if (!aiManjuBridge?.openPath) {
    openFileError.value = '当前环境不支持打开本地文件（仅桌面 app 内可用）'
    return
  }
  const err = await aiManjuBridge.openPath(filePath)
  openFileError.value = err ? `打开文件失败：${err}（路径：${filePath}）` : null
}

// 参考图字段原来只能手打本地绝对路径，不知道路径的话根本没法用。现在加个"选择文件…"
// 按钮，走 Electron 原生选择框，选完直接把绝对路径填回输入框——multiple=true 时是追加
// (逗号分隔，支持传多张参考图)，false 时是覆盖(起始帧这类单路径字段)。
//
// 预览图缓存按"文件路径"本身当 key(不是按字段名)：一开始按 previewKey(字段名)存，
// 只有刚选完文件那一刻有预览，页面刷新/重新打开项目/切换 tab 之后，路径还在但预览图
// 没了——因为渲染进程手里只剩一串本地绝对路径字符串，跟主进程刚选完文件时顺带传回来的
// data URI 早就对不上了，看起来就是"上传的图片不显示"。改成按路径缓存之后，只要
// 拿到一个路径(不管是刚选的还是从后端读出来的旧数据)，都能按需去问主进程要预览图，
// 而且同一个文件在不同字段里出现也只用读一次盘。
const pathPreviewCache = reactive<Record<string, string | null>>({})
const pathPreviewRequested = new Set<string>()

// 只在缓存里没有、也还没发起过请求时才去问主进程要——避免同一个路径在列表滚动/
// 每次渲染时被反复调用导致重复 IPC。请求失败/环境不支持(比如浏览器里调试)就记 null，
// 模板那边会显示"预览不可用"而不是一直转圈。
function ensurePathPreview(path: string | null | undefined): void {
  if (!path || path in pathPreviewCache || pathPreviewRequested.has(path)) return
  pathPreviewRequested.add(path)
  if (!aiManjuBridge?.readImagePreview) {
    pathPreviewCache[path] = null
    return
  }
  aiManjuBridge
    .readImagePreview(path)
    .then((dataUrl) => {
      pathPreviewCache[path] = dataUrl
    })
    .catch(() => {
      pathPreviewCache[path] = null
    })
}

// 模板里直接调用：命中缓存就同步返回，没命中就顺便触发一次异步加载(等结果回来
// 触发 pathPreviewCache 的响应式更新，下一轮渲染自然就有了)，调用方不用关心加载时序。
function pathPreview(path: string | null | undefined): string | null {
  if (!path) return null
  ensurePathPreview(path)
  return pathPreviewCache[path] ?? null
}

function splitPaths(raw: string | null | undefined): string[] {
  return (raw ?? '')
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean)
}

// 多参考图字段(逗号分隔多个路径)用：拆开之后每个路径各自去拿预览图，只保留已经
// 加载出来的——正在加载/加载失败的那几张不占位置，不会在缩略图行里留一堆空白框。
function previewEntries(raw: string | null | undefined): { path: string; preview: string }[] {
  return splitPaths(raw)
    .map((path) => ({ path, preview: pathPreview(path) }))
    .filter((entry): entry is { path: string; preview: string } => !!entry.preview)
}

async function pickReferenceFile(record: Record<string, string>, key: string, multiple: boolean): Promise<void> {
  if (!aiManjuBridge?.pickImageFile) {
    openFileError.value = '当前环境不支持选择本地文件（仅桌面 app 内可用），可以手动粘贴文件路径'
    return
  }
  const picked = await aiManjuBridge.pickImageFile()
  if (!picked) return
  // 主进程选完文件时已经顺手读出了 data URI，直接拿来填缓存，不用再让 ensurePathPreview
  // 多问主进程读一次盘。picked.error 有值说明路径选上了但读文件失败(常见于 macOS
  // 桌面/文稿/下载目录的隐私权限限制)——路径还是照样填进输入框(不影响生成，Python
  // 侧读同一个文件路径是另一套权限体系)，只是提示一下"为什么这张图看不到缩略图"，
  // 不然用户只会看到"路径填上了但缩略图消失"，完全摸不着头脑。
  pathPreviewCache[picked.path] = picked.dataUrl
  if (picked.error) openFileError.value = picked.error
  if (multiple) {
    const current = (record[key] ?? '').trim()
    record[key] = current ? `${current},${picked.path}` : picked.path
  } else {
    record[key] = picked.path
  }
}

// 多参考图字段(逗号分隔)里，手动去掉其中一张已选的图——只改文本框绑的那个字符串，
// 不动 pathPreviewCache(其他地方可能还在用同一张图的预览缓存，删了反而多余重新读盘)。
function removeReferencePath(record: Record<string, string>, key: string, path: string): void {
  record[key] = splitPaths(record[key]).filter((p) => p !== path).join(',')
}

type ApiStatus = 'checking' | 'ok' | 'error'
type View = 'projects' | 'project' | 'posters' | 'videoGen' | 'textImages' | 'settings' | 'manual'

type StyleMode = 'comic' | 'realistic' | 'render3d' | 'freeform'
// character: 人物剧情，正常按角色驱动写剧本；no_character: 无固定角色(风光/氛围/产品向)，
// 剧本不强行编人物出来，角色库步骤相应弱化。
type ContentType = 'character' | 'no_character'

interface ProjectSummary {
  id: string
  title: string
  premise: string
  status: string
  styleMode: StyleMode
  contentType: ContentType
  // 生成比例：这部剧所有分镜图片/视频统一用这个比例，见 seedream.py 的 IMAGE_RATIOS。
  aspectRatio: string
  createdAt: string
  // 最近一次导出成片成功的时间，null = 还没导出过，项目列表靠它显示"已导出"标签。
  lastExportedAt: string | null
}

interface Shot {
  id: string
  order: number
  sceneType: string | null
  drawPrompt: string
  motionPrompt: string | null
  dialogue: string | null
  durationSec: number
  characterName: string | null
  transitionToNext: string | null
  emotion: string | null
}

interface Scene {
  id: string
  order: number
  summary: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  refImagePath: string | null
  url: string | null
  error: string | null
  providerId: string | null
  model: string | null
  shots: Shot[]
}

interface Story {
  id: string
  content: string
  status: 'pending' | 'running' | 'completed' | 'failed'
}

interface ProjectDetail extends ProjectSummary {
  story: Story | null
  scenes: Scene[]
}

interface PosterOrientationOption {
  id: string
  label: string
}

type PosterLayoutMode = 'title' | 'textBlocks'

interface PosterTemplateItem {
  id: string
  label: string
  promptText: string
  layoutMode: PosterLayoutMode
  createdAt: string
}

interface PosterItem {
  id: string
  projectId: string | null
  orientation: string
  orientationLabel: string
  templateId: string | null
  templateLabel: string | null
  promptText: string | null
  layoutMode: PosterLayoutMode
  bodyLines: string[]
  styleMode: StyleMode
  title: string
  subtitle: string | null
  extraPrompt: string | null
  referenceImagePaths: string | null
  backgroundPath: string | null
  filePath: string | null
  url: string | null
  backgroundUrl: string | null
  status: 'pending' | 'running' | 'completed' | 'failed'
  error: string | null
  providerId: string | null
  model: string | null
  createdAt: string
}

interface VideoGenerationItem {
  id: string
  projectId: string | null
  referenceImagePath: string
  prompt: string
  ratio: string
  ratioLabel: string
  filePath: string | null
  url: string | null
  status: 'pending' | 'running' | 'completed' | 'failed'
  error: string | null
  providerId: string | null
  model: string | null
  createdAt: string
}

interface TextImageItem {
  id: string
  projectId: string | null
  prompt: string
  orientation: string
  orientationLabel: string
  styleMode: StyleMode
  referenceImagePaths: string | null
  characterReferenceImagePaths: string | null
  sceneReferenceImagePaths: string | null
  filePath: string | null
  url: string | null
  status: 'pending' | 'running' | 'completed' | 'failed'
  error: string | null
  providerId: string | null
  model: string | null
  createdAt: string
}

interface Asset {
  id: string
  shotId: string
  type: 'image' | 'video' | 'voice'
  status: 'pending' | 'running' | 'completed' | 'failed'
  filePath: string | null
  url: string | null
  providerId: string | null
  model: string | null
  error: string | null
  selected: boolean
  createdAt: string
}

interface CharacterRef {
  id: string
  storyId: string
  name: string
  prompt: string | null
  status: 'pending' | 'running' | 'completed' | 'failed'
  refImagePath: string | null
  url: string | null
  providerId: string | null
  model: string | null
  error: string | null
}

// 镜头的 characterName 是自由文本(顿号/逗号分隔多个角色名)，这里按名字去已加载的
// 角色库列表里找对应的头像——只做展示层查找，不改 characterName 本身的数据结构，
// 找不到(角色库里没有这个名字，或者还没生成完成)就跳过，不报错。
function characterThumbs(characterName: string | null | undefined): CharacterRef[] {
  if (!characterName) return []
  const names = characterName.split(/[、,，/]/).map((n) => n.trim()).filter(Boolean)
  if (names.length === 0) return []
  return names
    .map((name) => characters.value.find((c) => c.name === name && c.status === 'completed' && c.url))
    .filter((c): c is CharacterRef => !!c)
}

// 关联角色原来是纯手打自由文本，容易打错字（打错就匹配不上角色库，缩略头像也出不来）。
// 现在旁边加个角色库下拉，选一个就追加进去，追加逻辑复用跟 characterThumbs 一样的分隔符
// 规则；已经在里面的名字不重复加。文本框还留着，方便删除/调整顺序。
function appendCharacterName(shot: Shot, name: string): void {
  if (!name) return
  const current = (shot.characterName ?? '').trim()
  const existingNames = current.split(/[、,，/]/).map((n) => n.trim()).filter(Boolean)
  if (existingNames.includes(name)) return
  shot.characterName = current ? `${current}、${name}` : name
  saveShot(shot)
}

function onCharacterSelectChange(shot: Shot, event: Event): void {
  const select = event.target as HTMLSelectElement
  const value = select.value
  select.value = ''
  appendCharacterName(shot, value)
}

// 手动在文本框里删字容易删错/漏顿号，头像旁边直接放一个"×"更直接——选中谁删谁，
// 不用去数文本框里第几个顿号对应第几张头像。
function removeCharacterName(shot: Shot, name: string): void {
  const current = (shot.characterName ?? '').trim()
  const remaining = current
    .split(/[、,，/]/)
    .map((n) => n.trim())
    .filter((n) => n && n !== name)
  shot.characterName = remaining.join('、')
  saveShot(shot)
}

const apiStatus = ref<ApiStatus>('checking')
const view = ref<View>('projects')
// "项目"页原来是"新建项目"表单和"项目列表"堆在同一屏，创建表单本身就占了两步(模板+简介)，
// 跟列表挤在一起显得很长。拆成两个 tab，默认停在列表——大部分时候是回来找已有项目，
// 新建是相对低频的操作，需要时主动点过去。
const projectsTab = ref<'list' | 'create'>('list')

// ---- 设置 ----
const settingsForm = reactive({
  arkApiKey: '',
  arkBaseUrl: '',
  arkImageModel: '',
  arkVideoModel: '',
  arkTextModel: '',
  indexTtsBaseUrl: '',
  // 目录设置：留空 = 用默认目录（<项目根目录>/output）
  outputDir: '',
  // 导出设置
  exportDir: '',
  exportBurnSubtitles: true,
  // 背景音乐：本地音频文件路径，留空 = 没配置(即使开着 exportUseBgm 也不会加)。
  // exportBgmVolume 是背景音乐相对成片原音轨的音量系数(0~1)，成片原音轨音量不变。
  exportBgmPath: '',
  exportBgmVolume: 0.2,
  exportUseBgm: false,
  // 海报标题/副标题是 Pillow 渲染叠上去的，需要一个真的支持中文的字体文件路径；
  // 留空 = 自动按操作系统猜系统字体，猜不到生成海报时会报错，报错里会提示回这里填。
  posterFontPath: ''
})
const settingsInfo = reactive({ arkApiKeySet: false, arkApiKeyMasked: '' })
const settingsSaving = ref(false)
const settingsSavedAt = ref<string | null>(null)
const settingsError = ref<string | null>(null)
// 设置页原来是一长条竖着滚下去的表单，改成选项卡分区看着更清楚：
// 火山方舟模型配置 / AI生成剧本配置 / IndexTTS 配音配置 / 生成与导出(目录+导出+海报字体) /
// 提示词与模板(4个自定义提示词分组) / 关于(版本更新)。"保存全部设置"按钮不分tab，
// 固定在页面底部，切哪个tab都能一次性保存所有字段(后端本来就是整份 PUT)。
type SettingsTab = 'general' | 'story' | 'indextts' | 'generation' | 'prompts' | 'about'
const settingsTab = ref<SettingsTab>('general')
const SETTINGS_TABS: { id: SettingsTab; label: string }[] = [
  { id: 'general', label: '火山方舟模型配置' },
  { id: 'story', label: 'AI生成剧本配置' },
  { id: 'indextts', label: 'IndexTTS 配音配置' },
  { id: 'generation', label: '生成与导出' },
  { id: 'prompts', label: '提示词与模板' },
  { id: 'about', label: '关于' }
]

// ---- 设置：剧本生成方式（本机 claude CLI 或第三方 Anthropic Messages API 兼容服务）----
// 默认 claude_cli，跟后端 Setting.storyGenProvider 的默认值保持一致；只有用户主动
// 配好第三方 API 并切换过来，才会改用 api 方式，避免"什么都没配就被切走"。
const storyGenForm = reactive({
  provider: 'claude_cli' as 'claude_cli' | 'api',
  // claude_cli 模式的手动覆盖路径，留空 = 走自动检测（PATH 查找 + Windows 常见安装
  // 目录扫描 + npm 全局 prefix 动态查询 + 僵尸 shim 识别）。填了就只认这一个路径，
  // 给自动检测找不到/找错的情况一个逃生舱口。
  cliPath: '',
  apiBaseUrl: '',
  apiKey: '',
  apiModel: '',
  apiMaxTokens: 4096
})
const storyGenInfo = reactive({ apiKeySet: false, apiKeyMasked: '' })
interface StoryGenTestState {
  testing: boolean
  result: { ok: boolean; message: string } | null
}
const storyGenCliTest = reactive<StoryGenTestState>({ testing: false, result: null })
const storyGenApiTest = reactive<StoryGenTestState>({ testing: false, result: null })
const storyGenCliDetect = reactive({ detecting: false, message: '' })

async function testStoryGenCli(): Promise<void> {
  storyGenCliTest.testing = true
  storyGenCliTest.result = null
  try {
    // cliPath 留空字符串就不传（后端会用已保存的值/走自动检测）；填了就直接送当前
    // 输入框的值，哪怕还没点保存，方便先测一下路径对不对再决定要不要保存。
    const body: Record<string, unknown> = {}
    if (storyGenForm.cliPath.trim()) body.cliPath = storyGenForm.cliPath.trim()
    storyGenCliTest.result = await api('/settings/test-story-cli', {
      method: 'POST',
      body: JSON.stringify(body)
    })
  } catch (err) {
    storyGenCliTest.result = { ok: false, message: err instanceof Error ? err.message : String(err) }
  } finally {
    storyGenCliTest.testing = false
  }
}

async function detectStoryGenCliPath(): Promise<void> {
  storyGenCliDetect.detecting = true
  storyGenCliDetect.message = ''
  try {
    const result = await api<{ found: boolean; path: string | null; message?: string }>(
      '/settings/detect-story-cli-path',
      { method: 'POST' }
    )
    if (result.found && result.path) {
      storyGenForm.cliPath = result.path
      storyGenCliDetect.message = `检测到：${result.path}（记得点下面的"保存设置"才会生效）`
    } else {
      storyGenCliDetect.message = result.message ?? '自动检测未找到 claude CLI，请手动填写完整路径'
    }
  } catch (err) {
    storyGenCliDetect.message = err instanceof Error ? err.message : String(err)
  } finally {
    storyGenCliDetect.detecting = false
  }
}

async function testStoryGenApi(): Promise<void> {
  storyGenApiTest.testing = true
  storyGenApiTest.result = null
  try {
    // baseUrl/model 不是敏感信息，表单里就是真实值，直接送当前填的（哪怕还没点保存），
    // 这样改完还没保存也能先测；apiKey 是密码框，只有用户重新输入过才送新值，
    // 留空就代表"沿用已保存的那份"，交给后端去补，跟 arkApiKey 的处理方式一致。
    const body: Record<string, unknown> = {
      baseUrl: storyGenForm.apiBaseUrl,
      model: storyGenForm.apiModel,
      maxTokens: storyGenForm.apiMaxTokens
    }
    if (storyGenForm.apiKey) body.apiKey = storyGenForm.apiKey
    storyGenApiTest.result = await api('/settings/test-story-api', {
      method: 'POST',
      body: JSON.stringify(body)
    })
  } catch (err) {
    storyGenApiTest.result = { ok: false, message: err instanceof Error ? err.message : String(err) }
  } finally {
    storyGenApiTest.testing = false
  }
}

// ---- 设置：自定义提示词（出图风格前缀 / 剧本写作风格提示 / 内容类型提示 / 项目模板）----
// 这几个字段留空 = 用代码里写死的默认值，这里的常量只是给输入框当 placeholder 展示"默认值
// 长什么样"，不是真的会被发送到后端——真正生效的默认值以 ai-service 里的
// seedream.py STYLE_PREFIXES / story_generator.py STYLE_HINTS+CONTENT_TYPE_HINTS 为准，
// 这里只是抄一份展示用，两边如果后续各自改动措辞，不影响功能只影响 placeholder 文案。
const BUILTIN_STYLE_PREFIXES: Record<StyleMode, string> = {
  comic: '国漫赛璐璐风格，二次元厚涂动画质感，禁止写实摄影/真人风格。',
  realistic: '真实摄影质感，真人实拍风格，自然光影，电影级写实感，禁止动画/漫画/卡通风格。',
  render3d:
    '3D渲染动画质感，CG角色建模，皮克斯/迪士尼3D动画电影级渲染，柔和全局光照，合成质感，禁止2D手绘/真人摄影/赛璐璐风格。',
  freeform: '（无前缀，完全交给模型自由发挥）'
}
const BUILTIN_STYLE_HINTS: Record<StyleMode, string> = {
  comic: '国漫赛璐璐（二次元厚涂动画感）',
  realistic: '真人实拍写实摄影（不要出现动画/漫画/赛璐璐等二次元描述）',
  render3d: '3D渲染CG动画（皮克斯/迪士尼3D电影质感，不要出现2D手绘或真人摄影描述）',
  freeform: '不限定，自由发挥'
}
const BUILTIN_CONTENT_TYPE_HINTS: Record<ContentType, string> = {
  character: '这是一部有人物的短剧，正常按角色驱动来写。',
  no_character:
    '这个故事不需要固定的人物角色，画面以场景/氛围/产品/风光为主体。不要虚构人物角色，characterName 一律留空字符串，drawPrompt 里也不要描写人物。'
}
const STYLE_MODE_LABELS: Record<StyleMode, string> = {
  comic: '漫画风',
  realistic: '真人风',
  render3d: '3D风',
  freeform: 'AI自由发挥'
}
const CONTENT_TYPE_LABELS: Record<ContentType, string> = {
  character: '人物剧情',
  no_character: '无固定角色（风光/氛围/产品）'
}
const customStylePrefixesForm = reactive<Record<StyleMode, string>>({
  comic: '',
  realistic: '',
  render3d: '',
  freeform: ''
})
const customStyleHintsForm = reactive<Record<StyleMode, string>>({
  comic: '',
  realistic: '',
  render3d: '',
  freeform: ''
})
const customContentTypeHintsForm = reactive<Record<ContentType, string>>({
  character: '',
  no_character: ''
})
interface CustomProjectTemplateRow {
  label: string
  description: string
  contentType: ContentType
  styleMode: StyleMode
}
// 项目模板列表没有暴露 id 输入框给用户填——id 只是给前端选中状态用的内部标识，
// 让用户手填 id 徒增一个容易填重复/填出非法字符的技术细节，保存时按行号自动生成 user-0/user-1/...
const customProjectTemplatesForm = ref<CustomProjectTemplateRow[]>([])

function addCustomProjectTemplateRow(): void {
  customProjectTemplatesForm.value.push({ label: '', description: '', contentType: 'character', styleMode: 'comic' })
}
function removeCustomProjectTemplateRow(index: number): void {
  customProjectTemplatesForm.value.splice(index, 1)
}

// ---- 项目列表 ----
const projects = ref<ProjectSummary[]>([])
const newPremise = ref('')
// 内容类型/风格故意不给默认值(null=还没选)——之前两个都预选了默认项，静默默认很容易被
// 忽略着直接点创建，这也是"选了真人却出漫画"这类反馈的一部分成因。现在不主动选一次，
// "创建"按钮就不会亮，逼着过一遍这两个选择，而不是无意识用掉默认值。
const newContentType = ref<ContentType | null>('character')
const newStyleMode = ref<StyleMode | null>('comic')
// 生成比例是独立于"内容类型+风格"模板系统之外的第三个轴——不管选哪个模板，
// 比例都单独选，默认 9:16(手机短视频最常见的比例)，不强制像 contentType/styleMode
// 那样清空默认值逼用户选一次。
const newAspectRatio = ref<string>('9:16')
const creatingProject = ref(false)

// 项目模板：把"内容类型 + 风格"这两个抽象的轴，包成几个用户实际会用到的具体场景，
// 选个名字就把两个轴都填好了，不用每次新建都自己去拼。"自定义"不预填，走回两个原始单选。
// id 原本是个固定的联合类型，现在允许用户在设置页整份替换模板列表(customProjectTemplates)，
// 自定义模板的 id 是保存时生成的(user-0/user-1/...)，所以这里放宽成 string，
// 只保留字面量 'custom' 作为"自行选择"这个永远存在的兜底项的保留 id。
interface ProjectTemplateItem {
  id: string
  label: string
  description: string
  contentType: ContentType | null
  styleMode: StyleMode | null
}
const BUILTIN_PROJECT_TEMPLATES: ProjectTemplateItem[] = [
  { id: 'ai_comic', label: 'AI漫剧', description: '人物剧情 · 漫画风', contentType: 'character', styleMode: 'comic' },
  { id: 'ai_realistic', label: 'AI真人剧', description: '人物剧情 · 真人风', contentType: 'character', styleMode: 'realistic' },
  { id: 'anime_3d', label: '3D形象动漫', description: '人物剧情 · 3D风', contentType: 'character', styleMode: 'render3d' },
  { id: 'travel_vlog', label: '风景旅行Vlog', description: '无固定角色 · 真人风', contentType: 'no_character', styleMode: 'realistic' },
  { id: 'medical_vlog', label: '医美地陪Vlog', description: '人物剧情 · 真人风', contentType: 'character', styleMode: 'realistic' }
]
// 设置页里"项目类型模板"整份替换后的数据，null = 没自定义过，用内置的那几张卡。
const customProjectTemplatesRaw = ref<ProjectTemplateItem[] | null>(null)
const effectiveProjectTemplates = computed<ProjectTemplateItem[]>(() => {
  const base =
    customProjectTemplatesRaw.value && customProjectTemplatesRaw.value.length
      ? customProjectTemplatesRaw.value
      : BUILTIN_PROJECT_TEMPLATES
  return [...base, { id: 'custom', label: '自定义…', description: '自行选择内容类型与风格', contentType: null, styleMode: null }]
})
const newTemplate = ref<string>('ai_comic')

function applyProjectTemplate(templateId: string): void {
  newTemplate.value = templateId
  const tpl = effectiveProjectTemplates.value.find((t) => t.id === templateId)
  newContentType.value = tpl?.contentType ?? null
  newStyleMode.value = tpl?.styleMode ?? null
}

// 设置页加载完设置后 effectiveProjectTemplates 可能从内置卡片整份换成自定义卡片，
// 这时默认选中的 newTemplate('ai_comic') 很可能已经不存在了，卡片列表里就没有任何一张
// 是高亮的，体验很奇怪——这里发现选中的模板已经不在列表里时，自动切回列表第一张。
watch(effectiveProjectTemplates, (list) => {
  if (!list.find((t) => t.id === newTemplate.value)) {
    applyProjectTemplate(list[0]?.id ?? 'custom')
  }
})

const canCreateProject = computed(
  () => !!newPremise.value.trim() && !!newContentType.value && !!newStyleMode.value
)

// ---- 项目详情 ----
const activeProject = ref<ProjectDetail | null>(null)
const generatingStory = ref(false)
const assetsByShot = reactive<Record<string, Asset[]>>({})
const characters = ref<CharacterRef[]>([])
const generatingCharacter = reactive<Record<string, boolean>>({})
const refImagePathsInput = reactive<Record<string, string>>({})
const startImagePathInput = reactive<Record<string, string>>({})
const voiceOptionsInput = reactive<Record<string, { referenceAudioPath: string; voiceId: string; speed: number }>>(
  {}
)
const generatingAsset = reactive<Record<string, boolean>>({})
const exporting = ref(false)
const exportError = ref<string | null>(null)
const exportUrl = ref<string | null>(null)
const exportFilePath = ref<string | null>(null)
// 后端跳过的镜头列表(没有已完成视频，没进最终成片)，导出成功后用来提示"哪几镜被跳过了"。
const exportSkippedShots = ref<{ sceneOrder: number; shotOrder: number }[]>([])

// ---- 海报 ----
// 海报是独立的一级功能(见 posters.py 顶层 /posters 路由)，不挂在任何视频项目下面，
// 用户不用先建视频项目、写完剧本才能出一张宣传海报。
// "类型"不再是写死的几个预设，改成开放的模版库(PosterTemplate，见 poster-templates
// 路由)——选一个模版直接用它的提示词+排版方式，或者不选模版自己临时写一次性提示词。
// orientations(朝向)还是固定两个，走 /posters/options。
const posters = ref<PosterItem[]>([])
const posterOrientations = ref<PosterOrientationOption[]>([])
let posterOrientationsLoaded = false
const posterTemplates = ref<PosterTemplateItem[]>([])
const postersTab = ref<'list' | 'create'>('list')
const posterForm = reactive({
  orientation: 'portrait' as string,
  // 空字符串 = 不选模版，走"自定义"分支，下面 promptText/layoutMode 才生效。
  templateId: '',
  promptText: '',
  layoutMode: 'title' as PosterLayoutMode,
  // 多行正文的原始文本框内容，提交前按行拆分成数组(见 bodyLinesFromText)。
  bodyLinesText: '',
  styleMode: 'comic' as StyleMode,
  title: '',
  subtitle: '',
  extraPrompt: ''
})
// 参考图路径单独用一个 Record<string,string>，跟 sceneRefImagePathsInput 同一个模式，
// 这样能直接复用现成的 pickReferenceFile(record, key, multiple) 选择文件逻辑，
// 不用给 posterForm 整个对象额外声明索引签名。key 固定叫 'new'，因为创建表单只有一份。
const posterRefPathInput = reactive<Record<string, string>>({ new: '' })
const creatingPoster = ref(false)
const posterError = ref<string | null>(null)
const editingPosterId = ref<string | null>(null)
const editingPosterForm = reactive({ title: '', subtitle: '', bodyLinesText: '' })
const savingPosterText = ref(false)
const regeneratingPoster = reactive<Record<string, boolean>>({})
// "自定义"分支下把当次手写的提示词存成一条可复用模版，避免每次重新打字。
const showSaveTemplateForm = ref(false)
const newTemplateLabel = ref('')
const savingPosterTemplate = ref(false)

// 没选模版就用 posterForm.layoutMode 自己选的；选了模版就跟着模版的排版方式走，
// 表单上不重复显示排版选择器，避免"选了模版又能覆盖模版排版"这种模棱两可的状态。
const effectivePosterLayoutMode = computed<PosterLayoutMode>(() => {
  if (posterForm.templateId) {
    return posterTemplates.value.find((t) => t.id === posterForm.templateId)?.layoutMode ?? 'title'
  }
  return posterForm.layoutMode
})

function bodyLinesFromText(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

// ---- 视频生成（无剧本，上传参考图直接生成视频）----
// 跟海报一样是独立的一级功能，不需要先建视频项目/写剧本/拆场次镜头。单张参考图 +
// 一段描述，直接调 Seedance 出一条视频；不做配音/字幕/多段拼接，最快最简单的路径。
const videoGenList = ref<VideoGenerationItem[]>([])
const videoGenTab = ref<'list' | 'create'>('list')
const videoGenForm = reactive({ prompt: '', ratio: '9:16' as string })
// 复用 pickReferenceFile(record, key, multiple)，key 固定叫 'new'，
// 跟 posterRefPathInput 同一个模式；multiple=false 因为图生视频只需要一张起始帧参考图。
const videoGenRefPathInput = reactive<Record<string, string>>({ new: '' })
const creatingVideoGen = ref(false)
const videoGenError = ref<string | null>(null)
const regeneratingVideoGen = reactive<Record<string, boolean>>({})
// 生成比例：图生视频(每次单独选)和新建短剧(项目级设置)共用同一份比例词典，
// 走共享的 GET /media-ratios(跟海报/文生图各自的 /options 是同一份底层配置，
// 见 app/providers/seedream.py 的 IMAGE_RATIOS)。
const mediaRatios = ref<PosterOrientationOption[]>([])
let mediaRatiosLoaded = false

// ---- 文生图（独立文生图，不挂在任何项目下）----
const textImages = ref<TextImageItem[]>([])
const textImageOrientations = ref<PosterOrientationOption[]>([])
let textImageOrientationsLoaded = false
const textImagesTab = ref<'list' | 'create'>('list')
const textImageForm = reactive({
  prompt: '',
  styleMode: 'comic' as StyleMode,
  orientation: 'portrait' as string
})
// 角色参考图(character)和环境参考图(scene)分开管理——用户上传时清楚这张图是给
// "人物长相"参考还是"场景/环境"参考，两份各自支持多选+预览，生成时后端会合并
// 成一份传给 Seedream(接口本身不支持按图片打标签，纯粹是我们这边管理上分开)。
const textImageRefPathInput = reactive<Record<string, string>>({ character: '', scene: '' })
const creatingTextImage = ref(false)
const textImageError = ref<string | null>(null)
const regeneratingTextImage = reactive<Record<string, boolean>>({})
const textImagePromptPreview = computed(() => textImageForm.prompt.trim() || '夜晚城市天台，霓虹灯背景，一只猫坐在栏杆上')
const textImageOrientationLabel = computed(() => {
  return textImageOrientations.value.find((o) => o.id === textImageForm.orientation)?.label ?? '竖版'
})

let pollTimer: ReturnType<typeof setInterval> | null = null

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${apiBaseUrl}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options
    })
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err)
    throw new Error(`无法连接本地后端服务（${detail}）。如果页面左下角显示“后端 error”，说明 ai-service 没有启动成功；macOS 打包版通常是缺少 Python 依赖，请安装新版后重试。`)
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let message = text
    try {
      const parsed = JSON.parse(text)
      message = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail ?? parsed)
    } catch {
      // keep raw response text
    }
    throw new Error(message ? `${res.status} ${message}` : `${res.status} ${res.statusText}`)
  }
  return res.json() as Promise<T>
}

async function checkHealth(): Promise<void> {
  try {
    const res = await fetch(`${apiBaseUrl}/health`)
    apiStatus.value = res.ok ? 'ok' : 'error'
  } catch {
    apiStatus.value = 'error'
  }
}

// ---- "AI优化提示词"通用小工具：海报/文生图/图生视频/分镜画面描述/角色设定图/
// 场景参考图，凡是有一个"画面描述"输入框的地方都能挂这个按钮。用 key 区分各个
// 输入框各自的 loading/报错状态，不用为每个位置单独声明一份 reactive 状态。
interface PromptOptimizeState {
  optimizing: boolean
  error: string | null
}
const promptOptimizeStates = reactive<Record<string, PromptOptimizeState>>({})

function promptOptimizeState(key: string): PromptOptimizeState {
  if (!promptOptimizeStates[key]) promptOptimizeStates[key] = { optimizing: false, error: null }
  return promptOptimizeStates[key]
}

async function optimizePromptField(
  key: string,
  currentValue: string,
  context: string,
  applyResult: (optimized: string) => void
): Promise<void> {
  const state = promptOptimizeState(key)
  const prompt = currentValue.trim()
  if (!prompt) {
    state.error = '先写点内容再优化'
    return
  }
  state.optimizing = true
  state.error = null
  try {
    const result = await api<{ optimizedPrompt: string; engine: string }>('/prompts/optimize', {
      method: 'POST',
      body: JSON.stringify({ prompt, context })
    })
    applyResult(result.optimizedPrompt)
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err)
  } finally {
    state.optimizing = false
  }
}

// ---- 设置 ----
interface SettingsResponse {
  arkApiKeySet: boolean
  arkApiKey: string | null
  arkBaseUrl: string | null
  arkImageModel: string | null
  arkVideoModel: string | null
  arkTextModel: string | null
  indexTtsBaseUrl: string | null
  outputDir: string | null
  exportDir: string | null
  exportBurnSubtitles: boolean
  exportBgmPath: string | null
  exportBgmVolume: number
  exportUseBgm: boolean
  posterFontPath: string | null
  customStylePrefixes: Record<string, string> | null
  customStyleHints: Record<string, string> | null
  customContentTypeHints: Record<string, string> | null
  customProjectTemplates: (ProjectTemplateItem & { contentType: ContentType; styleMode: StyleMode })[] | null
  storyGenProvider: 'claude_cli' | 'api'
  storyGenApiBaseUrl: string | null
  storyGenApiKey: string | null
  storyGenApiKeySet: boolean
  storyGenApiModel: string | null
  storyGenApiMaxTokens: number
  storyGenCliPath: string | null
}

async function loadSettings(): Promise<void> {
  const data = await api<SettingsResponse>('/settings')
  settingsInfo.arkApiKeySet = data.arkApiKeySet
  settingsInfo.arkApiKeyMasked = data.arkApiKey ?? ''
  settingsForm.arkBaseUrl = data.arkBaseUrl ?? ''
  settingsForm.arkImageModel = data.arkImageModel ?? ''
  settingsForm.arkVideoModel = data.arkVideoModel ?? ''
  settingsForm.arkTextModel = data.arkTextModel ?? ''
  settingsForm.indexTtsBaseUrl = data.indexTtsBaseUrl ?? ''
  settingsForm.outputDir = data.outputDir ?? ''
  settingsForm.exportDir = data.exportDir ?? ''
  settingsForm.exportBurnSubtitles = data.exportBurnSubtitles
  settingsForm.exportBgmPath = data.exportBgmPath ?? ''
  settingsForm.exportBgmVolume = data.exportBgmVolume ?? 0.2
  settingsForm.exportUseBgm = data.exportUseBgm
  settingsForm.posterFontPath = data.posterFontPath ?? ''

  storyGenForm.provider = data.storyGenProvider ?? 'claude_cli'
  storyGenForm.cliPath = data.storyGenCliPath ?? ''
  storyGenForm.apiBaseUrl = data.storyGenApiBaseUrl ?? ''
  storyGenForm.apiModel = data.storyGenApiModel ?? ''
  storyGenForm.apiMaxTokens = data.storyGenApiMaxTokens ?? 4096
  storyGenInfo.apiKeySet = data.storyGenApiKeySet
  storyGenInfo.apiKeyMasked = data.storyGenApiKey ?? ''

  const prefixes = data.customStylePrefixes ?? {}
  const styleHints = data.customStyleHints ?? {}
  const contentHints = data.customContentTypeHints ?? {}
  for (const key of Object.keys(customStylePrefixesForm) as StyleMode[]) {
    customStylePrefixesForm[key] = prefixes[key] ?? ''
  }
  for (const key of Object.keys(customStyleHintsForm) as StyleMode[]) {
    customStyleHintsForm[key] = styleHints[key] ?? ''
  }
  for (const key of Object.keys(customContentTypeHintsForm) as ContentType[]) {
    customContentTypeHintsForm[key] = contentHints[key] ?? ''
  }
  customProjectTemplatesRaw.value = data.customProjectTemplates ?? null
  customProjectTemplatesForm.value = (data.customProjectTemplates ?? []).map((t) => ({
    label: t.label,
    description: t.description ?? '',
    contentType: t.contentType,
    styleMode: t.styleMode
  }))
}

// 把「key -> 提示文字」表单(4个或2个 styleMode/contentType key)序列化成给后端的 JSON 字符串：
// 过滤掉留空的 key(留空=用默认值，不应该被当成"用户把默认值改成空字符串"存进去)；
// 如果全部留空，返回空字符串——对应后端"传空字符串=清空自定义，恢复内置默认"的约定。
function serializeKeyMapField(form: Record<string, string>): string {
  const obj: Record<string, string> = {}
  for (const key of Object.keys(form)) {
    if (form[key] && form[key].trim()) obj[key] = form[key].trim()
  }
  return Object.keys(obj).length ? JSON.stringify(obj) : ''
}

function serializeProjectTemplatesField(): string {
  const rows = customProjectTemplatesForm.value.filter((r) => r.label.trim())
  if (!rows.length) return ''
  return JSON.stringify(
    rows.map((r, i) => ({
      id: `user-${i}`,
      label: r.label.trim(),
      description: r.description.trim(),
      contentType: r.contentType,
      styleMode: r.styleMode
    }))
  )
}

async function saveSettings(): Promise<void> {
  settingsSaving.value = true
  settingsError.value = null
  settingsSavedAt.value = null
  try {
    const body: Record<string, unknown> = {
      indexTtsBaseUrl: settingsForm.indexTtsBaseUrl,
      arkBaseUrl: settingsForm.arkBaseUrl,
      arkImageModel: settingsForm.arkImageModel,
      arkVideoModel: settingsForm.arkVideoModel,
      arkTextModel: settingsForm.arkTextModel,
      outputDir: settingsForm.outputDir,
      exportDir: settingsForm.exportDir,
      exportBurnSubtitles: settingsForm.exportBurnSubtitles,
      exportBgmPath: settingsForm.exportBgmPath,
      exportBgmVolume: settingsForm.exportBgmVolume,
      exportUseBgm: settingsForm.exportUseBgm,
      posterFontPath: settingsForm.posterFontPath,
      customStylePrefixes: serializeKeyMapField(customStylePrefixesForm),
      customStyleHints: serializeKeyMapField(customStyleHintsForm),
      customContentTypeHints: serializeKeyMapField(customContentTypeHintsForm),
      customProjectTemplates: serializeProjectTemplatesField(),
      storyGenProvider: storyGenForm.provider,
      storyGenCliPath: storyGenForm.cliPath,
      storyGenApiBaseUrl: storyGenForm.apiBaseUrl,
      storyGenApiModel: storyGenForm.apiModel,
      storyGenApiMaxTokens: storyGenForm.apiMaxTokens
    }
    if (settingsForm.arkApiKey) body.arkApiKey = settingsForm.arkApiKey
    if (storyGenForm.apiKey) body.storyGenApiKey = storyGenForm.apiKey
    await api('/settings', { method: 'PUT', body: JSON.stringify(body) })
    settingsForm.arkApiKey = ''
    storyGenForm.apiKey = ''
    settingsSavedAt.value = new Date().toLocaleTimeString()
    await loadSettings()
  } catch (err) {
    // 之前这里没有 catch：保存失败（比如后端还没起来、网络错误、400 校验失败）
    // 会被静默吞掉，界面上完全看不出保存到底成没成功。现在把错误显示出来。
    settingsError.value = err instanceof Error ? err.message : String(err)
  } finally {
    settingsSaving.value = false
  }
}

// ---- 项目列表 ----
async function loadProjects(): Promise<void> {
  projects.value = await api<ProjectSummary[]>('/projects')
}

async function createProject(): Promise<void> {
  if (!canCreateProject.value) return
  creatingProject.value = true
  try {
    await api('/projects', {
      method: 'POST',
      body: JSON.stringify({
        premise: newPremise.value.trim(),
        styleMode: newStyleMode.value,
        contentType: newContentType.value,
        aspectRatio: newAspectRatio.value
      })
    })
    newPremise.value = ''
    newTemplate.value = 'ai_comic'
    newContentType.value = 'character'
    newStyleMode.value = 'comic'
    newAspectRatio.value = '9:16'
    await loadProjects()
    projectsTab.value = 'list'
  } finally {
    creatingProject.value = false
  }
}

async function deleteProject(id: string): Promise<void> {
  if (!window.confirm('删除这个项目，连同它下面所有场次/镜头/角色库/已生成的素材记录一起删，确定吗？（磁盘上已经生成好的文件不会被删除）')) return
  await api(`/projects/${id}`, { method: 'DELETE' })
  await loadProjects()
}

// 项目列表标题原来只能进详情页改，一步之遥却要多点两次。改成列表页直接点标题旁的
// 编辑按钮，原地变成输入框，回车/失焦/点击别处都保存，Esc 放弃修改。
const editingProjectId = ref<string | null>(null)
const editingProjectTitle = ref('')

function startEditProjectTitle(p: ProjectSummary): void {
  editingProjectId.value = p.id
  editingProjectTitle.value = p.title
}

function cancelEditProjectTitle(): void {
  editingProjectId.value = null
}

async function saveProjectTitle(p: ProjectSummary): Promise<void> {
  if (editingProjectId.value !== p.id) return
  editingProjectId.value = null
  const title = editingProjectTitle.value.trim()
  if (!title || title === p.title) return
  const updated = await api<{ title: string }>(`/projects/${p.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ title })
  })
  p.title = updated.title
}

const styleModeLabels: Record<StyleMode, string> = {
  comic: '漫画',
  realistic: '真人',
  render3d: '3D',
  freeform: 'AI自由发挥'
}
const contentTypeLabels: Record<ContentType, string> = {
  character: '人物剧情',
  no_character: '无固定角色'
}

// 生成比例的 label 是动态从 /media-ratios 拉的(不是写死的 Record)，项目列表/详情页
// 复用同一份 mediaRatios 数据做 id -> label 查找，找不到就退化成显示原始 id。
function ratioLabel(id: string): string {
  return mediaRatios.value.find((r) => r.id === id)?.label ?? id
}

async function updateProjectStyleMode(mode: StyleMode): Promise<void> {
  if (!activeProject.value) return
  const updated = await api<{ styleMode: StyleMode }>(`/projects/${activeProject.value.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ styleMode: mode })
  })
  activeProject.value.styleMode = updated.styleMode
}

async function updateProjectContentType(type: ContentType): Promise<void> {
  if (!activeProject.value) return
  const updated = await api<{ contentType: ContentType }>(`/projects/${activeProject.value.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ contentType: type })
  })
  activeProject.value.contentType = updated.contentType
}

async function updateProjectAspectRatio(ratio: string): Promise<void> {
  if (!activeProject.value) return
  const updated = await api<{ aspectRatio: string }>(`/projects/${activeProject.value.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ aspectRatio: ratio })
  })
  activeProject.value.aspectRatio = updated.aspectRatio
}

// ---- 项目详情 ----
async function openProject(id: string): Promise<void> {
  view.value = 'project'
  activeStep.value = 'story'
  await loadProjectDetail(id)
}

// 生成产物文件名是固定的（比如每次都是 image.png），重新生成会原地覆盖，
// 但浏览器会按 URL 缓存图片/视频——同一个 URL 不会重新拉取，看起来就像"点了
// 重新生成但画面没变"。这里给每次拉取到的 url 加个时间戳查询参数，绕开缓存。
function withCacheBust(url: string | null): string | null {
  if (!url) return null
  return `${url}${url.includes('?') ? '&' : '?'}t=${Date.now()}`
}

async function loadProjectDetail(id: string): Promise<void> {
  activeProject.value = await api<ProjectDetail>(`/projects/${id}`)
  for (const scene of activeProject.value.scenes) {
    scene.url = withCacheBust(scene.url)
    for (const shot of scene.shots) {
      await loadShotAssets(shot.id)
      if (!(shot.id in voiceOptionsInput)) {
        voiceOptionsInput[shot.id] = { referenceAudioPath: '', voiceId: '', speed: 1 }
      }
    }
  }
  if (activeProject.value.scenes.length > 0) {
    await loadCharacters(id)
  }
  ensureValidSceneSelection()
  ensureValidShotSelection()
}

async function loadPosters(): Promise<void> {
  const data = await api<PosterItem[]>('/posters')
  // 海报文件名固定是 poster.png，重新生成/改文字都是原地覆盖，不加时间戳的话浏览器
  // 会因为 URL 没变而不重新拉取，看起来像"点了重新生成但海报没变"（跟场景参考图同一个坑）。
  for (const p of data) {
    p.url = withCacheBust(p.url)
  }
  posters.value = data
}

// 朝向是固定两个值(竖版/横版)，不会中途变化，只拉一次。
async function loadPosterOrientations(): Promise<void> {
  if (posterOrientationsLoaded) return
  try {
    const data = await api<{ orientations: PosterOrientationOption[] }>('/posters/options')
    posterOrientations.value = data.orientations
    posterOrientationsLoaded = true
  } catch {
    // 静默失败：创建海报表单里的朝向选择器会是空的，用户能看出来，不用额外弹错误。
  }
}

// 生成比例词典同样固定不变，只拉一次；图生视频表单和新建短剧表单共用这一份。
async function loadMediaRatios(): Promise<void> {
  if (mediaRatiosLoaded) return
  try {
    const data = await api<{ ratios: PosterOrientationOption[] }>('/media-ratios')
    mediaRatios.value = data.ratios
    mediaRatiosLoaded = true
  } catch {
    // 静默失败：图生视频/新建短剧表单里的比例选择器会是空的，用户能看出来，不用额外弹错误。
  }
}

// 模版库是用户能自己增删改的一份清单(见 poster-templates 路由)，每次进创建表单都
// 重新拉一遍，不像 orientations 那样只拉一次——用户随时可能新增/删除模版。
async function loadPosterTemplates(): Promise<void> {
  try {
    posterTemplates.value = await api<PosterTemplateItem[]>('/poster-templates')
  } catch {
    // 静默失败：模版选择器会是空的，用户还是能走"自定义"分支手写提示词。
  }
}

async function createPoster(): Promise<void> {
  if (!posterForm.title.trim()) return
  posterError.value = null

  const layoutMode = effectivePosterLayoutMode.value
  const bodyLines = layoutMode === 'textBlocks' ? bodyLinesFromText(posterForm.bodyLinesText) : []
  if (layoutMode === 'textBlocks' && bodyLines.length === 0) {
    posterError.value = '多行正文排版至少要填一行内容'
    return
  }
  if (!posterForm.templateId && !posterForm.promptText.trim()) {
    posterError.value = '请选择一个模版，或者填写提示词'
    return
  }

  creatingPoster.value = true
  try {
    await api('/posters', {
      method: 'POST',
      body: JSON.stringify({
        orientation: posterForm.orientation,
        templateId: posterForm.templateId || null,
        promptText: posterForm.templateId ? null : posterForm.promptText.trim(),
        layoutMode,
        bodyLines: layoutMode === 'textBlocks' ? bodyLines : null,
        styleMode: posterForm.styleMode,
        title: posterForm.title.trim(),
        subtitle: posterForm.subtitle.trim() || null,
        extraPrompt: posterForm.extraPrompt.trim() || null,
        referenceImagePaths: posterRefPathInput.new.trim() || null
      })
    })
    posterForm.title = ''
    posterForm.subtitle = ''
    posterForm.extraPrompt = ''
    posterForm.promptText = ''
    posterForm.bodyLinesText = ''
    posterRefPathInput.new = ''
    postersTab.value = 'list'
    await loadPosters()
  } catch (err) {
    posterError.value = err instanceof Error ? err.message : String(err)
  } finally {
    creatingPoster.value = false
  }
}

// 把"自定义"分支下当次手写的提示词存成一条可复用模版，避免每次重新打字——这是
// 用户最初提出海报功能时就想要的能力(自定义模版保存下次选择)。保存成功后直接
// 切到刚保存的模版，行为上等于"保存并使用"。
async function saveCurrentPosterAsTemplate(): Promise<void> {
  const label = newTemplateLabel.value.trim()
  const promptText = posterForm.promptText.trim()
  if (!label || !promptText) return
  savingPosterTemplate.value = true
  try {
    const tpl = await api<PosterTemplateItem>('/poster-templates', {
      method: 'POST',
      body: JSON.stringify({ label, promptText, layoutMode: posterForm.layoutMode })
    })
    await loadPosterTemplates()
    posterForm.templateId = tpl.id
    newTemplateLabel.value = ''
    showSaveTemplateForm.value = false
  } finally {
    savingPosterTemplate.value = false
  }
}

async function deletePosterTemplate(templateId: string): Promise<void> {
  if (!window.confirm('删除这个模版，确定吗？已经用它生成过的海报不受影响。')) return
  await api(`/poster-templates/${templateId}`, { method: 'DELETE' })
  await loadPosterTemplates()
  if (posterForm.templateId === templateId) {
    posterForm.templateId = ''
  }
}

async function regeneratePoster(posterId: string): Promise<void> {
  regeneratingPoster[posterId] = true
  try {
    await api(`/posters/${posterId}/regenerate`, { method: 'POST' })
    await loadPosters()
  } finally {
    regeneratingPoster[posterId] = false
  }
}

function startEditPosterText(poster: PosterItem): void {
  editingPosterId.value = poster.id
  editingPosterForm.title = poster.title
  editingPosterForm.subtitle = poster.subtitle ?? ''
  editingPosterForm.bodyLinesText = (poster.bodyLines ?? []).join('\n')
}

function cancelEditPosterText(): void {
  editingPosterId.value = null
}

async function savePosterText(posterId: string): Promise<void> {
  savingPosterText.value = true
  try {
    const poster = posters.value.find((p) => p.id === posterId)
    const body: Record<string, unknown> = {
      title: editingPosterForm.title.trim(),
      subtitle: editingPosterForm.subtitle.trim()
    }
    if (poster?.layoutMode === 'textBlocks') {
      body.bodyLines = bodyLinesFromText(editingPosterForm.bodyLinesText)
    }
    await api(`/posters/${posterId}`, { method: 'PATCH', body: JSON.stringify(body) })
    editingPosterId.value = null
    await loadPosters()
  } finally {
    savingPosterText.value = false
  }
}

async function deletePoster(posterId: string): Promise<void> {
  if (!window.confirm('删除这张海报，确定吗？（磁盘上已经生成好的文件不会被删除）')) return
  await api(`/posters/${posterId}`, { method: 'DELETE' })
  await loadPosters()
}

// ---- 视频生成（无剧本，上传参考图直接生成视频）----
async function loadVideoGenerations(): Promise<void> {
  const data = await api<VideoGenerationItem[]>('/video-generations')
  // 视频文件名固定是 video.mp4，重新生成是原地覆盖，不加时间戳浏览器不会重新拉取。
  for (const v of data) {
    v.url = withCacheBust(v.url)
  }
  videoGenList.value = data
}

async function createVideoGeneration(): Promise<void> {
  if (!videoGenForm.prompt.trim() || !videoGenRefPathInput.new.trim()) return
  videoGenError.value = null
  creatingVideoGen.value = true
  try {
    await api('/video-generations', {
      method: 'POST',
      body: JSON.stringify({
        referenceImagePath: videoGenRefPathInput.new.trim(),
        prompt: videoGenForm.prompt.trim(),
        ratio: videoGenForm.ratio
      })
    })
    videoGenForm.prompt = ''
    videoGenRefPathInput.new = ''
    videoGenTab.value = 'list'
    await loadVideoGenerations()
  } catch (err) {
    videoGenError.value = err instanceof Error ? err.message : String(err)
  } finally {
    creatingVideoGen.value = false
  }
}

async function regenerateVideoGeneration(videoId: string): Promise<void> {
  regeneratingVideoGen[videoId] = true
  try {
    await api(`/video-generations/${videoId}/regenerate`, { method: 'POST' })
    await loadVideoGenerations()
  } finally {
    regeneratingVideoGen[videoId] = false
  }
}

async function deleteVideoGeneration(videoId: string): Promise<void> {
  if (!window.confirm('删除这条视频记录，确定吗？（磁盘上已经生成好的文件不会被删除）')) return
  await api(`/video-generations/${videoId}`, { method: 'DELETE' })
  await loadVideoGenerations()
}

// ---- 文生图（独立文生图，不挂在任何项目下）----
async function loadTextImages(): Promise<void> {
  const data = await api<TextImageItem[]>('/text-images')
  for (const t of data) {
    t.url = withCacheBust(t.url)
  }
  textImages.value = data
}

async function loadTextImageOrientations(): Promise<void> {
  if (textImageOrientationsLoaded) return
  try {
    const data = await api<{ orientations: PosterOrientationOption[] }>('/text-images/options')
    textImageOrientations.value = data.orientations
    textImageOrientationsLoaded = true
  } catch {
    // 静默失败：画幅选择器会是空的，不影响提交(后端有默认值)。
  }
}

async function createTextImage(): Promise<void> {
  if (!textImageForm.prompt.trim()) return
  textImageError.value = null
  creatingTextImage.value = true
  try {
    await api('/text-images', {
      method: 'POST',
      body: JSON.stringify({
        prompt: textImageForm.prompt.trim(),
        styleMode: textImageForm.styleMode,
        orientation: textImageForm.orientation,
        characterReferenceImagePaths: textImageRefPathInput.character.trim() || null,
        sceneReferenceImagePaths: textImageRefPathInput.scene.trim() || null
      })
    })
    textImageForm.prompt = ''
    textImageRefPathInput.character = ''
    textImageRefPathInput.scene = ''
    textImagesTab.value = 'list'
    await loadTextImages()
  } catch (err) {
    textImageError.value = err instanceof Error ? err.message : String(err)
  } finally {
    creatingTextImage.value = false
  }
}

async function regenerateTextImage(imageId: string): Promise<void> {
  regeneratingTextImage[imageId] = true
  try {
    await api(`/text-images/${imageId}/regenerate`, { method: 'POST' })
    await loadTextImages()
  } finally {
    regeneratingTextImage[imageId] = false
  }
}

async function deleteTextImage(imageId: string): Promise<void> {
  if (!window.confirm('删除这张图，确定吗？（磁盘上已经生成好的文件不会被删除）')) return
  await api(`/text-images/${imageId}`, { method: 'DELETE' })
  await loadTextImages()
}

// "以此图参考生成图片"：从文生图列表跳到创建表单，带上原提示词(可改)，图片本身先放
// 一边等用户自己决定放进角色参考图还是场景参考图——两类参考图对生成效果影响不同
// (人物长相 vs 环境氛围)，不替用户瞎猜。
const pendingTextImageReference = ref<{ path: string; url: string } | null>(null)

function useTextImageAsReference(item: TextImageItem): void {
  if (!item.filePath) return
  textImageForm.prompt = item.prompt
  pendingTextImageReference.value = { path: item.filePath, url: item.url ?? '' }
  textImagesTab.value = 'create'
}

function addPendingReferenceTo(kind: 'character' | 'scene'): void {
  const pending = pendingTextImageReference.value
  if (!pending) return
  const existing = splitPaths(textImageRefPathInput[kind])
  if (!existing.includes(pending.path)) existing.push(pending.path)
  textImageRefPathInput[kind] = existing.join(',')
  pendingTextImageReference.value = null
}

function dismissPendingTextImageReference(): void {
  pendingTextImageReference.value = null
}

const generatingScene = reactive<Record<string, boolean>>({})
const sceneRefImagePathsInput = reactive<Record<string, string>>({})

async function generateScene(sceneId: string): Promise<void> {
  if (!activeProject.value) return
  generatingScene[sceneId] = true
  try {
    const raw = sceneRefImagePathsInput[sceneId]?.trim()
    const body: Record<string, unknown> = {}
    if (raw) body.referenceImagePaths = raw.split(',').map((s) => s.trim()).filter(Boolean)
    await api(`/scenes/${sceneId}/generate`, { method: 'POST', body: JSON.stringify(body) })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    generatingScene[sceneId] = false
  }
}

// ---- 场景跨项目复用：跟角色复用同一个道理，同一个环境没必要每个项目都重新生成 ----
interface SceneSearchResult extends Scene {
  projectId: string
  projectTitle: string
}

const sceneReuseOpenFor = ref<string | null>(null)
const sceneReuseQuery = reactive<Record<string, string>>({})
const sceneReuseResults = reactive<Record<string, SceneSearchResult[]>>({})
const sceneReuseSearching = reactive<Record<string, boolean>>({})
const sceneReusing = reactive<Record<string, boolean>>({})

function toggleSceneReuseSearch(sceneId: string): void {
  sceneReuseOpenFor.value = sceneReuseOpenFor.value === sceneId ? null : sceneId
  if (sceneReuseOpenFor.value === sceneId && !(sceneId in sceneReuseResults)) {
    searchSceneReuseCandidates(sceneId)
  }
}

async function searchSceneReuseCandidates(sceneId: string): Promise<void> {
  sceneReuseSearching[sceneId] = true
  try {
    const q = (sceneReuseQuery[sceneId] ?? '').trim()
    const params = new URLSearchParams({ excludeSceneId: sceneId })
    if (q) params.set('q', q)
    const data = await api<SceneSearchResult[]>(`/scenes/search?${params.toString()}`)
    sceneReuseResults[sceneId] = data.map((s) => ({ ...s, url: withCacheBust(s.url) }))
  } finally {
    sceneReuseSearching[sceneId] = false
  }
}

async function reuseScene(sceneId: string, sourceSceneId: string): Promise<void> {
  if (!activeProject.value) return
  sceneReusing[sceneId] = true
  try {
    await api(`/scenes/${sceneId}/reuse`, { method: 'POST', body: JSON.stringify({ sourceSceneId }) })
    sceneReuseOpenFor.value = null
    await loadProjectDetail(activeProject.value.id)
  } finally {
    sceneReusing[sceneId] = false
  }
}

async function loadShotAssets(shotId: string): Promise<void> {
  const data = await api<Asset[]>(`/shots/${shotId}/assets`)
  assetsByShot[shotId] = data.map((a) => ({ ...a, url: withCacheBust(a.url) }))
}

function assetOf(shotId: string, type: Asset['type']): Asset | undefined {
  // assetsByShot 里是按 createdAt 倒序排的；批量候选时可能同时存在好几条同类型素材，
  // 优先取 selected=true 的那条（用户已经选定），全都没选中(批量生成完还没点选)时
  // 退化成最新那条，跟后端 _latest_completed_asset_path 的逻辑保持一致。
  const list = assetsByShot[shotId]?.filter((a) => a.type === type) ?? []
  return list.find((a) => a.selected) ?? list[0]
}

function candidatesOf(shotId: string, type: Asset['type']): Asset[] {
  return assetsByShot[shotId]?.filter((a) => a.type === type) ?? []
}

async function selectAsset(shotId: string, type: Asset['type'], assetId: string): Promise<void> {
  await api(`/shots/${shotId}/${type}/${assetId}/select`, { method: 'POST' })
  await loadShotAssets(shotId)
}

function genButtonLabel(shotId: string, type: Asset['type'], firstLabel: string, againLabel: string): string {
  return assetOf(shotId, type) ? againLabel : firstLabel
}

function modelLabel(providerId: string | null | undefined, model: string | null | undefined): string | null {
  // IndexTTS 没有真正的"模型"概念，model 字段存的是服务地址，没必要在 UI 上把 IP 甩出来。
  if (!providerId) return null
  if (providerId === 'indextts') return 'IndexTTS'
  return model ? `${providerId} · ${model}` : providerId
}

// ---- 角色库 ----
async function loadCharacters(projectId: string): Promise<void> {
  const data = await api<CharacterRef[]>(`/projects/${projectId}/characters`)
  characters.value = data.map((c) => ({ ...c, url: withCacheBust(c.url) }))
}

async function generateCharacter(characterId: string): Promise<void> {
  if (!activeProject.value) return
  generatingCharacter[characterId] = true
  try {
    await api(`/characters/${characterId}/generate`, { method: 'POST' })
    await loadCharacters(activeProject.value.id)
  } finally {
    generatingCharacter[characterId] = false
  }
}

// 角色的外观描述(prompt)改完先保存，用户再手动点"重新生成设定图"——不自动触发生成，
// 免得每敲一个字就悄悄重新调一次 Seedream 烧配额。
async function saveCharacterPrompt(c: CharacterRef): Promise<void> {
  await api(`/characters/${c.id}`, { method: 'PATCH', body: JSON.stringify({ prompt: c.prompt ?? '' }) })
}

// ---- 角色跨项目复用：同一个角色没必要在每个新项目里重新调一次生成接口 ----
interface CharacterSearchResult extends CharacterRef {
  projectId: string
  projectTitle: string
}

const reuseOpenFor = ref<string | null>(null) // 当前哪个角色卡片展开了"复用已有角色"搜索框
const reuseQuery = reactive<Record<string, string>>({})
const reuseResults = reactive<Record<string, CharacterSearchResult[]>>({})
const reuseSearching = reactive<Record<string, boolean>>({})
const reusing = reactive<Record<string, boolean>>({})

function toggleReuseSearch(characterId: string): void {
  reuseOpenFor.value = reuseOpenFor.value === characterId ? null : characterId
  if (reuseOpenFor.value === characterId && !(characterId in reuseResults)) {
    searchReuseCandidates(characterId)
  }
}

async function searchReuseCandidates(characterId: string): Promise<void> {
  reuseSearching[characterId] = true
  try {
    const q = (reuseQuery[characterId] ?? '').trim()
    const params = new URLSearchParams({ excludeCharacterId: characterId })
    if (q) params.set('q', q)
    const data = await api<CharacterSearchResult[]>(`/characters/search?${params.toString()}`)
    reuseResults[characterId] = data.map((c) => ({ ...c, url: withCacheBust(c.url) }))
  } finally {
    reuseSearching[characterId] = false
  }
}

async function reuseCharacter(characterId: string, sourceCharacterId: string): Promise<void> {
  if (!activeProject.value) return
  reusing[characterId] = true
  try {
    await api(`/characters/${characterId}/reuse`, {
      method: 'POST',
      body: JSON.stringify({ sourceCharacterId })
    })
    reuseOpenFor.value = null
    await loadCharacters(activeProject.value.id)
  } finally {
    reusing[characterId] = false
  }
}

function backToProjects(): void {
  view.value = 'projects'
  activeProject.value = null
}

async function generateStory(): Promise<void> {
  if (!activeProject.value) return
  generatingStory.value = true
  try {
    await api(`/projects/${activeProject.value.id}/story/generate`, { method: 'POST' })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    generatingStory.value = false
  }
}

// ---- 手动加剧本 ----
// 除了调 claude CLI 自动生成，还想给一个"完全自己写"的口子：格式跟 claude 生成的
// scenes 一样(见 story_generator.py 的 PROMPT_TEMPLATE)，用户可以照着这个格式手写/
// 用其它工具批量产出后直接粘贴进来，一次性导入一整份剧本，不用一镜一镜手动点加。
const showImportBox = ref(false)
const importMode = ref<'append' | 'replace'>('append')
const importJsonText = ref('')
const importing = ref(false)
const importError = ref<string | null>(null)

// ---- 剧本表格总览：跟"生成/导入"是同一个「剧本」步骤下的两个视图，不是新数据源，
// 改字段走的还是 saveScene/saveShot 那两个接口。场次不做折叠/展开——用户明确反馈过
// 这里不需要能收起，全部平铺显示，一次看完所有场次和镜头才是这个视图存在的意义。
// 默认显示哪个视图不是固定的：还没有剧本(scenes 为空)时应该默认停在"生成/导入"，
// 引导先把剧本弄出来；已经有剧本了才默认打开"表格总览"直接看/改内容——不然新项目
// 打开就是一张空表格加一行提示文字，看着莫名其妙。下面两个 watch 分别覆盖
// "打开/切换项目那一刻"和"剧本生成完成那一刻"这两个真正会改变默认视图判断的时机，
// 不会在项目内其它跟剧本无关的刷新(比如轮询)时打断用户手动选的视图。 ----
const storyViewMode = ref<'edit' | 'table'>('edit')
watch(
  () => activeProject.value?.id,
  () => {
    storyViewMode.value = (activeProject.value?.scenes.length ?? 0) > 0 ? 'table' : 'edit'
  }
)
watch(
  () => activeProject.value?.story?.status,
  (newStatus, oldStatus) => {
    if (oldStatus === 'running' && newStatus === 'completed') {
      storyViewMode.value = 'table'
    }
  }
)

const IMPORT_TEMPLATE = `{
  "scenes": [
    {
      "summary": "第一场：清晨的老宅院子",
      "shots": [
        {
          "sceneType": "远景",
          "drawPrompt": "晨雾中的老宅院子，青石板路，光影斑驳",
          "motionPrompt": "缓慢推镜，从院门推向廊下",
          "dialogue": "今天天气不错。",
          "durationSec": 5,
          "characterName": "阿明"
        }
      ]
    }
  ]
}`

function fillImportTemplate(): void {
  importJsonText.value = IMPORT_TEMPLATE
}

async function importStory(): Promise<void> {
  if (!activeProject.value) return
  importError.value = null
  let body: unknown
  try {
    body = JSON.parse(importJsonText.value)
  } catch (err) {
    importError.value = `JSON 格式错误：${err instanceof Error ? err.message : String(err)}`
    return
  }
  if (
    importMode.value === 'replace' &&
    !window.confirm('replace 模式会先删掉这个项目已有的所有场次/镜头(和已生成的图片视频)，确定吗？')
  ) {
    return
  }
  importing.value = true
  try {
    await api(`/projects/${activeProject.value.id}/story/import`, {
      method: 'POST',
      body: JSON.stringify({ ...(body as Record<string, unknown>), mode: importMode.value })
    })
    importJsonText.value = ''
    showImportBox.value = false
    await loadProjectDetail(activeProject.value.id)
    // 导入是同步接口，成功回来这一刻剧本已经有内容了——不用等 story.status 的
    // running→completed 那个 watch(那是给"生成剧本"异步任务用的)，这里直接切表格总览。
    if ((activeProject.value?.scenes.length ?? 0) > 0) {
      storyViewMode.value = 'table'
    }
  } catch (err) {
    importError.value = err instanceof Error ? err.message : String(err)
  } finally {
    importing.value = false
  }
}

// ---- 手动增删场次/镜头 ----
const addingScene = ref(false)
const addingShot = reactive<Record<string, boolean>>({})

async function addScene(): Promise<void> {
  if (!activeProject.value) return
  addingScene.value = true
  try {
    await api(`/projects/${activeProject.value.id}/scenes`, {
      method: 'POST',
      body: JSON.stringify({ summary: '' })
    })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    addingScene.value = false
  }
}

async function deleteScene(sceneId: string): Promise<void> {
  if (!activeProject.value) return
  if (!window.confirm('删除这一整场，连同里面所有镜头和已生成的素材，确定吗？')) return
  await api(`/scenes/${sceneId}`, { method: 'DELETE' })
  await loadProjectDetail(activeProject.value.id)
}

async function saveScene(scene: Scene): Promise<void> {
  await api(`/scenes/${scene.id}`, { method: 'PATCH', body: JSON.stringify({ summary: scene.summary }) })
}

async function addShot(sceneId: string): Promise<void> {
  if (!activeProject.value) return
  addingShot[sceneId] = true
  try {
    await api(`/scenes/${sceneId}/shots`, { method: 'POST', body: JSON.stringify({ drawPrompt: '' }) })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    addingShot[sceneId] = false
  }
}

async function addShotAfter(scene: Scene, afterShotId: string): Promise<void> {
  if (!activeProject.value) return
  addingShot[scene.id] = true
  try {
    const created = await api<Shot>(`/scenes/${scene.id}/shots`, {
      method: 'POST',
      body: JSON.stringify({ drawPrompt: '' })
    })
    const ids = scene.shots.map((shot) => shot.id)
    const afterIndex = ids.indexOf(afterShotId)
    ids.splice(afterIndex + 1, 0, created.id)
    await api(`/scenes/${scene.id}/shots/reorder`, {
      method: 'PATCH',
      body: JSON.stringify({ shotIds: ids })
    })
    activeShotId.value = created.id
    await loadProjectDetail(activeProject.value.id)
  } finally {
    addingShot[scene.id] = false
  }
}

async function deleteShot(shotId: string): Promise<void> {
  if (!activeProject.value) return
  if (!window.confirm('删除这一镜，连同已生成的素材，确定吗？')) return
  await api(`/shots/${shotId}`, { method: 'DELETE' })
  await loadProjectDetail(activeProject.value.id)
}

// ---- 场次网格 + 常驻详情面板（主详式布局） ----
// 之前试过用弹窗装详情，反馈是"把网格完全遮住了，编辑时跟总览断开了"——弹窗这类
// 一次性浮层更适合偶尔用一下就关掉的场景，不适合"要长时间在这场戏里来回改字段/
// 生成素材"的编辑场景。改成左右分栏：左边始终是缩略图网格，点哪张卡片，右边常驻
// 面板就显示/编辑那一场，网格不会被挡住，点相邻场次也不用先关掉什么。
// 分镜脚本本身是严格线性的（一场接一场，没有分支/合并），没有必要做 n8n 那种
// 节点连线画布——那类画布的价值在"表达非线性的数据流向"，用在这里纯属多余的
// 平移/缩放/连线交互成本，专业分镜工具（Boords、StudioBinder）用的也是网格/胶片条，
// 不是节点图。
const activeSceneIndex = ref<number | null>(null)

const activeScene = computed(() =>
  activeSceneIndex.value !== null ? (activeProject.value?.scenes[activeSceneIndex.value] ?? null) : null
)
const canPrevScene = computed(() => activeSceneIndex.value !== null && activeSceneIndex.value > 0)
const canNextScene = computed(
  () =>
    activeSceneIndex.value !== null &&
    activeProject.value != null &&
    activeSceneIndex.value < activeProject.value.scenes.length - 1
)

function selectScene(index: number): void {
  activeSceneIndex.value = index
  ensureValidShotSelection()
}

function activateScene(index: number): void {
  selectScene(index)
}

function prevScene(): void {
  if (canPrevScene.value && activeSceneIndex.value !== null) activeSceneIndex.value -= 1
  ensureValidShotSelection()
}

function nextScene(): void {
  if (canNextScene.value && activeSceneIndex.value !== null) activeSceneIndex.value += 1
  ensureValidShotSelection()
}

// 上一场/下一场按钮上直接标出场次号，不用来回点了才知道跳到第几场。
function sceneLabelAt(offset: number): string {
  if (activeSceneIndex.value === null || !activeProject.value) return ''
  const target = activeProject.value.scenes[activeSceneIndex.value + offset]
  return target ? `第${target.order + 1}场` : ''
}

function handleSceneKeydown(e: KeyboardEvent): void {
  if (activeSceneIndex.value === null) return
  // 光标在输入框/文本域里时不拦截左右键，不然打字时选区移动会被误当成"切场次"
  const target = e.target as HTMLElement | null
  if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
  if (e.key === 'ArrowLeft') prevScene()
  else if (e.key === 'ArrowRight') nextScene()
}

// 有场次但还没选中任何一场时（比如刚生成完剧本），默认选第一场，右边详情面板不会
// 空着；场次被删掉导致下标越界时，往前收缩到合法范围内，不清空选择。
function ensureValidSceneSelection(): void {
  const scenes = activeProject.value?.scenes ?? []
  if (scenes.length === 0) {
    activeSceneIndex.value = null
  } else if (activeSceneIndex.value === null) {
    activeSceneIndex.value = 0
  } else if (activeSceneIndex.value >= scenes.length) {
    activeSceneIndex.value = scenes.length - 1
  }
}

// 场次卡片上要显示"这一场镜头出片进度"，跟 shotsStepStatus 同一个统计口径，只是范围
// 缩小到单个场次。展示成缩略图下方的 "已出片/总数" 比例，没出全就标红提醒。
function sceneShotDoneCount(scene: Scene): number {
  return scene.shots.filter((s) => assetOf(s.id, 'video')?.status === 'completed').length
}

function sceneProgressRatio(scene: Scene): string {
  return `${sceneShotDoneCount(scene)}/${scene.shots.length}`
}

function sceneProgressComplete(scene: Scene): boolean {
  return scene.shots.length > 0 && sceneShotDoneCount(scene) === scene.shots.length
}

// 详情面板里的镜头胶片条：跟场次网格同一个道理，选中哪个镜头才展开哪个的完整编辑表单，
// 不再把这场戏所有镜头的表单一次性堆出来。
const activeShotId = ref<string | null>(null)

function ensureValidShotSelection(): void {
  const shots = activeScene.value?.shots ?? []
  if (shots.length === 0) {
    activeShotId.value = null
  } else if (!shots.some((s) => s.id === activeShotId.value)) {
    activeShotId.value = shots[0].id
  }
}

// 镜头行点头部展开/收起：跟场次同一个手风琴逻辑，一次只完整展开一个镜头的表单——
// 生成的视频是竖屏的，媒体预览做成瘦高竖版之后，如果一场戏里所有镜头同时全展开，
// 页面会被拉得很长，所以保留"点哪个才展开哪个"，只是外观从胶片条换成纵向行。
function toggleShot(shotId: string): void {
  activeShotId.value = activeShotId.value === shotId ? null : shotId
}

// ---- 批量操作：参考 liblib.tv 那类节点画布工具里的"框选一堆节点，弹出批量生成/下载
// 工具条"——只在当前展开的这一场戏里选镜头，不跨场次，跟手风琴"一次只开一场"的结构
// 保持一致，不用额外处理"多场戏同时有选中状态"的复杂交互。批量下载先不做，
// 这版只做批量触发生成（图片/视频/配音），沿用逐个 generateAsset 的机制。 ----
const selectedShotIds = reactive<Record<string, boolean>>({})

function isShotSelected(shotId: string): boolean {
  return !!selectedShotIds[shotId]
}

function toggleShotSelection(shotId: string): void {
  selectedShotIds[shotId] = !selectedShotIds[shotId]
}

function selectedShotCount(scene: Scene): number {
  return scene.shots.filter((s) => selectedShotIds[s.id]).length
}

function selectAllShots(scene: Scene): void {
  for (const s of scene.shots) selectedShotIds[s.id] = true
}

function clearShotSelection(scene: Scene): void {
  for (const s of scene.shots) delete selectedShotIds[s.id]
}

const batchGenerating = reactive<Record<string, boolean>>({})

async function batchGenerateAsset(scene: Scene, kind: 'image' | 'video' | 'voice'): Promise<void> {
  const targets = scene.shots.filter((s) => selectedShotIds[s.id])
  if (targets.length === 0) return
  const key = `${scene.id}:${kind}`
  batchGenerating[key] = true
  try {
    // 并发触发即可——每个 /shots/{id}/{kind} 请求本身只是"起一个后台线程"，立刻返回，
    // 真正的生成进度靠已有的 3 秒轮询(pollTimer/hasRunningWork)统一刷新，不用在这里等全部做完。
    await Promise.all(targets.map((s) => generateAsset(s.id, kind)))
  } finally {
    batchGenerating[key] = false
  }
}

// 生成状态统一配色：红=未完成(待处理)、蓝=生成中、绿=已完成、红=失败——未完成和失败
// 都用红色提醒需要处理，用在生成按钮、预览框边框、缩略行的状态点上，样式互相呼应。
function statusColorClass(status: string | null | undefined): string {
  switch (status) {
    case 'running':
      return 'status-running'
    case 'completed':
      return 'status-completed'
    case 'failed':
      return 'status-failed'
    default:
      return 'status-pending'
  }
}

// 项目生命周期(draft/active/archived)跟上面生成状态是两套语义，不能直接复用红/蓝/绿
// 那一套(archived 不是"失败"，draft 也不是"待处理失败")，单独配一套色。
function projectStatusColorClass(status: string): string {
  switch (status) {
    case 'active':
      return 'tag-status-active'
    case 'archived':
      return 'tag-status-archived'
    default:
      return 'tag-status-draft'
  }
}

function projectStatusLabel(status: string): string {
  switch (status) {
    case 'active':
      return '进行中'
    case 'archived':
      return '已归档'
    case 'draft':
      return '草稿'
    default:
      return status
  }
}

// ---- 场次/镜头拖拽排序 ----
// 用原生 HTML5 拖放，不引入额外的拖拽库：卡片数量不大（一部漫剧几十场/几百镜封顶），
// 原生 drag events 足够用，也省得为了这一个功能多引入一个依赖。
const draggedSceneId = ref<string | null>(null)
const dragOverSceneId = ref<string | null>(null)
const draggedShotId = ref<string | null>(null)
const dragOverShotId = ref<string | null>(null)

async function reorderScenes(targetSceneId: string): Promise<void> {
  dragOverSceneId.value = null
  if (!activeProject.value || !draggedSceneId.value || draggedSceneId.value === targetSceneId) {
    draggedSceneId.value = null
    return
  }
  const ids = activeProject.value.scenes.map((s) => s.id)
  const fromIdx = ids.indexOf(draggedSceneId.value)
  const toIdx = ids.indexOf(targetSceneId)
  draggedSceneId.value = null
  if (fromIdx === -1 || toIdx === -1) return
  const draggedId = ids.splice(fromIdx, 1)[0]
  ids.splice(toIdx, 0, draggedId)
  const projectId = activeProject.value.id
  await api(`/projects/${projectId}/scenes/reorder`, { method: 'PATCH', body: JSON.stringify({ sceneIds: ids }) })
  await loadProjectDetail(projectId)
}

async function moveScene(sceneId: string, direction: -1 | 1): Promise<void> {
  if (!activeProject.value) return
  const ids = activeProject.value.scenes.map((scene) => scene.id)
  const fromIndex = ids.indexOf(sceneId)
  const toIndex = fromIndex + direction
  if (fromIndex < 0 || toIndex < 0 || toIndex >= ids.length) return
  ;[ids[fromIndex], ids[toIndex]] = [ids[toIndex], ids[fromIndex]]
  await api(`/projects/${activeProject.value.id}/scenes/reorder`, {
    method: 'PATCH',
    body: JSON.stringify({ sceneIds: ids })
  })
  activeSceneIndex.value = toIndex
  await loadProjectDetail(activeProject.value.id)
}

async function reorderShots(targetShotId: string): Promise<void> {
  dragOverShotId.value = null
  if (!activeScene.value || !draggedShotId.value || draggedShotId.value === targetShotId) {
    draggedShotId.value = null
    return
  }
  const ids = activeScene.value.shots.map((s) => s.id)
  const fromIdx = ids.indexOf(draggedShotId.value)
  const toIdx = ids.indexOf(targetShotId)
  const sceneId = activeScene.value.id
  draggedShotId.value = null
  if (fromIdx === -1 || toIdx === -1) return
  const draggedId = ids.splice(fromIdx, 1)[0]
  ids.splice(toIdx, 0, draggedId)
  if (!activeProject.value) return
  await api(`/scenes/${sceneId}/shots/reorder`, { method: 'PATCH', body: JSON.stringify({ shotIds: ids }) })
  await loadProjectDetail(activeProject.value.id)
}

// 拖拽排序不够直观（容易漏点），跟场次的"← 前移 / 后移 →"一个道理，给镜头也加一对
// 显式的移动按钮，复用同一个 reorder 接口，交换相邻两个镜头的位置。
async function moveShot(scene: Scene, shotId: string, direction: -1 | 1): Promise<void> {
  const ids = scene.shots.map((s) => s.id)
  const fromIndex = ids.indexOf(shotId)
  const toIndex = fromIndex + direction
  if (fromIndex < 0 || toIndex < 0 || toIndex >= ids.length) return
  ;[ids[fromIndex], ids[toIndex]] = [ids[toIndex], ids[fromIndex]]
  await api(`/scenes/${scene.id}/shots/reorder`, { method: 'PATCH', body: JSON.stringify({ shotIds: ids }) })
  if (!activeProject.value) return
  await loadProjectDetail(activeProject.value.id)
}

// ---- 分镜与运镜手册（用户上传的参考手册，内嵌成静态可搜索数据，见 cinematography.ts） ----
// 写画面描述/景别/运镜描述时经常要用到手册里的专业术语，之前只能切出去开那份 md 文件对照着抄。
// 现在把手册内容直接放进系统里，搜索到想要的词点「复制」，粘贴回对应输入框即可。
// 手册本身是左侧一级导航独立的一个 view('manual')，不挂在具体项目下面，不管有没有
// 打开项目都能看；跟"分镜"步骤里镜头字段旁边那些直接取 CINEMATOGRAPHY_MANUAL 数据
// 拼装的下拉菜单是两套互不影响的东西，改这里不会动到镜头字段下拉的逻辑。
const manualTab = ref(CINEMATOGRAPHY_MANUAL[0].id)
const manualSearch = ref('')
const manualCopiedKey = ref<string | null>(null)

const activeManualTab = computed(() => CINEMATOGRAPHY_MANUAL.find((t) => t.id === manualTab.value) ?? CINEMATOGRAPHY_MANUAL[0])

const filteredManualEntries = computed(() => {
  const keyword = manualSearch.value.trim().toLowerCase()
  if (!keyword) return activeManualTab.value.entries
  return activeManualTab.value.entries.filter(
    (e) => e.zh.toLowerCase().includes(keyword) || e.en.toLowerCase().includes(keyword)
  )
})

async function copyManualText(text: string, key: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    manualCopiedKey.value = key
    setTimeout(() => {
      if (manualCopiedKey.value === key) manualCopiedKey.value = null
    }, 1500)
  } catch {
    // 极少数环境剪贴板权限不可用，静默失败即可，不影响别的功能
  }
}

// ---- 镜头字段下面的手册下拉：选完直接拼进输入框，拼完还是普通文本框，能接着手动改 ----
// 跟上面搜索面板的"复制"是两条路：搜索面板适合翻阅/查找不确定要用哪个词的场景，
// 这里的下拉是"确定要给这个字段加术语了"时更快的路径，不用来回切面板复制粘贴。
interface ManualOptionGroup {
  label: string
  options: ManualEntry[]
}

function buildManualOptionGroups(tabIds: string[]): ManualOptionGroup[] {
  const groups: ManualOptionGroup[] = []
  const indexByLabel = new Map<string, ManualOptionGroup>()
  for (const tabId of tabIds) {
    const tab = CINEMATOGRAPHY_MANUAL.find((t) => t.id === tabId)
    if (!tab) continue
    for (const entry of tab.entries) {
      const label = entry.sub ? `${tab.label} · ${entry.sub}` : tab.label
      let group = indexByLabel.get(label)
      if (!group) {
        group = { label, options: [] }
        indexByLabel.set(label, group)
        groups.push(group)
      }
      group.options.push(entry)
    }
  }
  return groups
}

// 景别只跟"景别"这一类术语相关；画面描述偏静态视觉(构图/灯光/调色/焦距景深/导演风格)；
// 运镜描述偏动态(运镜/转场/镜头功能剪辑)。这样分类比把整本手册一股脑塞进一个下拉更好用。
const sceneTypeManualGroups = buildManualOptionGroups(['shotSize'])
const emotionManualGroups = buildManualOptionGroups(['emotion'])
const drawPromptManualGroups = buildManualOptionGroups(['composition', 'lighting', 'color', 'lens', 'director'])
const motionPromptManualGroups = buildManualOptionGroups(['movement', 'transition', 'editing'])
// 转场衔接下拉专用：只取手册"转场"这一类的50个词条，给镜头胶片条之间的连接点用，
// 跟运镜描述里那个更大的下拉（movement+transition+editing 混合）是两个不同的场景。
const transitionManualGroups = buildManualOptionGroups(['transition'])

// 图生视频只有一个"画面/运镜描述"输入框（不像分镜里画面描述/运镜描述是两个分开的字段），
// 所以这里把 drawPrompt 那几类(构图/灯光/调色/焦距/导演风格)和 motionPrompt 那几类
// (运镜/转场/镜头功能剪辑)合并成一个下拉，覆盖"分镜与运镜手册"里跟生成提示词相关的
// 全部类别；情绪/景别是结构化字段的概念，负面词条是排除性质的，这两者都不适合直接拼进
// 一段正向的生成提示词，所以不放进来。
const videoGenManualGroups = buildManualOptionGroups([
  'composition',
  'lighting',
  'color',
  'lens',
  'director',
  'movement',
  'transition',
  'editing'
])

function onVideoGenManualSelectChange(event: Event): void {
  const select = event.target as HTMLSelectElement
  const value = select.value
  select.value = ''
  if (!value) return
  const current = videoGenForm.prompt.trim()
  videoGenForm.prompt = current ? `${current}，${value}` : value
}

type ShotTextField = 'sceneType' | 'drawPrompt' | 'motionPrompt' | 'emotion'

function appendManualTerm(shot: Shot, field: ShotTextField, value: string): void {
  if (!value) return
  const current = (shot[field] ?? '').trim()
  shot[field] = current ? `${current}，${value}` : value
  saveShot(shot)
}

function onManualSelectChange(shot: Shot, field: ShotTextField, event: Event): void {
  const select = event.target as HTMLSelectElement
  const value = select.value
  select.value = ''
  appendManualTerm(shot, field, value)
}

// 转场连接点小徽标只有 30px 宽，只放得下英文短语；hover 提示想看完整中文说明，
// 得反查一下这个英文值在手册"转场"分类里对应哪一条。
function transitionZhLabel(en: string | null): string | null {
  if (!en) return null
  for (const g of transitionManualGroups) {
    const hit = g.options.find((opt) => opt.en === en)
    if (hit) return hit.zh
  }
  return en
}

// 镜头之间的转场连接点要放进窄窄的 tab 间隙里，手册里的中文说明太长（带编号和破折号
// 详细描述），这里只取名字本身，比如"1. 甩镜转场 — 快速横向摇摄..."只取"甩镜转场"。
function transitionShortLabel(en: string | null): string | null {
  const full = transitionZhLabel(en)
  if (!full) return null
  return full.replace(/^\d+\.\s*/, '').split(' — ')[0]
}

// 转场衔接：选完不只是存个标签，还得自动拼进运镜描述——不然 Seedance 生成视频时根本
// 不知道"这一镜结尾要接一个叠化"，画面上就不会真的做出这个转场动作。清空(选"（不设置）")
// 时只清标签，不去动已经写进运镜描述里的文字（用户可能已经手动改过那句话了）。
// 注意：这里故意存空字符串而不是 null——PATCH /shots/{id} 的语义是"传 null(不传这个键)就跳过
// 这个字段"，只有传空字符串才会真的把数据库里已有的转场标签清空。
function setShotTransition(shot: Shot, value: string): void {
  shot.transitionToNext = value
  if (value) {
    const marker = `转场至下一镜：${value}`
    const current = (shot.motionPrompt ?? '').trim()
    if (!current.includes(marker)) {
      shot.motionPrompt = current ? `${current}；${marker}` : marker
    }
  }
  saveShot(shot)
}

async function saveShot(shot: Shot): Promise<void> {
  await api(`/shots/${shot.id}`, {
    method: 'PATCH',
    body: JSON.stringify({
      sceneType: shot.sceneType,
      drawPrompt: shot.drawPrompt,
      motionPrompt: shot.motionPrompt,
      dialogue: shot.dialogue,
      characterName: shot.characterName,
      transitionToNext: shot.transitionToNext,
      emotion: shot.emotion,
      // durationSec 之前一直漏在这个 PATCH 之外——字段本身早就有，界面上却从没能改过，
      // 纯属遗漏，这次一起补上。
      durationSec: shot.durationSec
    })
  })
}

async function generateAsset(
  shotId: string,
  kind: 'image' | 'video' | 'voice',
  count = 1
): Promise<void> {
  generatingAsset[`${shotId}:${kind}`] = true
  try {
    const body: Record<string, unknown> = {}
    if (kind === 'image') {
      const raw = refImagePathsInput[shotId]?.trim()
      if (raw) body.referenceImagePaths = raw.split(',').map((s) => s.trim()).filter(Boolean)
      if (count > 1) body.count = count
    } else if (kind === 'video') {
      const raw = startImagePathInput[shotId]?.trim()
      if (raw) body.startImagePath = raw
    } else if (kind === 'voice') {
      const opts = voiceOptionsInput[shotId]
      if (opts?.referenceAudioPath) body.referenceAudioPath = opts.referenceAudioPath
      if (opts?.voiceId) body.voiceId = opts.voiceId
      if (opts?.speed) body.speed = opts.speed
    }
    await api(`/shots/${shotId}/${kind}`, { method: 'POST', body: JSON.stringify(body) })
    await loadShotAssets(shotId)
  } finally {
    generatingAsset[`${shotId}:${kind}`] = false
  }
}

// 删掉某一镜已生成的素材（不删镜头本身）：比如配错音了想清掉重来，或者这一镜暂时
// 不需要配音/视频了，删掉之后这一镜的状态会退回"待处理"。
const deletingAsset = reactive<Record<string, boolean>>({})

async function deleteShotAsset(shotId: string, kind: 'image' | 'video' | 'voice'): Promise<void> {
  deletingAsset[`${shotId}:${kind}`] = true
  try {
    await api(`/shots/${shotId}/${kind}`, { method: 'DELETE' })
    await loadShotAssets(shotId)
  } finally {
    deletingAsset[`${shotId}:${kind}`] = false
  }
}

// 台词清空了之后字幕就不会再拿这一镜的旧台词去对时间轴——跟"这一镜没视频会被跳过"配合，
// 手动清掉不想要的台词，比留着一句对不上时间轴的字幕更干净。
function clearDialogue(shot: Shot): void {
  if (!(shot.dialogue ?? '').trim()) return
  shot.dialogue = ''
  saveShot(shot)
}

async function exportProject(): Promise<void> {
  if (!activeProject.value) return
  exporting.value = true
  exportError.value = null
  exportUrl.value = null
  exportFilePath.value = null
  exportSkippedShots.value = []
  try {
    // 显式传 settingsForm.exportBurnSubtitles（设置页保存的默认值，也是这里勾选框绑的值），
    // 不再写死 true——不然设置页的"默认是否烧字幕"选项就白设置了。
    const result = await api<{
      url: string | null
      filePath: string
      skippedShots: { sceneOrder: number; shotOrder: number }[]
    }>(`/projects/${activeProject.value.id}/export`, {
      method: 'POST',
      body: JSON.stringify({
        burnSubtitles: settingsForm.exportBurnSubtitles,
        useBgm: settingsForm.exportUseBgm
      })
    })
    exportUrl.value = result.url
    exportFilePath.value = result.filePath
    exportSkippedShots.value = result.skippedShots ?? []
  } catch (err) {
    exportError.value = err instanceof Error ? err.message : String(err)
  } finally {
    exporting.value = false
  }
}

// 导出前先在页面上提示"哪些镜头还没视频，导出时会被跳过"，不用等点了导出才知道——
// 跟后端"缺视频的镜头直接跳过"是同一套统计口径，纯前端算，不用额外请求。
const missingVideoShots = computed(() => {
  const list: { sceneOrder: number; shotOrder: number }[] = []
  for (const scene of activeProject.value?.scenes ?? []) {
    for (const shot of scene.shots) {
      if (assetOf(shot.id, 'video')?.status !== 'completed') {
        list.push({ sceneOrder: scene.order + 1, shotOrder: shot.order + 1 })
      }
    }
  }
  return list
})

// 导出页那两条"哪些镜头缺视频"的提示，点一下标签直接跳到「分镜」步骤对应的场次/镜头，
// 不用自己在一堆场次里找"第5场镜2"到底是哪一个——sceneOrder/shotOrder 都是显示用的
// 1-based 编号，这里换算回 0-based 跟 scene.order/shot.order 匹配。
function jumpToShot(sceneOrder: number, shotOrder: number): void {
  const scenes = activeProject.value?.scenes ?? []
  const sceneIdx = scenes.findIndex((s) => s.order + 1 === sceneOrder)
  if (sceneIdx === -1) return
  const shot = scenes[sceneIdx].shots.find((s) => s.order + 1 === shotOrder)
  if (!shot) return
  activeStep.value = 'shots'
  activeSceneIndex.value = sceneIdx
  activeShotId.value = shot.id
}

// ---- "总览模式"：跟"编辑模式"(原有 step-tabs + 手风琴)是同一份数据的两种视图，
// 切换不会丢状态——总览模式让用户先一眼扫完参考图、再一眼扫完所有分镜画面，
// 确认没问题了再挑着点批量生成按钮；点具体某张卡片会跳回编辑模式对应的位置，
// 方便临时改一下文字/参考图。默认值：还没有剧本时停在编辑模式（引导先生成/导入
// 剧本，总览模式在没有场次/镜头时是空的没意义），已经有剧本了默认进总览模式。
type ProjectViewMode = 'overview' | 'edit'
const projectViewMode = ref<ProjectViewMode>('edit')
watch(
  () => activeProject.value?.id,
  () => {
    projectViewMode.value = (activeProject.value?.scenes.length ?? 0) > 0 ? 'overview' : 'edit'
  }
)

function jumpToCharactersEdit(): void {
  projectViewMode.value = 'edit'
  activeStep.value = 'characters'
}

function jumpToSceneEdit(sceneIdx: number): void {
  projectViewMode.value = 'edit'
  activeStep.value = 'shots'
  activateScene(sceneIdx)
}

function jumpToShotEdit(sceneOrder: number, shotOrder: number): void {
  projectViewMode.value = 'edit'
  jumpToShot(sceneOrder, shotOrder)
}

// 总览模式的 5 个"一键生成"批量按钮，各自独立触发，互不牵动。跟单项生成
// (generateCharacter/generateAsset 等)共用同一套"点了先请求、请求完立刻刷新一次
// 本地状态"模式——批量接口内部是后台线程跑，接口本身立刻返回 running，这里手动
// 刷新一次拿到"已经被标记成 running"的最新状态，后续进度交给已有的 3 秒轮询
// (startPolling 里的 hasRunningWork 判断)接力，不用另外单独写一套轮询逻辑。
const batchRunning = reactive({
  characters: false,
  scenes: false,
  images: false,
  videos: false,
  voices: false
})

async function generateAllCharacters(): Promise<void> {
  if (!activeProject.value) return
  batchRunning.characters = true
  try {
    await api(`/projects/${activeProject.value.id}/characters/generate-all`, { method: 'POST' })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    batchRunning.characters = false
  }
}

async function generateAllScenes(): Promise<void> {
  if (!activeProject.value) return
  batchRunning.scenes = true
  try {
    await api(`/projects/${activeProject.value.id}/scenes/generate-all`, { method: 'POST' })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    batchRunning.scenes = false
  }
}

async function generateAllShotImages(): Promise<void> {
  if (!activeProject.value) return
  batchRunning.images = true
  try {
    await api(`/projects/${activeProject.value.id}/shots/generate-all-images`, { method: 'POST' })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    batchRunning.images = false
  }
}

async function generateAllShotVideos(): Promise<void> {
  if (!activeProject.value) return
  batchRunning.videos = true
  try {
    await api(`/projects/${activeProject.value.id}/shots/generate-all-videos`, { method: 'POST' })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    batchRunning.videos = false
  }
}

async function generateAllShotVoices(): Promise<void> {
  if (!activeProject.value) return
  batchRunning.voices = true
  try {
    await api(`/projects/${activeProject.value.id}/shots/generate-all-voices`, { method: 'POST' })
    await loadProjectDetail(activeProject.value.id)
  } finally {
    batchRunning.voices = false
  }
}

const hasRunningWork = computed(() => {
  if (activeProject.value?.story?.status === 'running') return true
  if (characters.value.some((c) => c.status === 'running')) return true
  if (activeProject.value?.scenes.some((s) => s.status === 'running')) return true
  return Object.values(assetsByShot).some((assets) => assets.some((a) => a.status === 'running'))
})

// ---- 项目详情页 step 步骤条 ----
// 之前是"锚点跳转 + 长滚动页面"，四块内容一直全部渲染在同一条竖向长页面里，
// 只是点了按钮滚过去。反馈是每块内容想只看自己那一块，滚动页面里其它步骤的内容
// 还占着地方/让人分心。现在改成真正的 tab：同一时间只显示 activeStep 对应的那一块，
// 其它几块直接不渲染。步骤之间还是不锁——经常需要跳回去重新生成某个角色或某一镜，
// 纯线性向导反而添乱，所以这里仍然是"随便点随便跳"，只是从"滚动定位"变成"切换显示"。
type StepId = 'story' | 'characters' | 'shots' | 'export'
const activeStep = ref<StepId>('story')
const premiseExpanded = ref(false)

const storyStepStatus = computed(() => statusLabel(activeProject.value?.story?.status ?? 'pending'))

const charactersStepStatus = computed(() => {
  // 场景参考图也算在这一步里（跟角色库同属"一致性资产"），所以统计口径是
  // 角色 + 场景两边加起来的完成数，不只是角色。
  const total = characters.value.length + (activeProject.value?.scenes.length ?? 0)
  if (total === 0) return '待生成'
  const done =
    characters.value.filter((c) => c.status === 'completed').length +
    (activeProject.value?.scenes.filter((s) => s.status === 'completed').length ?? 0)
  return `${done}/${total}`
})

const shotsStepStatus = computed(() => {
  const shots = activeProject.value?.scenes.flatMap((s) => s.shots) ?? []
  if (shots.length === 0) return '待生成'
  const done = shots.filter((s) => assetOf(s.id, 'video')?.status === 'completed').length
  return `${done}/${shots.length} 镜已出片`
})

// 优先看本次会话里刚导出的结果(exportUrl/exportFilePath)，没有的话落回项目自己的
// lastExportedAt(后端持久化的"最近一次导出成功时间")——不然重新打开项目/重启应用后，
// 明明之前导出过，这里也会因为 exportUrl 是内存态被清空而显示"待导出"，不准确。
const exportStepStatus = computed(() =>
  exportUrl.value || exportFilePath.value || activeProject.value?.lastExportedAt ? '已导出' : '待导出'
)

function startPolling(): void {
  pollTimer = setInterval(async () => {
    // 后端健康检查独立于项目轮询：Electron 拉起 ai-service 子进程需要一点时间，
    // 之前只在 mounted 时查一次，赶上后端还没起来就会永远卡在 "error"，
    // 哪怕后面接口调用其实都是通的。这里持续重试，恢复了就更新回 ok。
    await checkHealth()

    // 海报是独立页面，跟项目详情的轮询分开判断：只要海报列表页里还有 running 状态的
    // 海报(生成中)，不管当前在不在项目详情页都继续刷，直到生成完/失败为止。
    if (posters.value.some((p) => p.status === 'running')) {
      await loadPosters()
    }
    if (videoGenList.value.some((v) => v.status === 'running')) {
      await loadVideoGenerations()
    }
    if (textImages.value.some((t) => t.status === 'running')) {
      await loadTextImages()
    }

    if (view.value !== 'project' || !activeProject.value) return
    if (!hasRunningWork.value) return
    const id = activeProject.value.id
    await loadProjectDetail(id)
  }, 3000)
}

async function waitForBackendThenLoad(): Promise<void> {
  // 数据其实一直好好地存在 SQLite 里，不会因为重启就消失。但 Electron 启动时要先
  // 拉起 ai-service 子进程（FastAPI 启动需要几秒），如果这时候就去请求
  // /projects、/settings，会连接失败；之前这两个请求失败了就直接放弃且不重试
  // （.catch(() => {})），界面上表现出来就是"项目列表空了"——其实只是没请求成功，
  // 不是数据丢了。这里先等后端就绪，再去加载，并且失败会重试。
  for (let i = 0; i < 30; i++) {
    await checkHealth()
    if (apiStatus.value === 'ok') break
    await new Promise((resolve) => setTimeout(resolve, 1000))
  }
  await Promise.all([
    loadSettings().catch(() => {}),
    loadProjects().catch(() => {}),
    loadPosters().catch(() => {}),
    loadPosterOrientations().catch(() => {}),
    loadPosterTemplates().catch(() => {}),
    loadVideoGenerations().catch(() => {}),
    loadTextImages().catch(() => {}),
    loadTextImageOrientations().catch(() => {}),
    loadMediaRatios().catch(() => {})
  ])
}

onMounted(() => {
  waitForBackendThenLoad()
  refreshUpdateStatus().catch(() => {})
  removeUpdateStatusListener = aiManjuBridge?.onUpdateStatus?.((status) => {
    updateStatus.value = status
  }) ?? null
  startPolling()
  window.addEventListener('keydown', handleSceneKeydown)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  removeUpdateStatusListener?.()
  window.removeEventListener('keydown', handleSceneKeydown)
})

function statusLabel(status: string): string {
  return { pending: '待生成', running: '生成中…', completed: '已完成', failed: '失败' }[status] ?? status
}
</script>

<template>
  <div class="shell" :class="`ui-${uiVersion}`">
    <aside class="sidebar">
      <div class="sidebar-brand" title="AI视频工作台" aria-label="AI视频工作台">
        <img class="sidebar-brand-logo" :src="appLogoUrl" alt="" />
      </div>
      <nav class="sidebar-nav">
        <div class="sidebar-nav-group">
        <span class="sidebar-nav-label">短剧</span>
        <button
          :class="{ active: view === 'projects' && projectsTab === 'create' }"
          @click="view = 'projects'; projectsTab = 'create'"
        >
          <FileVideo2 class="nav-icon" aria-hidden="true" /><span>新建短剧</span>
        </button>
        <button
          :class="{ active: (view === 'projects' && projectsTab === 'list') || view === 'project' }"
          @click="view = 'projects'; projectsTab = 'list'"
        >
          <ListVideo class="nav-icon" aria-hidden="true" /><span>短剧列表</span>
        </button>
        <button :class="{ active: view === 'manual' }" @click="view = 'manual'"><BookOpen class="nav-icon" aria-hidden="true" /><span>分镜与运镜手册</span></button>
        </div>

        <!-- 海报：独立的一级功能，不挂在任何视频项目下面。 -->
        <div class="sidebar-nav-group">
        <span class="sidebar-nav-label">海报</span>
        <button
          :class="{ active: view === 'posters' && postersTab === 'create' }"
          @click="view = 'posters'; postersTab = 'create'"
        >
          <Wand2 class="nav-icon" aria-hidden="true" /><span>新建海报</span>
        </button>
        <button
          :class="{ active: view === 'posters' && postersTab === 'list' }"
          @click="view = 'posters'; postersTab = 'list'"
        >
          <Images class="nav-icon" aria-hidden="true" /><span>海报列表</span>
        </button>
        </div>

        <!-- 文生图：独立的一级功能，写一段描述直接出图，不做标题文字合成。 -->
        <div class="sidebar-nav-group">
        <span class="sidebar-nav-label">文生图</span>
        <button
          :class="{ active: view === 'textImages' && textImagesTab === 'create' }"
          @click="view = 'textImages'; textImagesTab = 'create'"
        >
          <Wand2 class="nav-icon" aria-hidden="true" /><span>新建文生图</span>
        </button>
        <button
          :class="{ active: view === 'textImages' && textImagesTab === 'list' }"
          @click="view = 'textImages'; textImagesTab = 'list'"
        >
          <Images class="nav-icon" aria-hidden="true" /><span>文生图列表</span>
        </button>
        </div>

        <div class="sidebar-nav-group">
        <span class="sidebar-nav-label">视频</span>
        <button
          :class="{ active: view === 'videoGen' && videoGenTab === 'create' }"
          @click="view = 'videoGen'; videoGenTab = 'create'"
        >
          <Clapperboard class="nav-icon" aria-hidden="true" /><span>图生视频</span>
        </button>
        <button
          :class="{ active: view === 'videoGen' && videoGenTab === 'list' }"
          @click="view = 'videoGen'; videoGenTab = 'list'"
        >
          <ListVideo class="nav-icon" aria-hidden="true" /><span>视频列表</span>
        </button>
        </div>

        <div class="sidebar-nav-group sidebar-nav-group-settings">
        <span class="sidebar-nav-label">设置</span>
        <button :class="{ active: view === 'settings' }" @click="view = 'settings'"><Settings class="nav-icon" aria-hidden="true" /><span>设置</span></button>
        </div>
      </nav>
      <div v-if="activeProject" class="sidebar-current">
        <span class="hint">当前项目</span>
        <strong>{{ activeProject.title }}</strong>
      </div>
      <div class="sidebar-footer">
        <span class="api-status" :class="apiStatus">后端 {{ apiStatus }}</span>
      </div>
    </aside>

    <main class="main-content">
    <div v-if="openFileError" class="file-open-toast">
      {{ openFileError }}
      <button @click="openFileError = null">知道了</button>
    </div>
    <!-- 设置页 -->
    <section v-if="view === 'settings'" class="panel settings-page">
      <button v-if="activeProject" class="back" @click="view = 'project'">← 返回「{{ activeProject.title }}」</button>
      <div class="settings-page-head compact-page-head">
        <div><h1>设置</h1><p class="hint">模型、剧本、配音、导出和提示词</p></div>
        <span class="settings-state" :class="settingsInfo.arkApiKeySet ? 'ready' : 'missing'">
          {{ settingsInfo.arkApiKeySet ? 'API 已配置' : 'API 未配置' }}
        </span>
      </div>

      <div class="manual-tabs settings-tabs">
        <button
          v-for="tab in SETTINGS_TABS"
          :key="tab.id"
          class="manual-tab"
          :class="{ active: settingsTab === tab.id }"
          @click="settingsTab = tab.id"
        >
          {{ tab.label }}
        </button>
      </div>

      <template v-if="settingsTab === 'about'">
      <section class="settings-group settings-group-update">
      <div class="settings-group-head"><div><h2>版本更新</h2><p>检查 GitHub Release 并安装新版</p></div><span>{{ updateStatus.currentVersion }}</span></div>
      <div class="update-card">
        <div>
          <strong>{{ updateStatus.message }}</strong>
          <p class="hint">
            当前版本：{{ updateStatus.currentVersion }}
            <template v-if="updateStatus.latestVersion"> · 最新版本：{{ updateStatus.latestVersion }}</template>
          </p>
          <div v-if="updateStatus.state === 'downloading'" class="update-progress">
            <span :style="{ width: `${Math.max(0, Math.min(100, updateStatus.percent ?? 0))}%` }"></span>
          </div>
          <p v-if="updateStatus.state === 'error'" class="field-help">
            如果自动更新失败，请打开 Release 手动下载。
          </p>
        </div>
        <div class="update-actions">
          <button :disabled="updateActionDisabled" @click="runUpdatePrimaryAction">
            {{ updatePrimaryActionLabel }}
          </button>
          <button class="ghost" @click="openLatestRelease">打开 Release</button>
        </div>
      </div>
      </section>
      </template>

      <template v-if="settingsTab === 'general'">
      <section class="settings-group settings-group-primary">
      <div class="settings-group-head"><div><h2>火山方舟模型配置</h2><p>配置 Seedream 出图和 Seedance 视频</p></div><span>必填</span></div>
      <div class="field">
        <label>火山方舟 API Key <span class="field-badge">Seedream / Seedance</span></label>
        <input
          v-model="settingsForm.arkApiKey"
          type="password"
          :placeholder="settingsInfo.arkApiKeySet ? `当前已设置：${settingsInfo.arkApiKeyMasked}` : '还没设置'"
        />
      </div>
      <div class="field">
        <label>Ark Base URL</label>
        <input v-model="settingsForm.arkBaseUrl" placeholder="https://ark.cn-beijing.volces.com/api/plan/v3" />
        <p class="field-help">Plan 套餐可留空；按量账号填 <code>.../api/v3</code>。</p>
      </div>
      <div class="field">
        <label>Seedance 视频模型 ID</label>
        <input v-model="settingsForm.arkVideoModel" placeholder="doubao-seedance-2.0" />
        <p class="field-help">留空用默认模型；不确定时填控制台里的推理接入点 <code>ep-xxxx</code>。</p>
        <details class="settings-doc"><summary>模型 ID 怎么填？</summary>
          <ul class="hint model-ref-hint">
            <li>Plan 套餐：<code>doubao-seedance-2.0</code> / <code>fast</code> / <code>mini</code>。</li>
            <li>按量账号：填控制台快照 ID 或 <code>ep-xxxxxxxx</code>。</li>
            <li>具体可用型号以火山控制台「模型广场」为准。</li>
          </ul>
        </details>
      </div>
      <div class="field">
        <label>Seedream 图片模型 ID</label>
        <input v-model="settingsForm.arkImageModel" placeholder="doubao-seedream-5.0-lite" />
        <p class="field-help">留空用默认模型；追求质量可改 pro。</p>
        <details class="settings-doc"><summary>模型 ID 怎么填？</summary>
          <ul class="hint model-ref-hint">
            <li>Plan 套餐：<code>doubao-seedream-5.0-lite</code> 或 <code>pro</code>。</li>
            <li>按量账号：填控制台快照 ID 或 <code>ep-xxxxxxxx</code>。</li>
            <li>具体可用型号以火山控制台「模型广场」为准。</li>
          </ul>
        </details>
      </div>
      <div class="field">
        <label>Ark 文本对话模型 ID <span class="field-badge">"AI优化提示词"用</span></label>
        <input v-model="settingsForm.arkTextModel" placeholder="比如 doubao-seed-1.6" />
        <p class="field-help">留空则使用「AI生成剧本配置」；填写后优先走 Ark。</p>
      </div>
      </section>
      </template>

      <template v-if="settingsTab === 'indextts'">
      <section class="settings-group settings-group-primary">
      <div class="settings-group-head"><div><h2>IndexTTS 配音配置</h2><p>配置本地或局域网配音服务</p></div><span>可选</span></div>
      <div class="field">
        <label>IndexTTS 服务地址 <span class="field-badge">局域网配音</span></label>
        <input v-model="settingsForm.indexTtsBaseUrl" placeholder="http://localhost:7860" />
        <p class="field-help">不配置也能生成剧本、图片和视频。</p>
      </div>
      </section>
      </template>

      <template v-if="settingsTab === 'story'">
      <section class="settings-group settings-group-primary">
      <div class="settings-group-head">
        <div><h2>AI生成剧本配置</h2><p>选择本机 Claude 或第三方 API</p></div>
        <span>必填其一</span>
      </div>
      <div class="field story-gen-provider-field">
        <label>生成方式</label>
        <div class="story-gen-provider-tabs" role="tablist" aria-label="剧本生成方式">
          <button
            type="button"
            class="story-gen-provider-tab"
            :class="{ active: storyGenForm.provider === 'claude_cli' }"
            role="tab"
            :aria-selected="storyGenForm.provider === 'claude_cli'"
            @click="storyGenForm.provider = 'claude_cli'"
          >
            <strong>本机 claude CLI</strong>
            <span>适合已安装 Claude Code</span>
          </button>
          <button
            type="button"
            class="story-gen-provider-tab"
            :class="{ active: storyGenForm.provider === 'api' }"
            role="tab"
            :aria-selected="storyGenForm.provider === 'api'"
            @click="storyGenForm.provider = 'api'"
          >
            <strong>第三方 API</strong>
            <span>不依赖本机终端</span>
          </button>
        </div>
      </div>

      <template v-if="storyGenForm.provider === 'claude_cli'">
        <div class="field">
          <label>claude CLI 路径 <span class="field-badge">可选</span></label>
          <div class="cli-path-row">
            <input
              v-model="storyGenForm.cliPath"
              placeholder="留空 = 自动检测"
            />
            <button class="ghost" type="button" :disabled="storyGenCliDetect.detecting" @click="detectStoryGenCliPath">
              {{ storyGenCliDetect.detecting ? '检测中…' : '自动检测' }}
            </button>
          </div>
          <p v-if="storyGenCliDetect.message" class="field-help">{{ storyGenCliDetect.message }}</p>
          <p class="field-help">一般留空。找不到时点「自动检测」，或粘贴 claude 完整路径。</p>
        </div>
      </template>

      <div v-if="storyGenForm.provider === 'claude_cli'" class="story-gen-test-block">
        <button class="ghost terminal-test-button" :disabled="storyGenCliTest.testing" @click="testStoryGenCli">
          {{ storyGenCliTest.testing ? '测试中…' : '测试本机终端是否生效' }}
        </button>
        <p v-if="storyGenCliTest.result" :class="['story-gen-test-result', storyGenCliTest.result.ok ? 'ok' : 'error']">
          {{ storyGenCliTest.result.ok ? '✓ ' : '✗ ' }}{{ storyGenCliTest.result.message }}
        </p>
        <p class="field-help">会调用一次本机 <code>claude</code>，用来确认桌面 App 能找到命令。</p>
      </div>

      <template v-if="storyGenForm.provider === 'api'">
        <div class="field">
          <label>Base URL</label>
          <input v-model="storyGenForm.apiBaseUrl" placeholder="https://your-proxy.example.com/api" />
          <p class="field-help">填写到 <code>/v1/messages</code> 之前的地址。</p>
        </div>
        <div class="field">
          <label>API Key</label>
          <input
            v-model="storyGenForm.apiKey"
            type="password"
            :placeholder="storyGenInfo.apiKeySet ? `当前已设置：${storyGenInfo.apiKeyMasked}` : '还没设置'"
          />
        </div>
        <div class="field">
          <label>模型名</label>
          <input v-model="storyGenForm.apiModel" placeholder="比如 claude-sonnet-5" />
        </div>
        <div class="field">
          <label>最大输出 Token</label>
          <input v-model.number="storyGenForm.apiMaxTokens" type="number" min="1" max="200000" style="max-width: 160px" />
        </div>
        <div class="story-gen-test-block">
          <button class="ghost" :disabled="storyGenApiTest.testing" @click="testStoryGenApi">
            {{ storyGenApiTest.testing ? '测试中…' : '测试连通性' }}
          </button>
          <p v-if="storyGenApiTest.result" :class="['story-gen-test-result', storyGenApiTest.result.ok ? 'ok' : 'error']">
            {{ storyGenApiTest.result.ok ? '✓ ' : '✗ ' }}{{ storyGenApiTest.result.message }}
          </p>
          <p class="field-help">会用当前表单测试；API Key 留空则沿用已保存的。</p>
        </div>
      </template>
      </section>
      </template>

      <template v-if="settingsTab === 'generation'">
      <section class="settings-group">
      <div class="settings-group-head"><div><h2>生成目录</h2><p>设置素材保存位置</p></div><span>可选</span></div>
      <div class="field">
        <label>生成产物目录</label>
        <input v-model="settingsForm.outputDir" placeholder="留空 = 默认目录" />
        <p class="field-help">留空使用默认 <code>output</code>；自定义路径必须存在且可写。</p>
      </div>
      </section>

      <section class="settings-group">
      <div class="settings-group-head"><div><h2>导出</h2><p>设置成片、字幕和背景音乐</p></div><span>可选</span></div>
      <div class="field">
        <label>导出目录</label>
        <input v-model="settingsForm.exportDir" placeholder="留空 = 默认目录" />
        <p class="field-help">留空时保存到生成产物目录的 <code>projects/项目ID</code> 下。</p>
      </div>
      <div class="field checkbox-field">
        <label><input type="checkbox" v-model="settingsForm.exportBurnSubtitles" /> 导出时默认烧录字幕</label>
      </div>
      <div class="field">
        <label>背景音乐文件<span class="field-badge">可选</span></label>
        <input v-model="settingsForm.exportBgmPath" placeholder="本地音频文件路径，比如 bgm.mp3" />
        <p class="field-help">会循环叠加到成片音轨下方，支持 mp3/wav/aac 等格式。</p>
      </div>
      <div class="field">
        <label>背景音乐音量<span class="field-badge">相对成片原音轨，0~1</span></label>
        <input
          v-model.number="settingsForm.exportBgmVolume"
          type="number"
          min="0"
          max="1"
          step="0.05"
          style="max-width: 120px"
        />
      </div>
      <div class="field checkbox-field">
        <label>
          <input type="checkbox" v-model="settingsForm.exportUseBgm" :disabled="!settingsForm.exportBgmPath" />
          导出时默认加背景音乐{{ settingsForm.exportBgmPath ? '' : '（先填音乐文件）' }}
        </label>
      </div>
      </section>

      <section class="settings-group">
      <div class="settings-group-head"><div><h2>海报字体</h2><p>中文海报文字渲染用</p></div><span>可选</span></div>
      <div class="field">
        <label>字体文件路径</label>
        <input v-model="settingsForm.posterFontPath" placeholder="留空 = 自动检测" />
        <p class="field-help">
          留空自动使用系统中文字体；失败时再填写字体文件路径。
        </p>
      </div>
      </section>
      </template>

      <template v-if="settingsTab === 'prompts'">
      <section class="settings-group">
      <div class="settings-group-head"><div><h2>出图风格前缀</h2><p>按风格补充生图 prompt</p></div><span>可选</span></div>
      <div class="field" v-for="mode in (['comic', 'realistic', 'render3d', 'freeform'] as StyleMode[])" :key="`prefix-${mode}`">
        <label>{{ STYLE_MODE_LABELS[mode] }}</label>
        <textarea v-model="customStylePrefixesForm[mode]" rows="2" :placeholder="`留空 = ${BUILTIN_STYLE_PREFIXES[mode] || '（无前缀）'}`" />
      </div>
      </section>

      <section class="settings-group">
      <div class="settings-group-head"><div><h2>剧本写作风格提示</h2><p>影响 AI 写分镜时的画风描述</p></div><span>可选</span></div>
      <div class="field" v-for="mode in (['comic', 'realistic', 'render3d', 'freeform'] as StyleMode[])" :key="`hint-${mode}`">
        <label>{{ STYLE_MODE_LABELS[mode] }}</label>
        <textarea v-model="customStyleHintsForm[mode]" rows="2" :placeholder="`留空 = ${BUILTIN_STYLE_HINTS[mode]}`" />
      </div>
      </section>

      <section class="settings-group">
      <div class="settings-group-head"><div><h2>内容类型提示</h2><p>控制是否需要固定角色</p></div><span>可选</span></div>
      <div class="field" v-for="ct in (['character', 'no_character'] as ContentType[])" :key="`ct-${ct}`">
        <label>{{ CONTENT_TYPE_LABELS[ct] }}</label>
        <textarea v-model="customContentTypeHintsForm[ct]" rows="2" :placeholder="`留空 = ${BUILTIN_CONTENT_TYPE_HINTS[ct]}`" />
      </div>
      </section>

      <section class="settings-group">
      <div class="settings-group-head">
        <div><h2>项目类型模板</h2><p>自定义新建项目页的模板卡片</p></div>
        <span>可选</span>
      </div>
      <div class="custom-template-rows">
        <div v-for="(row, idx) in customProjectTemplatesForm" :key="idx" class="custom-template-row">
          <input v-model="row.label" placeholder="卡片标题，比如：地产带看Vlog" />
          <input v-model="row.description" placeholder="卡片说明，比如：无固定角色 · 真人风" />
          <select v-model="row.contentType">
            <option value="character">人物剧情</option>
            <option value="no_character">无固定角色</option>
          </select>
          <select v-model="row.styleMode">
            <option value="comic">漫画风</option>
            <option value="realistic">真人风</option>
            <option value="render3d">3D风</option>
            <option value="freeform">AI自由发挥</option>
          </select>
          <button class="ghost" @click="removeCustomProjectTemplateRow(idx)">删除</button>
        </div>
        <p v-if="customProjectTemplatesForm.length === 0" class="hint">未添加自定义模板，当前使用内置模板。</p>
      </div>
      <p class="field-help">不添加自定义行 = 使用内置模板；添加任意行 = 替换内置模板。</p>
      <button class="ghost" @click="addCustomProjectTemplateRow">+ 添加一张模板卡片</button>
      </section>
      </template>

      <div class="settings-actions">
        <div><strong>保存设置</strong><span v-if="settingsSavedAt" class="hint">上次保存：{{ settingsSavedAt }}</span><p v-if="settingsError" class="error">保存失败：{{ settingsError }}</p></div>
        <button :disabled="settingsSaving" @click="saveSettings">{{ settingsSaving ? '保存中…' : '保存全部设置' }}</button>
      </div>
    </section>

    <!-- 分镜与运镜手册：纯静态参考数据(见 cinematography.ts)，不依赖具体项目，放在左侧
         一级导航里，不管有没有打开项目、打开的是哪个项目都能随时查——之前挂在项目详情页
         的步骤条里，没打开项目时反而看不了，不太合理。 -->
    <section v-else-if="view === 'manual'" class="panel manual-page">
      <button v-if="activeProject" class="back" @click="view = 'project'">← 返回「{{ activeProject.title }}」</button>
      <div class="manual-page-head compact-page-head">
        <div><h1>分镜与运镜手册</h1><p class="hint">查找专业术语并复制到镜头设置</p></div>
        <span class="manual-result-count">{{ filteredManualEntries.length }} 条</span>
      </div>
      <details class="manual-page-help">
        <summary>使用说明</summary>
        <p class="hint">景别/运镜/转场/构图/灯光/调色/焦距景深/负面词/导演风格，搜词后点「复制」，切回项目里粘贴进对应字段——
          镜头字段旁边的下拉菜单里也能直接选同样的术语插入，这里只是方便整体查阅/搜索。</p>
      </details>
      <div class="manual-box">
        <div class="manual-search-row">
          <input v-model="manualSearch" class="manual-search" placeholder="搜索中文/英文关键词，比如：黄金时刻 / orbit" />
          <button v-if="manualSearch" class="ghost" @click="manualSearch = ''">清空搜索</button>
        </div>
        <div class="manual-tabs">
          <button
            v-for="tab in CINEMATOGRAPHY_MANUAL"
            :key="tab.id"
            class="manual-tab"
            :class="{ active: manualTab === tab.id }"
            @click="manualTab = tab.id"
          >
            {{ tab.label }}
          </button>
        </div>
        <div class="manual-list">
          <div v-for="(entry, idx) in filteredManualEntries" :key="idx" class="manual-entry">
            <div class="manual-entry-text">
              <span v-if="entry.sub" class="tag manual-sub">{{ entry.sub }}</span>
              <strong>{{ entry.zh }}</strong>
              <code>{{ entry.en }}</code>
              <span v-if="entry.extra" class="hint">{{ entry.extra }}</span>
            </div>
            <button class="ghost manual-copy" @click="copyManualText(entry.en, `${manualTab}-${idx}`)">
              {{ manualCopiedKey === `${manualTab}-${idx}` ? '已复制' : '复制' }}
            </button>
          </div>
          <p v-if="filteredManualEntries.length === 0" class="hint">没搜到相关词条，换个关键词试试</p>
        </div>
      </div>
    </section>

    <!-- 项目列表 -->
    <section v-else-if="view === 'projects'" class="panel projects-page">
      <div class="projects-page-head compact-page-head">
        <div>
          <h1>{{ projectsTab === 'create' ? '新建短剧' : '短剧列表' }}</h1>
          <p class="hint">{{ projectsTab === 'create' ? '选一个模板，一句话说清楚想要什么' : '创建和管理 AI 短剧项目' }}</p>
        </div>
        <button v-if="projectsTab === 'list'" class="refresh" @click="loadProjects">刷新列表</button>
      </div>

      <template v-if="projectsTab === 'create'">
        <div class="project-create-panel">
        <div class="project-create-head"><div><h2>新建项目</h2><p class="hint">选择最接近的项目模板，之后仍可调整</p></div><span>两步完成</span></div>
        <div class="field-row project-create-form">
          <div class="project-create-config">
            <div class="project-create-step-title"><span>1</span><div><strong>选择项目模板</strong><small>自动配置内容类型与出图风格</small></div></div>
            <div class="project-template-grid">
              <button
                v-for="tpl in effectiveProjectTemplates"
                :key="tpl.id"
                class="project-template-card"
                :class="{ active: newTemplate === tpl.id }"
                @click="applyProjectTemplate(tpl.id)"
              >
                <span class="project-template-check">{{ newTemplate === tpl.id ? '✓' : '' }}</span>
                <strong>{{ tpl.label }}</strong>
                <small>{{ tpl.description }}</small>
              </button>
            </div>
            <div v-if="newTemplate === 'custom'" class="project-custom-options">
            <div class="style-mode-picker">
              <span class="style-mode-label">内容类型</span>
              <label><input type="radio" value="character" v-model="newContentType" /> 人物剧情</label>
              <label><input type="radio" value="no_character" v-model="newContentType" /> 无固定角色（风光/氛围/产品）</label>
            </div>
            <div class="style-mode-picker">
              <span class="style-mode-label">出图风格</span>
              <label><input type="radio" value="comic" v-model="newStyleMode" /> 漫画风</label>
              <label><input type="radio" value="realistic" v-model="newStyleMode" /> 真人风</label>
              <label><input type="radio" value="render3d" v-model="newStyleMode" /> 3D风</label>
              <label><input type="radio" value="freeform" v-model="newStyleMode" /> AI自由发挥</label>
            </div>
            </div>
            <!-- 生成比例是独立于模板之外的第三个轴，不管选哪个模板都单独选，
                 这部剧所有分镜图片/视频都会用这个比例(见 Project.aspectRatio)。 -->
            <div class="style-mode-picker">
              <span class="style-mode-label">生成比例</span>
              <label v-for="r in mediaRatios" :key="r.id">
                <input type="radio" :value="r.id" v-model="newAspectRatio" /> {{ r.label }}
              </label>
            </div>
          </div>
          <div class="project-create-step-title"><span>2</span><div><strong>填写项目内容</strong><small>用一句话说明想要生成的故事或视频</small></div></div>
          <div class="project-create-input-row">
            <textarea v-model="newPremise" placeholder="一句话故事简介，比如：雨夜里久别重逢的两个人" rows="2" />
            <button :disabled="creatingProject || !canCreateProject" @click="createProject">
              {{ creatingProject ? '创建中…' : '创建' }}
            </button>
          </div>
        </div>
        <p class="hint">内容类型和出图风格创建后仍可修改，但已经写好的剧本和生成过的图片不会自动重新生成。</p>
        </div>
      </template>

      <template v-else>
        <div class="list-head project-list-head">
          <h2 style="margin: 0">项目列表</h2>
          <span class="hint">{{ projects.length }} 个项目</span>
        </div>
        <p v-if="projects.length === 0" class="hint">
          还没有项目，先切到「新建项目」创建一个吧（如果你确定之前创建过，点"刷新列表"再看看）
        </p>
        <ul class="project-list">
          <li v-for="(p, idx) in projects" :key="p.id" @click="editingProjectId === p.id ? undefined : openProject(p.id)">
            <span class="project-index">{{ idx + 1 }}</span>
            <input
              v-if="editingProjectId === p.id"
              :ref="(el) => { if (el) (el as HTMLInputElement).focus() }"
              v-model="editingProjectTitle"
              class="project-title-input"
              @click.stop
              @keyup.enter="saveProjectTitle(p)"
              @keyup.esc="cancelEditProjectTitle"
              @blur="saveProjectTitle(p)"
            />
            <strong v-else class="project-title-text">{{ p.title }}</strong>
            <span class="tag" :class="projectStatusColorClass(p.status)">{{ projectStatusLabel(p.status) }}</span>
            <span class="tag tag-content-type">{{ contentTypeLabels[p.contentType] }}</span>
            <span class="tag tag-style-mode">{{ styleModeLabels[p.styleMode] }}</span>
            <span class="tag tag-style-mode">{{ ratioLabel(p.aspectRatio) }}</span>
            <span v-if="p.lastExportedAt" class="tag tag-exported">已导出</span>
            <div class="project-actions" @click.stop>
              <button
                v-if="editingProjectId !== p.id"
                class="ghost project-title-edit"
                title="改标题"
                @click.stop="startEditProjectTitle(p)"
              >改标题</button>
              <button class="ghost danger project-delete" @click.stop="deleteProject(p.id)">删除</button>
            </div>
          </li>
        </ul>
      </template>
    </section>

    <!-- 海报：独立的一级功能，不挂在任何视频项目下面，建海报不需要先建视频项目、
         写完剧本才能出海报。跟"项目"页同一套 list/create 两个 tab 的结构。 -->
    <section v-else-if="view === 'posters'" class="panel posters-page">
      <div class="projects-page-head compact-page-head">
        <div>
          <h1>{{ postersTab === 'create' ? '新建海报' : '海报列表' }}</h1>
          <p class="hint">{{ postersTab === 'create' ? '选朝向和类型模版（或自己写提示词），填标题，AI 只画背景，文字是程序渲染叠上去的' : '所有生成过的海报，不分项目' }}</p>
        </div>
        <button v-if="postersTab === 'list'" class="refresh" @click="loadPosters">刷新列表</button>
      </div>

      <template v-if="postersTab === 'create'">
        <div class="poster-create-panel">
          <div class="poster-create-layout">
          <div class="poster-form-main">
          <section class="poster-form-section">
          <div class="poster-step-head"><span>1</span><div><strong>选择版式</strong><small>确定画布方向和海报类型</small></div></div>
          <div class="field">
            <label>画布方向</label>
            <div class="poster-orientation-picker">
              <label v-for="o in posterOrientations" :key="o.id">
                <input type="radio" :value="o.id" v-model="posterForm.orientation" /> {{ o.label }}
              </label>
            </div>
          </div>

          <div class="field">
            <label>海报类型<span class="field-badge">选择模板会自动配置背景提示词和排版</span></label>
            <div class="poster-preset-grid">
              <button
                v-for="tpl in posterTemplates"
                :key="tpl.id"
                type="button"
                class="project-template-card poster-template-card"
                :class="{ active: posterForm.templateId === tpl.id }"
                @click="posterForm.templateId = tpl.id"
              >
                <span class="project-template-check">{{ posterForm.templateId === tpl.id ? '✓' : '' }}</span>
                <strong>{{ tpl.label }}</strong>
                <span class="ghost danger poster-template-delete" title="删除模版" @click.stop="deletePosterTemplate(tpl.id)">×</span>
              </button>
              <button
                type="button"
                class="project-template-card"
                :class="{ active: posterForm.templateId === '' }"
                @click="posterForm.templateId = ''"
              >
                <span class="project-template-check">{{ posterForm.templateId === '' ? '✓' : '' }}</span>
                <strong>自定义</strong>
              </button>
            </div>
          </div>
          </section>

          <template v-if="!posterForm.templateId">
            <section class="poster-custom-template">
            <div class="field">
              <label>提示词<span class="field-badge">描述这张海报的背景要画什么</span></label>
              <textarea v-model="posterForm.promptText" rows="3" placeholder="比如：海外医美诊所前台，专业整洁的接待场景" />
              <div class="ai-optimize-row">
                <button
                  type="button"
                  class="ghost ai-optimize-button"
                  :disabled="promptOptimizeState('posterPrompt').optimizing"
                  @click="optimizePromptField('posterPrompt', posterForm.promptText, '海报背景画面描述', (v) => (posterForm.promptText = v))"
                >
                  {{ promptOptimizeState('posterPrompt').optimizing ? '优化中…' : 'AI优化提示词' }}
                </button>
              </div>
              <p v-if="promptOptimizeState('posterPrompt').error" class="ai-optimize-error">{{ promptOptimizeState('posterPrompt').error }}</p>
            </div>
            <div class="field">
              <label>排版方式</label>
              <div class="style-mode-picker">
                <label><input type="radio" value="title" v-model="posterForm.layoutMode" /> 标题+副标题</label>
                <label><input type="radio" value="textBlocks" v-model="posterForm.layoutMode" /> 多行正文（价格表/知识点）</label>
              </div>
            </div>
            <div class="field">
              <button v-if="!showSaveTemplateForm" class="ghost" :disabled="!posterForm.promptText.trim()" @click="showSaveTemplateForm = true">
                保存为模版，下次直接选
              </button>
              <div v-else class="ref-path-row">
                <input v-model="newTemplateLabel" placeholder="模版名称，比如：地陪介绍海报" />
                <button :disabled="savingPosterTemplate || !newTemplateLabel.trim()" @click="saveCurrentPosterAsTemplate">
                  {{ savingPosterTemplate ? '保存中…' : '保存' }}
                </button>
                <button class="ghost" @click="showSaveTemplateForm = false">取消</button>
              </div>
            </div>
            </section>
          </template>

          <section class="poster-form-section">
          <div class="poster-step-head"><span>2</span><div><strong>填写海报文字</strong><small>文字由程序排版，中文不会交给 AI 绘制</small></div></div>
          <div class="field">
            <label>标题</label>
            <input v-model="posterForm.title" placeholder="比如：2026年医美项目价格表" />
          </div>
          <div class="field">
            <label>副标题<span class="field-badge">可选</span></label>
            <input v-model="posterForm.subtitle" placeholder="比如：仅供参考，以到院咨询为准" />
          </div>
          <div class="field" v-if="effectivePosterLayoutMode === 'textBlocks'">
            <label>正文<span class="field-badge">每行一条，价格类可写"项目名|价格"，价格会自动右对齐</span></label>
            <textarea
              v-model="posterForm.bodyLinesText"
              rows="5"
              placeholder="双眼皮手术|8000元起&#10;热玛吉全脸|12000元起&#10;水光针基础款|1500元起"
            />
          </div>
          </section>

          <details class="poster-visual-settings">
            <summary><span class="poster-step-number">3</span><div><strong>画面与参考设置</strong><small>出图风格、补充描述和参考图</small></div></summary>
            <div class="poster-visual-settings-body">
              <div class="style-mode-picker">
                <span class="style-mode-label">出图风格</span>
                <label><input type="radio" value="comic" v-model="posterForm.styleMode" /> 漫画风</label>
                <label><input type="radio" value="realistic" v-model="posterForm.styleMode" /> 真人风</label>
                <label><input type="radio" value="render3d" v-model="posterForm.styleMode" /> 3D风</label>
                <label><input type="radio" value="freeform" v-model="posterForm.styleMode" /> AI自由发挥</label>
              </div>
              <div class="field">
                <label>补充画面描述<span class="field-badge">可选</span></label>
                <textarea v-model="posterForm.extraPrompt" rows="2" placeholder="比如：夜晚，霓虹灯背景，下雨" />
              </div>
              <div class="field">
                <label>参考图<span class="field-badge">可选</span></label>
                <div class="ref-path-row">
                  <input v-model="posterRefPathInput.new" placeholder="本地图片路径，留空纯文生图" />
                  <button class="ghost" @click="pickReferenceFile(posterRefPathInput, 'new', true)">选择文件…</button>
                </div>
                <div class="ref-preview-row">
                  <div v-for="entry in previewEntries(posterRefPathInput.new)" :key="entry.path" class="ref-pick-thumb">
                    <img class="ref-pick-preview" :src="entry.preview" :title="entry.path" />
                    <button
                      type="button"
                      class="ref-pick-remove"
                      title="删除这张参考图"
                      @click="removeReferencePath(posterRefPathInput, 'new', entry.path)"
                    >×</button>
                  </div>
                </div>
              </div>
            </div>
          </details>

          <div class="poster-create-actions">
            <div><strong>准备生成</strong><span>AI 生成无文字背景，标题与正文随后自动排版</span><p v-if="posterError" class="error">{{ posterError }}</p></div>
            <button :disabled="creatingPoster || !posterForm.title.trim()" @click="createPoster">{{ creatingPoster ? '生成中…' : '生成海报' }}</button>
          </div>
          </div>

          <aside class="poster-preview-panel">
            <div class="poster-preview-head"><strong>排版预览</strong><span>{{ posterOrientations.find((o) => o.id === posterForm.orientation)?.label }}</span></div>
            <div class="poster-preview-canvas" :class="`orientation-${posterForm.orientation}`">
              <div class="poster-preview-bg">AI 背景图</div>
              <div class="poster-preview-copy">
                <strong>{{ posterForm.title || '海报标题' }}</strong>
                <span v-if="posterForm.subtitle || effectivePosterLayoutMode === 'title'">{{ posterForm.subtitle || '副标题' }}</span>
                <ul v-if="effectivePosterLayoutMode === 'textBlocks'">
                  <li v-for="(line, idx) in bodyLinesFromText(posterForm.bodyLinesText).slice(0, 6)" :key="idx">{{ line }}</li>
                </ul>
              </div>
            </div>
            <p>预览仅表达文字层级和占位，实际背景由 AI 生成。</p>
          </aside>
          </div>
        </div>
      </template>

      <template v-else>
        <p v-if="posters.length === 0" class="hint">
          还没有海报，先切到「新建海报」创建一个吧（如果你确定之前创建过，点"刷新列表"再看看）
        </p>
        <div class="poster-grid">
          <div v-for="poster in posters" :key="poster.id" class="poster-card">
            <div class="poster-card-media" :class="statusColorClass(poster.status)">
              <img v-if="poster.url" :src="`${apiBaseUrl}${poster.url}`" title="双击用系统程序打开原图" @dblclick="openInSystemViewer(poster.filePath)" />
              <span v-else>{{ poster.status === 'running' ? '生成中…' : statusLabel(poster.status) }}</span>
            </div>
            <div class="poster-card-info">
              <div class="poster-card-title-row">
                <span class="tag tag-style-mode">{{ poster.templateLabel || '自定义' }}</span>
                <span class="tag">{{ poster.orientationLabel }}</span>
                <span class="tag" :class="statusColorClass(poster.status)">{{ statusLabel(poster.status) }}</span>
              </div>
              <template v-if="editingPosterId === poster.id">
                <input v-model="editingPosterForm.title" placeholder="标题" />
                <input v-model="editingPosterForm.subtitle" placeholder="副标题" />
                <textarea
                  v-if="poster.layoutMode === 'textBlocks'"
                  v-model="editingPosterForm.bodyLinesText"
                  rows="4"
                  placeholder="每行一条，可用｜分隔价格"
                />
                <div class="poster-card-actions">
                  <button :disabled="savingPosterText" @click="savePosterText(poster.id)">
                    {{ savingPosterText ? '保存中…' : '保存文字' }}
                  </button>
                  <button class="ghost" @click="cancelEditPosterText">取消</button>
                </div>
              </template>
              <template v-else>
                <strong>{{ poster.title }}</strong>
                <p v-if="poster.subtitle" class="hint">{{ poster.subtitle }}</p>
                <ul v-if="poster.bodyLines && poster.bodyLines.length" class="poster-card-body-lines">
                  <li v-for="(line, idx) in poster.bodyLines" :key="idx">{{ line }}</li>
                </ul>
                <p v-if="poster.error" class="error">{{ poster.error }}</p>
                <div class="poster-card-actions">
                  <button class="ghost" @click="startEditPosterText(poster)">改文字</button>
                  <button class="ghost" :disabled="regeneratingPoster[poster.id]" @click="regeneratePoster(poster.id)">
                    {{ regeneratingPoster[poster.id] ? '生成中…' : '重新生成' }}
                  </button>
                  <button class="ghost danger" @click="deletePoster(poster.id)">删除</button>
                </div>
              </template>
            </div>
          </div>
        </div>
      </template>
    </section>

    <!-- 视频生成：无剧本，上传一张参考图 + 写一段描述，直接调 Seedance 图生视频。
         独立的一级功能，不挂在任何视频项目下面，不经过 Project/Story/Scene/Shot
         结构，也没有配音/字幕/多段拼接——单张参考图进，单条视频出。 -->
    <section v-else-if="view === 'videoGen'" class="panel posters-page">
      <div class="projects-page-head compact-page-head">
        <div>
          <h1>{{ videoGenTab === 'create' ? '图生视频' : '视频列表' }}</h1>
          <p class="hint">{{ videoGenTab === 'create' ? '上传一张参考图 + 写一段画面/运镜描述，直接生成一条视频（不需要先建项目/写剧本）' : '所有生成过的视频，不分项目' }}</p>
        </div>
        <button v-if="videoGenTab === 'list'" class="refresh" @click="loadVideoGenerations">刷新列表</button>
      </div>

      <template v-if="videoGenTab === 'create'">
      <div class="poster-form-main video-gen-form-main">
        <section class="poster-form-section">
          <div class="poster-step-head"><span>1</span><div><strong>参考图</strong><small>作为视频的起始帧</small></div></div>
          <div class="field field-inline">
            <label>参考图<span class="field-badge">必填</span></label>
            <div class="field-inline-content">
              <div class="ref-path-row">
                <input v-model="videoGenRefPathInput.new" placeholder="本地图片路径" />
                <button class="ghost" @click="pickReferenceFile(videoGenRefPathInput, 'new', false)">选择文件…</button>
                <img
                  v-if="pathPreview(videoGenRefPathInput.new)"
                  class="ref-pick-preview ref-pick-preview-inline"
                  :src="pathPreview(videoGenRefPathInput.new) ?? ''"
                />
              </div>
            </div>
          </div>
        </section>

        <section class="poster-form-section">
          <div class="poster-step-head"><span>2</span><div><strong>画面/运镜描述</strong><small>喂给 Seedance 的提示词</small></div></div>
          <div class="field">
            <div class="video-gen-prompt-row">
              <textarea v-model="videoGenForm.prompt" rows="3" placeholder="比如：镜头缓慢推进，女孩转头微笑，微风吹动头发" />
              <select class="manual-inline-select" @change="onVideoGenManualSelectChange">
                <option value="">+ 从手册插入构图/灯光/调色/运镜/转场等术语…</option>
                <optgroup v-for="g in videoGenManualGroups" :key="g.label" :label="g.label">
                  <option v-for="(opt, i) in g.options" :key="i" :value="opt.en">{{ opt.zh }}</option>
                </optgroup>
              </select>
            </div>
            <div class="ai-optimize-row">
              <button
                type="button"
                class="ghost ai-optimize-button"
                :disabled="promptOptimizeState('videoGenPrompt').optimizing"
                @click="optimizePromptField('videoGenPrompt', videoGenForm.prompt, '图生视频的画面/运镜描述', (v) => (videoGenForm.prompt = v))"
              >
                {{ promptOptimizeState('videoGenPrompt').optimizing ? '优化中…' : 'AI优化提示词' }}
              </button>
            </div>
            <p v-if="promptOptimizeState('videoGenPrompt').error" class="ai-optimize-error">{{ promptOptimizeState('videoGenPrompt').error }}</p>
          </div>
        </section>

        <section class="poster-form-section">
          <div class="poster-step-head"><span>3</span><div><strong>生成比例</strong><small>决定这条视频的画幅</small></div></div>
          <div class="field field-inline">
            <label>比例</label>
            <div class="field-inline-content">
              <div class="style-mode-picker text-image-choice-row">
                <label v-for="r in mediaRatios" :key="r.id">
                  <input type="radio" :value="r.id" v-model="videoGenForm.ratio" /> {{ r.label }}
                </label>
              </div>
            </div>
          </div>
        </section>

        <div class="poster-create-actions">
          <div><strong>准备生成</strong><span>4秒 / 720p，跟分镜生成视频同一套时长/分辨率参数</span><p v-if="videoGenError" class="error">{{ videoGenError }}</p></div>
          <button
            :disabled="creatingVideoGen || !videoGenForm.prompt.trim() || !videoGenRefPathInput.new.trim()"
            @click="createVideoGeneration"
          >{{ creatingVideoGen ? '生成中…' : '生成视频' }}</button>
        </div>
      </div>
      </template>

      <template v-else>
        <p v-if="videoGenList.length === 0" class="hint">
          还没有生成过视频，先切到「图生视频」创建一个吧（如果你确定之前生成过，点"刷新列表"再看看）
        </p>
        <div class="poster-grid">
          <div v-for="item in videoGenList" :key="item.id" class="poster-card">
            <div class="poster-card-media" :class="statusColorClass(item.status)">
              <video v-if="item.url" controls :src="`${apiBaseUrl}${item.url}`" @dblclick="openInSystemViewer(item.filePath)" />
              <span v-else>{{ item.status === 'running' ? '生成中…' : statusLabel(item.status) }}</span>
            </div>
            <div class="poster-card-info">
              <div class="poster-card-title-row">
                <span class="tag" :class="statusColorClass(item.status)">{{ statusLabel(item.status) }}</span>
                <span class="tag tag-style-mode">{{ item.ratioLabel }}</span>
              </div>
              <p class="hint">{{ item.prompt }}</p>
              <p v-if="item.error" class="error">{{ item.error }}</p>
              <div class="poster-card-actions">
                <button class="ghost" :disabled="regeneratingVideoGen[item.id]" @click="regenerateVideoGeneration(item.id)">
                  {{ regeneratingVideoGen[item.id] ? '生成中…' : '重新生成' }}
                </button>
                <button class="ghost danger" @click="deleteVideoGeneration(item.id)">删除</button>
              </div>
            </div>
          </div>
        </div>
      </template>
    </section>

    <!-- 文生图：独立的一级功能，写一段描述直接出图，不做标题文字合成，跟海报共用
         出图风格(styleMode)和画幅(orientation)概念。 -->
    <section v-else-if="view === 'textImages'" class="panel posters-page text-images-page">
      <div class="projects-page-head compact-page-head">
        <div>
          <h1>{{ textImagesTab === 'create' ? '新建文生图' : '文生图列表' }}</h1>
          <p class="hint">{{ textImagesTab === 'create' ? '写一段画面描述直接出图，不做任何文字合成' : '所有生成过的图片，不分项目' }}</p>
        </div>
        <button v-if="textImagesTab === 'list'" class="refresh" @click="loadTextImages">刷新列表</button>
      </div>

      <template v-if="textImagesTab === 'create'">
        <div class="text-image-create-panel">
          <section v-if="pendingTextImageReference" class="pending-ref-panel">
            <img :src="pathPreview(pendingTextImageReference.path) ?? pendingTextImageReference.url ?? ''" class="pending-ref-thumb" />
            <div class="pending-ref-info">
              <strong>从"以此图参考生成图片"带过来的图</strong>
              <p class="hint">选这张图要放进哪类参考图，或者不需要就直接关掉。</p>
              <div class="pending-ref-actions">
                <button class="ghost" @click="addPendingReferenceTo('character')">加入角色参考图</button>
                <button class="ghost" @click="addPendingReferenceTo('scene')">加入场景参考图</button>
                <button class="ghost" @click="dismissPendingTextImageReference">不需要，关闭</button>
              </div>
            </div>
          </section>
          <section class="text-image-form-card">
            <div class="poster-step-head"><span>1</span><div><strong>画面描述</strong><small>喂给 Seedream 的提示词</small></div></div>
            <div class="field text-image-prompt-field">
              <label>提示词</label>
              <textarea v-model="textImageForm.prompt" rows="7" placeholder="比如：夜晚城市天台，霓虹灯背景，一只猫坐在栏杆上，电影感，雨后反光地面" />
              <p class="field-help">只描述画面本身。不要在这里写标题文字；这条功能会直接输出 AI 原图。</p>
              <div class="ai-optimize-row">
                <button
                  type="button"
                  class="ghost ai-optimize-button"
                  :disabled="promptOptimizeState('textImagePrompt').optimizing"
                  @click="optimizePromptField('textImagePrompt', textImageForm.prompt, '文生图画面描述', (v) => (textImageForm.prompt = v))"
                >
                  {{ promptOptimizeState('textImagePrompt').optimizing ? '优化中…' : 'AI优化提示词' }}
                </button>
              </div>
              <p v-if="promptOptimizeState('textImagePrompt').error" class="ai-optimize-error">{{ promptOptimizeState('textImagePrompt').error }}</p>
            </div>

            <div class="text-image-options-grid">
              <div class="field">
                <label>画布方向</label>
                <div class="poster-orientation-picker text-image-choice-row">
                  <label v-for="o in textImageOrientations" :key="o.id">
                    <input type="radio" :value="o.id" v-model="textImageForm.orientation" /> {{ o.label }}
                  </label>
                </div>
              </div>
              <div class="field">
                <label>出图风格</label>
                <div class="style-mode-picker text-image-choice-row">
                  <label><input type="radio" value="comic" v-model="textImageForm.styleMode" /> 漫画风</label>
                  <label><input type="radio" value="realistic" v-model="textImageForm.styleMode" /> 真人风</label>
                  <label><input type="radio" value="render3d" v-model="textImageForm.styleMode" /> 3D风</label>
                  <label><input type="radio" value="freeform" v-model="textImageForm.styleMode" /> AI自由发挥</label>
                </div>
              </div>
            </div>

            <div class="field">
              <label>角色参考图<span class="field-badge">可选，多张用逗号分隔</span></label>
              <div class="ref-path-row text-image-ref-row">
                <input v-model="textImageRefPathInput.character" placeholder="本地图片路径；想让画面人物长相/穿着贴近这几张图" />
                <button class="ghost" @click="pickReferenceFile(textImageRefPathInput, 'character', true)">选择文件…</button>
              </div>
              <div v-if="previewEntries(textImageRefPathInput.character).length" class="text-image-ref-preview-wrap">
                <div v-for="entry in previewEntries(textImageRefPathInput.character)" :key="entry.path" class="ref-pick-thumb">
                  <img class="ref-pick-preview text-image-ref-preview" :src="entry.preview" :title="entry.path" />
                  <button
                    type="button"
                    class="ref-pick-remove"
                    title="删除这张参考图"
                    @click="removeReferencePath(textImageRefPathInput, 'character', entry.path)"
                  >×</button>
                </div>
                <span>已选择 {{ splitPaths(textImageRefPathInput.character).length }} 张角色参考图</span>
              </div>
            </div>

            <div class="field">
              <label>环境参考图<span class="field-badge">可选，多张用逗号分隔</span></label>
              <div class="ref-path-row text-image-ref-row">
                <input v-model="textImageRefPathInput.scene" placeholder="本地图片路径；想让画面场景/背景/光线贴近这几张图" />
                <button class="ghost" @click="pickReferenceFile(textImageRefPathInput, 'scene', true)">选择文件…</button>
              </div>
              <div v-if="previewEntries(textImageRefPathInput.scene).length" class="text-image-ref-preview-wrap">
                <div v-for="entry in previewEntries(textImageRefPathInput.scene)" :key="entry.path" class="ref-pick-thumb">
                  <img class="ref-pick-preview text-image-ref-preview" :src="entry.preview" :title="entry.path" />
                  <button
                    type="button"
                    class="ref-pick-remove"
                    title="删除这张参考图"
                    @click="removeReferencePath(textImageRefPathInput, 'scene', entry.path)"
                  >×</button>
                </div>
                <span>已选择 {{ splitPaths(textImageRefPathInput.scene).length }} 张环境参考图</span>
              </div>
            </div>
            <p v-if="!textImageRefPathInput.character && !textImageRefPathInput.scene" class="field-help">
              两种参考图都留空就是纯文生图，全靠画面描述发挥。
            </p>
          </section>

          <aside class="text-image-preview-panel">
            <div class="poster-preview-head">
              <strong>出图预览</strong>
              <span>{{ textImageOrientationLabel }} · {{ styleModeLabels[textImageForm.styleMode] }}</span>
            </div>
            <div class="text-image-preview-canvas" :class="`orientation-${textImageForm.orientation}`">
              <div class="text-image-preview-bg"></div>
              <p>{{ textImagePromptPreview }}</p>
            </div>
            <div class="poster-create-actions text-image-create-actions">
              <div><strong>准备生成</strong><span>生成结果就是 AI 原图，不做标题合成</span><p v-if="textImageError" class="error">{{ textImageError }}</p></div>
              <button :disabled="creatingTextImage || !textImageForm.prompt.trim()" @click="createTextImage">
                {{ creatingTextImage ? '生成中…' : '生成图片' }}
              </button>
            </div>
          </aside>
        </div>
      </template>

      <template v-else>
        <p v-if="textImages.length === 0" class="hint">
          还没有生成过图片，先切到「新建文生图」创建一个吧（如果你确定之前生成过，点"刷新列表"再看看）
        </p>
        <div class="poster-grid text-image-grid">
          <div v-for="item in textImages" :key="item.id" class="poster-card text-image-card">
            <div class="poster-card-media text-image-card-media" :class="[statusColorClass(item.status), `orientation-${item.orientation}`]">
              <img v-if="item.url" :src="`${apiBaseUrl}${item.url}`" title="双击用系统程序打开原图" @dblclick="openInSystemViewer(item.filePath)" />
              <span v-else>{{ item.status === 'running' ? '生成中…' : statusLabel(item.status) }}</span>
            </div>
            <div class="poster-card-info">
              <div class="poster-card-title-row">
                <span class="tag tag-style-mode">{{ styleModeLabels[item.styleMode] }}</span>
                <span class="tag">{{ item.orientationLabel }}</span>
                <span class="tag" :class="statusColorClass(item.status)">{{ statusLabel(item.status) }}</span>
              </div>
              <p class="hint">{{ item.prompt }}</p>
              <p v-if="item.error" class="error">{{ item.error }}</p>
              <div class="poster-card-actions">
                <button class="ghost" :disabled="regeneratingTextImage[item.id]" @click="regenerateTextImage(item.id)">
                  {{ regeneratingTextImage[item.id] ? '生成中…' : '重新生成' }}
                </button>
                <button class="ghost" :disabled="!item.filePath" @click="useTextImageAsReference(item)">
                  以此图参考生成图片
                </button>
                <button class="ghost danger" @click="deleteTextImage(item.id)">删除</button>
              </div>
            </div>
          </div>
        </div>
      </template>
    </section>

    <!-- 项目详情 -->
    <section v-else-if="view === 'project' && activeProject" class="panel panel-wide">
      <button class="back" @click="backToProjects">← 返回项目列表</button>

      <!-- step 步骤条：剧本→角色库/场景→分镜→导出本来就是有先后依赖的一条链
           （角色库靠剧本解析出来，分镜生图靠角色/场景库，导出靠分镜视频）。
           真正的 tab 切换：同一时刻只显示 activeStep 对应的那一块内容，不是长滚动页面；
           但步骤之间不锁——经常需要跳回去重新生成某个角色或某一镜，随便点随便切。
           放在页面最顶上，标题/简介这些跟着切下面走，不用先划过一段简介才找到导航。 -->
      <nav v-if="projectViewMode === 'edit'" class="step-bar">
        <button class="step-item" :class="{ active: activeStep === 'story' }" @click="activeStep = 'story'">
          <span class="step-num">①</span> 剧本
          <span class="step-status">{{ storyStepStatus }}</span>
        </button>
        <button class="step-item" :class="{ active: activeStep === 'characters' }" @click="activeStep = 'characters'">
          <span class="step-num">②</span> 角色库/场景
          <span class="step-status">{{ charactersStepStatus }}</span>
        </button>
        <button class="step-item" :class="{ active: activeStep === 'shots' }" @click="activeStep = 'shots'">
          <span class="step-num">③</span> 分镜
          <span class="step-status">{{ shotsStepStatus }}</span>
        </button>
        <button class="step-item" :class="{ active: activeStep === 'export' }" @click="activeStep = 'export'">
          <span class="step-num">④</span> 导出
          <span class="step-status">{{ exportStepStatus }}</span>
        </button>
      </nav>

      <div class="project-overview">
        <div class="project-title-row">
          <h2 :title="activeProject.title">{{ activeProject.title }}</h2>
          <span class="tag" :class="projectStatusColorClass(activeProject.status)">{{ projectStatusLabel(activeProject.status) }}</span>
          <span v-if="activeProject.lastExportedAt" class="tag tag-exported">已导出</span>
          <div class="view-mode-toggle">
            <button
              type="button"
              class="tab-btn"
              :class="{ active: projectViewMode === 'overview' }"
              @click="projectViewMode = 'overview'"
            >总览模式</button>
            <button
              type="button"
              class="tab-btn"
              :class="{ active: projectViewMode === 'edit' }"
              @click="projectViewMode = 'edit'"
            >编辑模式</button>
          </div>
        </div>
        <!-- 出图风格全程都在生效：角色设定图/场景参考图/每一镜画面，每次生成时都会现读
             Project.styleMode(见 seedream.py 的 _style_mode_for_*)，不是只在写剧本那一刻
             用一次——所以放在这里跨步骤常驻显示/可改，在"分镜"步骤切换风格后重新生成
             某一镜，是真的会用新风格。文案(premise)和内容类型(contentType)不一样，
             它们只在"生成剧本"这个动作发生的那一刻被读一次(讲给 claude 听怎么写剧本)，
             剧本写完之后就跟后面的生图/生视频/导出完全没关系了，所以挪到下面"剧本"步骤
             里去，不用每个步骤都占地方显示两个已经不起作用的设置。 -->
        <div class="style-mode-picker style-mode-picker-inline">
          <span class="style-mode-label">出图风格</span>
          <label><input type="radio" value="comic" :checked="activeProject.styleMode === 'comic'" @change="updateProjectStyleMode('comic')" /> 漫画风</label>
          <label><input type="radio" value="realistic" :checked="activeProject.styleMode === 'realistic'" @change="updateProjectStyleMode('realistic')" /> 真人风</label>
          <label><input type="radio" value="render3d" :checked="activeProject.styleMode === 'render3d'" @change="updateProjectStyleMode('render3d')" /> 3D风</label>
          <label><input type="radio" value="freeform" :checked="activeProject.styleMode === 'freeform'" @change="updateProjectStyleMode('freeform')" /> AI自由发挥</label>
          <span class="hint style-mode-note">每次生成图片/视频时都会现读这个设置，随时切换随时生效</span>
        </div>
        <!-- 生成比例：跟出图风格同一个模式，项目级常驻设置，每次生图/生视频都现读；
             但已经生成过的分镜图片/视频不会跟着重新生成，换比例前生成的素材还是老比例，
             混进同一部剧拼接导出时画幅会不一致，建议尽早定好比例再批量生成分镜。 -->
        <div class="style-mode-picker style-mode-picker-inline">
          <span class="style-mode-label">生成比例</span>
          <label v-for="r in mediaRatios" :key="r.id">
            <input
              type="radio"
              :value="r.id"
              :checked="activeProject.aspectRatio === r.id"
              @change="updateProjectAspectRatio(r.id)"
            /> {{ r.label }}
          </label>
          <span class="hint style-mode-note">这部剧所有分镜图片/视频统一用这个比例，方便导出时拼到一起</span>
        </div>
      </div>

      <div v-if="!settingsInfo.arkApiKeySet" class="warning-box">
        还没配置火山方舟 API Key，生成图片/视频会失败。
        <button @click="view = 'settings'">去设置页填一下</button>
      </div>

      <template v-if="projectViewMode === 'overview'">
        <section class="overview-section">
          <div class="overview-section-head">
            <h3>参考图一览</h3>
            <div class="overview-section-actions">
              <button class="ghost" :disabled="generatingStory" @click="generateStory">
                {{ generatingStory ? '生成中…' : '剧本一键生成' }}
              </button>
              <button class="ghost" :disabled="batchRunning.characters" @click="generateAllCharacters">
                {{ batchRunning.characters ? '触发中…' : '角色一键生成' }}
              </button>
              <button class="ghost" :disabled="batchRunning.scenes" @click="generateAllScenes">
                {{ batchRunning.scenes ? '触发中…' : '场景参考图一键生成' }}
              </button>
            </div>
          </div>
          <p v-if="characters.length === 0 && activeProject.scenes.length === 0" class="hint">
            还没有剧本，先用上面「剧本一键生成」或去编辑模式手动加剧本。
          </p>
          <template v-else>
            <p class="overview-subhead">角色</p>
            <div class="overview-ref-grid">
              <button
                v-for="c in characters"
                :key="c.id"
                type="button"
                class="overview-ref-card"
                @click="jumpToCharactersEdit"
              >
                <span class="overview-ref-thumb" :class="statusColorClass(c.status)">
                  <img v-if="c.url" :src="`${apiBaseUrl}${c.url}`" />
                  <i v-else class="overview-ref-placeholder">{{ c.status === 'running' ? '…' : '角' }}</i>
                  <span class="overview-ref-dot" :class="statusColorClass(c.status)"></span>
                </span>
                <span class="overview-ref-label">{{ c.name }}</span>
              </button>
              <p v-if="characters.length === 0" class="hint">还没有角色，先生成剧本会自动解析出角色。</p>
            </div>
            <p class="overview-subhead">场景</p>
            <div class="overview-ref-grid">
              <button
                v-for="(scene, sIdx) in activeProject.scenes"
                :key="scene.id"
                type="button"
                class="overview-ref-card"
                @click="jumpToSceneEdit(sIdx)"
              >
                <span class="overview-ref-thumb" :class="statusColorClass(scene.status)">
                  <img v-if="scene.url" :src="`${apiBaseUrl}${scene.url}`" />
                  <i v-else class="overview-ref-placeholder">{{ scene.status === 'running' ? '…' : '景' }}</i>
                  <span class="overview-ref-dot" :class="statusColorClass(scene.status)"></span>
                </span>
                <span class="overview-ref-label">第{{ scene.order + 1 }}场</span>
              </button>
            </div>
          </template>
        </section>

        <section class="overview-section">
          <div class="overview-section-head">
            <h3>分镜一览</h3>
            <div class="overview-section-actions">
              <button class="ghost" :disabled="batchRunning.images" @click="generateAllShotImages">
                {{ batchRunning.images ? '触发中…' : '分镜图片一键生成' }}
              </button>
            </div>
          </div>
          <p v-if="activeProject.scenes.length === 0" class="hint">还没有分镜，先去生成/导入剧本。</p>
          <div v-for="scene in activeProject.scenes" :key="scene.id">
            <p class="overview-subhead">第{{ scene.order + 1 }}场</p>
            <div class="overview-shot-grid">
              <button
                v-for="shot in scene.shots"
                :key="shot.id"
                type="button"
                class="overview-shot-card"
                @click="jumpToShotEdit(scene.order + 1, shot.order + 1)"
              >
                <span class="overview-shot-media" :class="statusColorClass(assetOf(shot.id, 'image')?.status)">
                  <img v-if="assetOf(shot.id, 'image')?.url" :src="`${apiBaseUrl}${assetOf(shot.id, 'image')?.url}`" />
                  <i v-else class="overview-shot-placeholder">{{ assetOf(shot.id, 'image')?.status === 'running' ? '生成中…' : '暂无画面' }}</i>
                  <span class="overview-shot-dot" :class="statusColorClass(assetOf(shot.id, 'image')?.status)"></span>
                  <span v-if="assetOf(shot.id, 'video')?.status === 'completed'" class="overview-shot-video-badge">▶</span>
                  <button
                    type="button"
                    class="overview-shot-refresh"
                    title="重新生成这一镜的图片"
                    :disabled="generatingAsset[`${shot.id}:image`]"
                    @click.stop="generateAsset(shot.id, 'image')"
                  >↻</button>
                </span>
                <span class="overview-shot-label">第{{ scene.order + 1 }}场·第{{ shot.order + 1 }}镜</span>
              </button>
            </div>
          </div>
          <div class="overview-cta-bar">
            <span>分镜图片都确认没问题了 →</span>
            <button class="ghost accent" :disabled="batchRunning.videos" @click="generateAllShotVideos">
              {{ batchRunning.videos ? '触发中…' : '分镜视频一键生成' }}
            </button>
            <button class="ghost accent" :disabled="batchRunning.voices" @click="generateAllShotVoices">
              {{ batchRunning.voices ? '触发中…' : '配音一键生成' }}
            </button>
          </div>
        </section>
      </template>

      <template v-if="projectViewMode === 'edit'">
      <div v-if="activeStep === 'story'">
        <section class="story-command-panel">
          <div class="story-command-summary">
            <div><span class="story-command-label">剧本</span><strong>{{ activeProject.scenes.length }} 个场次 · {{ activeProject.scenes.reduce((sum, scene) => sum + scene.shots.length, 0) }} 个镜头</strong></div>
            <span class="story-status" :class="statusColorClass(activeProject.story?.status)">{{ statusLabel(activeProject.story?.status ?? 'pending') }}</span>
          </div>
          <div class="story-command-actions">
            <button :disabled="generatingStory || activeProject.story?.status === 'running'" @click="generateStory">
              {{ generatingStory || activeProject.story?.status === 'running' ? '生成中…' : activeProject.scenes.length > 0 ? 'AI 重新生成剧本' : 'AI 生成剧本' }}
            </button>
            <button class="ghost" @click="showImportBox = !showImportBox">{{ showImportBox ? '收起导入' : '导入剧本' }}</button>
            <button class="ghost" :disabled="addingScene" @click="addScene">{{ addingScene ? '添加中…' : '+ 新增场次' }}</button>
          </div>
          <details class="story-context-panel">
            <summary>生成依据与内容类型</summary>
            <div class="premise-row">
              <p class="premise" :class="{ expanded: premiseExpanded }">{{ activeProject.premise }}</p>
              <button v-if="activeProject.premise.length > 120" class="ghost premise-toggle" @click="premiseExpanded = !premiseExpanded">{{ premiseExpanded ? '收起简介' : '展开简介' }}</button>
            </div>
            <div class="style-mode-picker style-mode-picker-inline">
              <span class="style-mode-label">内容类型</span>
              <label><input type="radio" value="character" :checked="activeProject.contentType === 'character'" @change="updateProjectContentType('character')" /> 人物剧情</label>
              <label><input type="radio" value="no_character" :checked="activeProject.contentType === 'no_character'" @change="updateProjectContentType('no_character')" /> 无固定角色</label>
              <span class="hint style-mode-note">只在生成剧本时生效，不改变已有剧本</span>
            </div>
          </details>
        </section>
        <div class="story-view-toggle">
          <button class="tab-btn" :class="{ active: storyViewMode === 'edit' }" @click="storyViewMode = 'edit'">场次大纲</button>
          <button class="tab-btn" :class="{ active: storyViewMode === 'table' }" @click="storyViewMode = 'table'">镜头表格</button>
        </div>

        <template v-if="storyViewMode === 'edit'">
        <div v-if="showImportBox" class="import-box">
          <div class="import-box-head"><div><strong>导入剧本</strong><span>粘贴符合格式的 JSON 内容</span></div><button class="ghost" @click="showImportBox = false">关闭</button></div>
          <details class="import-help"><summary>查看导入说明</summary><p class="hint">
            不想用 claude 自动生成？自己写一份剧本 JSON 粘贴进来直接导入。景别/运镜/构图/灯光/
            调色等专业措辞可以直接写进 sceneType（景别）和 motionPrompt（运镜/动态描述）里，
            让分镜的画面描述更精确。只有 drawPrompt 是必填字段。
          </p></details>
          <div class="field-row">
            <label class="hint"><input type="radio" value="append" v-model="importMode" /> 追加到已有场次后面</label>
            <label class="hint"><input type="radio" value="replace" v-model="importMode" /> 清空重来</label>
            <button class="ghost" @click="fillImportTemplate">填入格式示例</button>
          </div>
          <textarea v-model="importJsonText" rows="10" placeholder="粘贴剧本 JSON，格式见「填入格式示例」" />
          <div class="field-row">
            <button :disabled="importing || !importJsonText.trim()" @click="importStory">
              {{ importing ? '导入中…' : '导入' }}
            </button>
          </div>
          <p v-if="importError" class="error">{{ importError }}</p>
        </div>

        <div v-if="activeProject.scenes.length === 0" class="story-empty">
          <strong>还没有场次</strong><span>使用 AI 生成、导入剧本，或者手动新增一个场次。</span>
        </div>
        <div v-else class="story-outline">
          <div v-for="scene in activeProject.scenes" :key="scene.id" class="story-outline-row">
            <span class="story-outline-index">第{{ scene.order + 1 }}场</span>
            <input v-model="scene.summary" placeholder="场次描述" @change="saveScene(scene)" />
            <span class="story-outline-count">{{ scene.shots.length }} 镜</span>
            <button class="ghost shot-add-button" :disabled="addingShot[scene.id]" @click="addShot(scene.id)">+ 镜头</button>
            <button class="ghost danger" @click="deleteScene(scene.id)">删除</button>
          </div>
        </div>
        </template>

        <!-- 表格总览：场次是分组行，点展开箭头看这场戏所有镜头，每个字段格子都能直接改，
             改完 @change 照旧调 saveScene/saveShot，跟分镜步骤详情面板走的是同一套保存逻辑，
             这里只是换了个"一次看全部"的排布，不是另一套数据/另一条保存路径。
             故意只放文字类字段（场次描述/景别/关联角色/画面描述/运镜描述/台词/转场）——
             生成图片/视频/配音这些操作还是得去「分镜」步骤的详情面板做，两边分工不重合。 -->
        <div v-else class="story-table">
          <div v-if="activeProject.scenes.length === 0" class="hint">还没有镜头数据，先使用上方操作生成、导入或新增场次。</div>
          <div v-for="scene in activeProject.scenes" :key="scene.id" class="story-table-group">
            <div class="story-table-group-head">
              <span class="story-table-scene-label">第{{ scene.order + 1 }}场</span>
              <input
                v-model="scene.summary"
                class="story-table-summary-input"
                placeholder="场次描述"
                @change="saveScene(scene)"
              />
              <span class="hint">{{ scene.shots.length }} 镜</span>
              <button class="ghost shot-add-button" :disabled="addingShot[scene.id]" @click="addShot(scene.id)">
                {{ addingShot[scene.id] ? '…' : '+ 镜头' }}
              </button>
              <button class="ghost danger" @click="deleteScene(scene.id)">删除场次</button>
            </div>

            <div class="story-table-shots">
              <div class="story-table-row story-table-row-header">
                <span>镜号</span>
                <span>时长</span>
                <span>景别</span>
                <span>关联角色</span>
                <span>情绪</span>
                <span>画面描述</span>
                <span>运镜描述</span>
                <span>台词</span>
                <span>转场至下一镜</span>
                <span></span>
              </div>
              <div v-for="shot in scene.shots" :key="shot.id" class="story-table-row">
                <span class="story-table-order">{{ shot.order + 1 }}</span>
                <input type="number" min="1" step="0.5" v-model.number="shot.durationSec" @change="saveShot(shot)" />
                <input v-model="shot.sceneType as string" placeholder="景别" @change="saveShot(shot)" />
                <div class="story-table-character-cell">
                  <input v-model="shot.characterName as string" placeholder="角色" @change="saveShot(shot)" />
                  <select
                    class="manual-inline-select"
                    :disabled="characters.length === 0"
                    @change="onCharacterSelectChange(shot, $event)"
                  >
                    <option value="">+ 选择…</option>
                    <option v-for="c in characters" :key="c.id" :value="c.name">{{ c.name }}</option>
                  </select>
                  <div v-if="characterThumbs(shot.characterName).length > 0" class="char-thumb-row">
                    <span v-for="c in characterThumbs(shot.characterName)" :key="c.id" class="char-thumb-chip">
                      <img :src="`${apiBaseUrl}${c.url}`" :title="c.name" />
                      <span class="char-thumb-name">{{ c.name }}</span>
                      <button class="char-thumb-remove" type="button" :title="`移除${c.name}`" @click="removeCharacterName(shot, c.name)">×</button>
                    </span>
                  </div>
                </div>
                <input v-model="shot.emotion as string" placeholder="紧张/释然…" @change="saveShot(shot)" />
                <textarea v-model="shot.drawPrompt" rows="3" placeholder="画面描述" @change="saveShot(shot)" />
                <textarea v-model="shot.motionPrompt as string" rows="3" placeholder="运镜描述" @change="saveShot(shot)" />
                <textarea v-model="shot.dialogue as string" rows="2" placeholder="台词" @change="saveShot(shot)" />
                <select :value="shot.transitionToNext || ''" @change="setShotTransition(shot, ($event.target as HTMLSelectElement).value)">
                  <option value="">（不设置）</option>
                  <optgroup v-for="g in transitionManualGroups" :key="g.label" :label="g.label">
                    <option v-for="(opt, i) in g.options" :key="i" :value="opt.en">{{ opt.zh }}</option>
                  </optgroup>
                </select>
                <button class="ghost danger" title="删除镜头" @click="deleteShot(shot.id)">×</button>
              </div>
              <p v-if="scene.shots.length === 0" class="hint">这场戏还没有镜头</p>
            </div>
          </div>
          <div class="field-row">
            <button class="ghost" :disabled="addingScene" @click="addScene">
              {{ addingScene ? '添加中…' : '+ 手动添加一场' }}
            </button>
          </div>
        </div>
      </div>

      <div v-if="activeStep === 'characters'" class="character-box">
        <h2 style="margin-top: 0">角色库</h2>
        <p class="hint">
          先把每个角色的设定图生成出来，下面分镜生成图片时会自动用同名角色的设定图当参考，
          解决"每一镜角色长得不一样"的问题。
        </p>
        <div v-if="activeProject?.contentType === 'no_character'" class="warning-box">
          这个项目选的是"无固定角色"，剧本生成时不会刻意编人物出来——如果下面确实列出了角色，
          说明剧本里实际提到了，按需处理就行，不是必须填满的步骤。
        </div>
        <p v-if="characters.length === 0" class="hint">
          还没有角色——先去「剧本」步骤生成/添加剧本，镜头里写了 characterName 之后会自动出现在这里。
        </p>
        <div v-else class="character-grid">
          <div v-for="c in characters" :key="c.id" class="character-cell">
            <div class="character-head">
              <strong>{{ c.name }}</strong>
              <span class="tag" :class="statusColorClass(c.status)">{{ statusLabel(c.status) }}</span>
            </div>
            <img v-if="c.url" :src="`${apiBaseUrl}${c.url}`" title="双击用系统程序打开原图" @dblclick="openInSystemViewer(c.refImagePath)" />
            <p v-if="modelLabel(c.providerId, c.model)" class="hint model-tag">{{ modelLabel(c.providerId, c.model) }}</p>
            <p v-if="c.error" class="error">{{ c.error }}</p>
            <label class="character-prompt-label">外观描述/提示词</label>
            <textarea
              v-model="c.prompt as string"
              class="character-prompt-input"
              rows="2"
              placeholder="留空则只用角色名让模型自由发挥，比如：黑色长发，校服，温柔笑容"
              @change="saveCharacterPrompt(c)"
            />
            <div class="ai-optimize-row">
              <button
                type="button"
                class="ghost ai-optimize-button"
                :disabled="promptOptimizeState(`characterPrompt-${c.id}`).optimizing"
                @click="optimizePromptField(`characterPrompt-${c.id}`, c.prompt ?? '', `角色「${c.name}」的外观设定图描述`, (v) => { c.prompt = v; saveCharacterPrompt(c) })"
              >
                {{ promptOptimizeState(`characterPrompt-${c.id}`).optimizing ? '优化中…' : 'AI优化提示词' }}
              </button>
            </div>
            <p v-if="promptOptimizeState(`characterPrompt-${c.id}`).error" class="ai-optimize-error">{{ promptOptimizeState(`characterPrompt-${c.id}`).error }}</p>
            <div class="character-actions">
              <button :disabled="generatingCharacter[c.id] || c.status === 'running'" @click="generateCharacter(c.id)">
                {{ c.status === 'completed' ? '重新生成设定图' : '生成设定图' }}
              </button>
              <button class="ghost" @click="toggleReuseSearch(c.id)">
                {{ reuseOpenFor === c.id ? '收起' : '复用已有角色' }}
              </button>
            </div>

            <div v-if="reuseOpenFor === c.id" class="reuse-box">
              <input
                v-model="reuseQuery[c.id]"
                placeholder="按角色名搜其它项目里已生成好的角色，留空看最近的"
                @keyup.enter="searchReuseCandidates(c.id)"
                @change="searchReuseCandidates(c.id)"
              />
              <p v-if="reuseSearching[c.id]" class="hint">搜索中…</p>
              <p v-else-if="(reuseResults[c.id]?.length ?? 0) === 0" class="hint">没搜到已生成完成的角色</p>
              <div v-else class="reuse-results">
                <div v-for="r in reuseResults[c.id]" :key="r.id" class="reuse-item">
                  <img v-if="r.url" :src="`${apiBaseUrl}${r.url}`" />
                  <div class="reuse-item-info">
                    <strong>{{ r.name }}</strong>
                    <span class="hint">来自项目「{{ r.projectTitle }}」</span>
                  </div>
                  <button :disabled="reusing[c.id]" @click="reuseCharacter(c.id, r.id)">使用</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="activeStep === 'shots'">
      <!-- 场次改成纵向一行一行的手风琴，不再是"左边缩略图网格+右边常驻详情"两栏布局——
           跟"剧本"步骤的表格总览是同一个设计语言，两个步骤看起来是一套系统。
           点场次行展开，下面就是这场戏的所有镜头(也是手风琴行)；镜头默认收起成一条紧凑
           预览行，点开才展开完整表单——生成的视频是竖屏的，媒体预览做成瘦高的竖版，
           如果每一镜都同时全展开，页面会被拉得很长，所以还是保留"一次只完整展开一个"，
           跟之前胶片条的取舍逻辑一致，只是外观从横向胶片条换成纵向行。 -->
      <div class="scene-accordion">
        <div
          v-for="(scene, idx) in (activeProject?.scenes ?? [])"
          :key="scene.id"
          class="scene-row"
          :class="{
            expanded: idx === activeSceneIndex,
            dragging: draggedSceneId === scene.id,
            'drag-over': dragOverSceneId === scene.id && draggedSceneId !== scene.id
          }"
          draggable="true"
          @dragstart="draggedSceneId = scene.id"
          @dragend="draggedSceneId = null; dragOverSceneId = null"
          @dragover.prevent="dragOverSceneId = scene.id"
          @dragleave="dragOverSceneId === scene.id && (dragOverSceneId = null)"
          @drop.prevent="reorderScenes(scene.id)"
        >
          <div class="scene-row-head" @click="activateScene(idx)">
            <div class="scene-row-thumb-wrap">
              <div class="scene-row-thumb">
                <img v-if="scene.url" :src="`${apiBaseUrl}${scene.url}`" />
                <div v-else class="scene-row-thumb-placeholder" :class="{ 'skeleton-pulse': scene.status === 'running' }">
                  {{ scene.status === 'running' ? '' : statusLabel(scene.status) }}
                </div>
              </div>
              <span class="scene-row-progress" :class="{ 'is-incomplete': !sceneProgressComplete(scene) }">
                {{ sceneProgressRatio(scene) }}
              </span>
            </div>
            <strong class="scene-row-title">第{{ scene.order + 1 }}场</strong>
            <p class="scene-row-summary">{{ scene.summary || '（还没写场次描述）' }}</p>
          </div>

          <Transition name="fade">
            <div v-if="idx === activeSceneIndex" class="scene-row-body">
              <div class="scene-detail-header">
                <div class="scene-detail-title-row">
                  <input
                    v-model="scene.summary"
                    class="scene-summary-input"
                    placeholder="场次描述"
                    @change="saveScene(scene)"
                    @click.stop
                  />
                  <button
                    type="button"
                    class="ghost ai-optimize-button"
                    :disabled="promptOptimizeState(`sceneSummary-${scene.id}`).optimizing"
                    @click.stop="optimizePromptField(`sceneSummary-${scene.id}`, scene.summary ?? '', '短剧场次描述，同时也是这场戏场景参考图的生成提示词', (v) => { scene.summary = v; saveScene(scene) })"
                  >
                    {{ promptOptimizeState(`sceneSummary-${scene.id}`).optimizing ? '优化中…' : 'AI优化提示词' }}
                  </button>
                  <button class="ghost danger" @click.stop="deleteScene(scene.id)">删除场次</button>
                </div>
                <p v-if="promptOptimizeState(`sceneSummary-${scene.id}`).error" class="ai-optimize-error">{{ promptOptimizeState(`sceneSummary-${scene.id}`).error }}</p>
                <div class="scene-detail-meta-row">
                  <span class="hint">场次公共参数：以下设置会应用到本场所有镜头 · 第{{ scene.order + 1 }}场 · {{ scene.shots.length }} 镜</span>
                  <div class="scene-structure-actions">
                    <div class="scene-action-group">
                      <button
                        class="ghost"
                        :disabled="!canPrevScene"
                        :title="canPrevScene ? `跳到${sceneLabelAt(-1)}` : ''"
                        @click.stop="prevScene"
                      >← 上一场</button>
                      <button
                        class="ghost"
                        :disabled="!canNextScene"
                        :title="canNextScene ? `跳到${sceneLabelAt(1)}` : ''"
                        @click.stop="nextScene"
                      >下一场 →</button>
                    </div>
                    <span class="scene-actions-divider"></span>
                    <div class="scene-action-group">
                      <button class="ghost" :disabled="idx === 0" title="把这场戏往前移一位" @click="moveScene(scene.id, -1)">前移</button>
                      <button class="ghost" :disabled="idx === (activeProject?.scenes.length ?? 0) - 1" title="把这场戏往后移一位" @click="moveScene(scene.id, 1)">后移</button>
                    </div>
                    <button class="ghost accent" :disabled="addingScene" @click="addScene">+ 新增场次</button>
                  </div>
                </div>
              </div>

              <div class="scene-ref">
                <div class="scene-ref-media">
                  <div class="scene-ref-preview">
                    <img v-if="scene.url" :src="`${apiBaseUrl}${scene.url}`" title="双击用系统程序打开原图" @dblclick="openInSystemViewer(scene.refImagePath)" />
                    <div v-else class="scene-ref-placeholder" :class="{ 'skeleton-pulse': scene.status === 'running' }">
                      {{ scene.status === 'running' ? '生成中…' : '暂无场景图' }}
                    </div>
                    <span class="scene-ref-media-tag">生成图</span>
                  </div>
                  <div class="scene-ref-preview scene-ref-upload-preview">
                    <img
                      v-if="pathPreview(splitPaths(sceneRefImagePathsInput[scene.id]).slice(-1)[0])"
                      :src="pathPreview(splitPaths(sceneRefImagePathsInput[scene.id]).slice(-1)[0]) ?? ''"
                      title="最近选的这张参考图"
                    />
                    <div v-else class="scene-ref-placeholder">暂无参考图</div>
                    <span class="scene-ref-media-tag">上传的参考图</span>
                  </div>
                </div>
                <div class="scene-ref-info">
                  <div class="scene-ref-title-row"><label class="scene-ref-label">场景参考图</label><span class="tag" :class="statusColorClass(scene.status)">{{ statusLabel(scene.status) }}</span></div>
                  <p class="hint">
                    这场戏所有镜头生图时会自动带上，锁定背景、光线和色调。左边是生成结果，右边是你上传的图生图参考图（留空就是纯文生图）。
                  </p>
                  <p v-if="modelLabel(scene.providerId, scene.model)" class="hint model-tag">{{ modelLabel(scene.providerId, scene.model) }}</p>
                  <p v-if="scene.error" class="error">{{ scene.error }}</p>
                  <div class="ref-path-row">
                    <input
                      v-model="sceneRefImagePathsInput[scene.id]"
                      placeholder="参考图本地路径，留空纯文生图（图生图更贴合已有场地照片/参考画面）"
                    />
                    <button class="ghost" @click="pickReferenceFile(sceneRefImagePathsInput, scene.id, true)">选择文件…</button>
                  </div>
                  <div v-if="previewEntries(sceneRefImagePathsInput[scene.id]).length" class="ref-preview-row">
                    <div v-for="entry in previewEntries(sceneRefImagePathsInput[scene.id])" :key="entry.path" class="ref-pick-thumb">
                      <img class="ref-pick-preview" :src="entry.preview" :title="entry.path" />
                      <button
                        type="button"
                        class="ref-pick-remove"
                        title="删除这张参考图"
                        @click="removeReferencePath(sceneRefImagePathsInput, scene.id, entry.path)"
                      >×</button>
                    </div>
                  </div>
                  <div class="scene-ref-actions">
                    <button :disabled="generatingScene[scene.id] || scene.status === 'running'" @click="generateScene(scene.id)">
                      {{ scene.status === 'completed' ? '重新生成场景参考图' : '生成场景参考图' }}
                    </button>
                    <button class="ghost" @click="toggleSceneReuseSearch(scene.id)">
                      {{ sceneReuseOpenFor === scene.id ? '收起' : '复用已有场景' }}
                    </button>
                  </div>

                  <div v-if="sceneReuseOpenFor === scene.id" class="reuse-box">
                    <input
                      v-model="sceneReuseQuery[scene.id]"
                      placeholder="按场次描述搜其它项目里已生成好的场景，留空看最近的"
                      @keyup.enter="searchSceneReuseCandidates(scene.id)"
                      @change="searchSceneReuseCandidates(scene.id)"
                    />
                    <p v-if="sceneReuseSearching[scene.id]" class="hint">搜索中…</p>
                    <p v-else-if="(sceneReuseResults[scene.id]?.length ?? 0) === 0" class="hint">没搜到已生成完成的场景</p>
                    <div v-else class="reuse-results">
                      <div v-for="r in sceneReuseResults[scene.id]" :key="r.id" class="reuse-item">
                        <img v-if="r.url" :src="`${apiBaseUrl}${r.url}`" />
                        <div class="reuse-item-info">
                          <strong>{{ r.summary || '（无描述）' }}</strong>
                          <span class="hint">来自项目「{{ r.projectTitle }}」</span>
                        </div>
                        <button :disabled="sceneReusing[scene.id]" @click="reuseScene(scene.id, r.id)">使用</button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- 镜头列表：每一镜也是一个手风琴行，收起时是缩略图+一行预览+三个状态点
                   (画面/视频/配音各一个)，点开才展开完整的"媒体在左、文字在右"表单。 -->
              <!-- 批量操作工具条：只在这场戏里选中至少一镜时出现，选中范围不跨场次。
                   参考 liblib.tv 那类工具"框选一批节点，弹出批量生成/下载工具条"的思路，
                   我们先只做批量触发生成，下载以后再说。 -->
              <div v-if="selectedShotCount(scene) > 0" class="batch-toolbar">
                <span class="batch-toolbar-count">已选 {{ selectedShotCount(scene) }} 镜</span>
                <button
                  class="ghost"
                  :disabled="batchGenerating[`${scene.id}:image`]"
                  @click="batchGenerateAsset(scene, 'image')"
                >
                  {{ batchGenerating[`${scene.id}:image`] ? '生成中…' : '批量生成图片' }}
                </button>
                <button
                  class="ghost"
                  :disabled="batchGenerating[`${scene.id}:video`]"
                  @click="batchGenerateAsset(scene, 'video')"
                >
                  {{ batchGenerating[`${scene.id}:video`] ? '生成中…' : '批量生成视频' }}
                </button>
                <button
                  class="ghost"
                  :disabled="batchGenerating[`${scene.id}:voice`]"
                  @click="batchGenerateAsset(scene, 'voice')"
                >
                  {{ batchGenerating[`${scene.id}:voice`] ? '生成中…' : '批量生成配音' }}
                </button>
                <button class="ghost" @click="clearShotSelection(scene)">取消选择</button>
              </div>
              <div v-else-if="scene.shots.length > 1" class="batch-toolbar batch-toolbar-empty">
                <button class="ghost accent batch-toolbar-select-all" @click="selectAllShots(scene)">全选本场镜头，批量生成</button>
              </div>

              <nav class="shot-tabs" aria-label="镜头选择">
                <div
                  v-for="(shot, shotIdx) in scene.shots"
                  :key="shot.id"
                  class="shot-tab-group"
                >
                  <button class="shot-tab" :class="{ active: shot.id === activeShotId }" @click="activeShotId = shot.id">
                    <span>镜头{{ shot.order + 1 }}</span>
                    <span class="shot-tab-statuses">
                      <i :class="statusColorClass(assetOf(shot.id, 'image')?.status)">图</i>
                      <i :class="statusColorClass(assetOf(shot.id, 'video')?.status)">视</i>
                      <i :class="statusColorClass(assetOf(shot.id, 'voice')?.status)">音</i>
                    </span>
                  </button>
                  <button
                    class="shot-tab-add shot-add-button"
                    :disabled="addingShot[scene.id]"
                    :title="`在镜头${shot.order + 1}后新增镜头`"
                    @click="addShotAfter(scene, shot.id)"
                  >+</button>
                  <span
                    v-if="shotIdx < scene.shots.length - 1"
                    class="shot-tab-arrow"
                    :class="{ 'has-transition': !!shot.transitionToNext }"
                    :title="shot.transitionToNext ? `转场：${transitionZhLabel(shot.transitionToNext)}` : '未设置转场'"
                  >{{ shot.transitionToNext ? transitionShortLabel(shot.transitionToNext) : '→' }}</span>
                </div>
              </nav>

              <div class="shot-list">
                <div
                  v-for="(shot, shotIdx) in scene.shots"
                  :key="shot.id"
                  class="shot-row"
                  :class="{
                    expanded: shot.id === activeShotId,
                    dragging: draggedShotId === shot.id,
                    'drag-over': dragOverShotId === shot.id && draggedShotId !== shot.id
                  }"
                  draggable="true"
                  @dragstart="draggedShotId = shot.id"
                  @dragend="draggedShotId = null; dragOverShotId = null"
                  @dragover.prevent="dragOverShotId = shot.id"
                  @dragleave="dragOverShotId === shot.id && (dragOverShotId = null)"
                  @drop.prevent="reorderShots(shot.id)"
                >
                  <div class="shot-row-head" @click="toggleShot(shot.id)">
                    <input
                      type="checkbox"
                      class="shot-row-checkbox"
                      :checked="isShotSelected(shot.id)"
                      @click.stop="toggleShotSelection(shot.id)"
                    />
                    <button class="ghost shot-row-expand">{{ shot.id === activeShotId ? '▾' : '▸' }}</button>
                    <div class="shot-row-thumb">
                      <img v-if="assetOf(shot.id, 'image')?.url" :src="`${apiBaseUrl}${assetOf(shot.id, 'image')?.url}`" />
                      <span v-else-if="assetOf(shot.id, 'image')?.status === 'running'" class="skeleton-pulse shot-row-thumb-skeleton"></span>
                      <span v-else>{{ shot.order + 1 }}</span>
                    </div>
                    <span class="shot-row-order">镜{{ shot.order + 1 }}</span>
                    <span v-if="characterThumbs(shot.characterName).length > 0" class="char-thumb-row char-thumb-row-inline">
                      <img
                        v-for="c in characterThumbs(shot.characterName)"
                        :key="c.id"
                        :src="`${apiBaseUrl}${c.url}`"
                        :title="c.name"
                      />
                    </span>
                    <span v-if="shot.emotion" class="tag shot-row-emotion-tag">{{ shot.emotion }}</span>
                    <p class="shot-row-caption" :title="shot.drawPrompt">{{ shot.drawPrompt || '（未填画面描述）' }}</p>
                    <span class="shot-asset-statuses" title="画面 / 视频 / 配音状态">
                      <span class="asset-status-mini" :class="statusColorClass(assetOf(shot.id, 'image')?.status)">图</span>
                      <span class="asset-status-mini" :class="statusColorClass(assetOf(shot.id, 'video')?.status)">视</span>
                      <span class="asset-status-mini" :class="statusColorClass(assetOf(shot.id, 'voice')?.status)">音</span>
                    </span>
                    <button class="ghost danger" @click.stop="deleteShot(shot.id)">删除</button>
                  </div>

                  <Transition name="fade">
                    <div v-if="shot.id === activeShotId" class="shot-row-body">
                      <aside class="shot-media-pane">
                        <div class="shot-media-pane-head">
                          <strong>镜头{{ shot.order + 1 }} 生成结果</strong>
                          <span class="hint">图片 · 视频 · 语音</span>
                        </div>
                        <div class="shot-media-card">
                          <div class="shot-media-card-title"><span>分镜图片</span><span :class="statusColorClass(assetOf(shot.id, 'image')?.status)">{{ statusLabel(assetOf(shot.id, 'image')?.status ?? 'pending') }}</span></div>
                          <div class="shot-media-preview" :class="statusColorClass(assetOf(shot.id, 'image')?.status)">
                            <img v-if="assetOf(shot.id, 'image')?.url" :src="`${apiBaseUrl}${assetOf(shot.id, 'image')?.url}`" @dblclick="openInSystemViewer(assetOf(shot.id, 'image')?.filePath)" />
                            <span v-else>{{ statusLabel(assetOf(shot.id, 'image')?.status ?? 'pending') }}</span>
                          </div>
                        </div>
                        <div class="media-correspondence">当前分镜图片 <span>↓</span> 作为视频默认起始帧</div>
                        <div class="shot-media-card">
                          <div class="shot-media-card-title"><span>镜头视频</span><span :class="statusColorClass(assetOf(shot.id, 'video')?.status)">{{ statusLabel(assetOf(shot.id, 'video')?.status ?? 'pending') }}</span></div>
                          <div class="shot-media-preview" :class="statusColorClass(assetOf(shot.id, 'video')?.status)">
                            <video v-if="assetOf(shot.id, 'video')?.url" :src="`${apiBaseUrl}${assetOf(shot.id, 'video')?.url}`" controls @dblclick="openInSystemViewer(assetOf(shot.id, 'video')?.filePath)" />
                            <span v-else>{{ statusLabel(assetOf(shot.id, 'video')?.status ?? 'pending') }}</span>
                          </div>
                        </div>
                        <div class="shot-media-card shot-media-card-audio">
                          <div class="shot-media-card-title"><span>镜头语音</span><span :class="statusColorClass(assetOf(shot.id, 'voice')?.status)">{{ statusLabel(assetOf(shot.id, 'voice')?.status ?? 'pending') }}</span></div>
                          <audio v-if="assetOf(shot.id, 'voice')?.url" :src="`${apiBaseUrl}${assetOf(shot.id, 'voice')?.url}`" controls @dblclick="openInSystemViewer(assetOf(shot.id, 'voice')?.filePath)" />
                          <span v-else class="hint">{{ statusLabel(assetOf(shot.id, 'voice')?.status ?? 'pending') }}</span>
                        </div>
                      </aside>
                      <div class="shot-settings-pane">
                      <section class="consistency-panel">
                        <div class="consistency-panel-head">
                          <div><strong>一致性控制</strong><span>锁定人物、场景和视频起始帧</span></div>
                          <div class="shot-move-actions">
                            <button class="ghost" :disabled="shotIdx === 0" @click.stop="moveShot(scene, shot.id, -1)">← 前移</button>
                            <button class="ghost" :disabled="shotIdx === scene.shots.length - 1" @click.stop="moveShot(scene, shot.id, 1)">后移 →</button>
                            <button class="ghost danger" @click.stop="deleteShot(shot.id)">删除镜头</button>
                          </div>
                          <span class="tag">镜头{{ shot.order + 1 }}</span>
                        </div>
                        <div class="consistency-grid">
                          <div class="consistency-item">
                            <label>角色一致性</label>
                            <span>{{ characterThumbs(shot.characterName).length > 0 ? `已关联 ${characterThumbs(shot.characterName).length} 个角色设定图` : '未匹配角色设定图' }}</span>
                          </div>
                          <div class="consistency-item">
                            <label>场景一致性</label>
                            <span>{{ scene.status === 'completed' ? '已使用本场环境母版图' : '场景母版图尚未完成' }}</span>
                          </div>
                          <div class="consistency-item consistency-item-wide">
                            <label>画面参考图</label>
                            <div class="ref-path-row">
                              <input v-model="refImagePathsInput[shot.id]" placeholder="留空时自动使用角色设定图和场景母版图" />
                              <div v-for="entry in previewEntries(refImagePathsInput[shot.id])" :key="entry.path" class="ref-pick-thumb">
                                <img class="ref-pick-preview" :src="entry.preview" :title="entry.path" />
                                <button
                                  type="button"
                                  class="ref-pick-remove"
                                  title="删除这张参考图"
                                  @click="removeReferencePath(refImagePathsInput, shot.id, entry.path)"
                                >×</button>
                              </div>
                              <button class="ghost" @click="pickReferenceFile(refImagePathsInput, shot.id, true)">选择文件…</button>
                            </div>
                          </div>
                          <div class="consistency-item consistency-item-wide">
                            <label>视频起始帧</label>
                            <div class="ref-path-row">
                              <input v-model="startImagePathInput[shot.id]" placeholder="留空时自动使用当前镜头已选中的分镜图片" />
                              <img
                                v-if="pathPreview(startImagePathInput[shot.id])"
                                class="ref-pick-preview"
                                :src="pathPreview(startImagePathInput[shot.id]) ?? ''"
                                title="视频起始帧参考图"
                              />
                              <button class="ghost" @click="pickReferenceFile(startImagePathInput, shot.id, false)">选择文件…</button>
                            </div>
                          </div>
                        </div>
                      </section>
                      <div class="shot-stage-heading">
                        <span class="shot-stage-num">1</span>
                        <div><strong>镜头参数</strong><span>定义景别、角色、情绪、时长与转场</span></div>
                      </div>
                      <div class="shot-meta-row">
                        <div class="shot-meta-field">
                          <label>景别</label>
                          <input v-model="shot.sceneType as string" placeholder="远景/全景/中景/近景/特写" @change="saveShot(shot)" />
                          <select class="manual-inline-select" @change="onManualSelectChange(shot, 'sceneType', $event)">
                            <option value="">+ 手册景别术语…</option>
                            <optgroup v-for="g in sceneTypeManualGroups" :key="g.label" :label="g.label">
                              <option v-for="(opt, i) in g.options" :key="i" :value="opt.en">{{ opt.zh }}</option>
                            </optgroup>
                          </select>
                        </div>
                        <div class="shot-meta-field">
                          <label>关联角色</label>
                          <input v-model="shot.characterName as string" placeholder="角色库里的角色名，多个用顿号分隔" @change="saveShot(shot)" />
                          <select
                            class="manual-inline-select"
                            :disabled="characters.length === 0"
                            @change="onCharacterSelectChange(shot, $event)"
                          >
                            <option value="">{{ characters.length === 0 ? '角色库还没有角色' : '+ 从角色库选择…' }}</option>
                            <option v-for="c in characters" :key="c.id" :value="c.name">{{ c.name }}</option>
                          </select>
                          <div v-if="characterThumbs(shot.characterName).length > 0" class="char-thumb-row">
                            <span v-for="c in characterThumbs(shot.characterName)" :key="c.id" class="char-thumb-chip">
                              <img :src="`${apiBaseUrl}${c.url}`" :title="c.name" />
                              <span class="char-thumb-name">{{ c.name }}</span>
                              <button class="char-thumb-remove" type="button" :title="`移除${c.name}`" @click="removeCharacterName(shot, c.name)">×</button>
                            </span>
                          </div>
                        </div>
                        <div class="shot-meta-field">
                          <label>情绪/表演基调</label>
                          <input v-model="shot.emotion as string" placeholder="紧张/释然/悲伤…" @change="saveShot(shot)" />
                          <select class="manual-inline-select" @change="onManualSelectChange(shot, 'emotion', $event)">
                            <option value="">+ 手册情绪术语…</option>
                            <optgroup v-for="g in emotionManualGroups" :key="g.label" :label="g.label">
                              <option v-for="(opt, i) in g.options" :key="i" :value="opt.en">{{ opt.zh }}</option>
                            </optgroup>
                          </select>
                        </div>
                        <div class="shot-meta-field shot-meta-field-narrow">
                          <label>时长（秒）</label>
                          <input
                            type="number"
                            min="1"
                            step="0.5"
                            v-model.number="shot.durationSec"
                            @change="saveShot(shot)"
                          />
                        </div>
                        <div class="shot-meta-field">
                          <label>转场至{{ shotIdx === scene.shots.length - 1 ? '下一场' : '下一镜' }}</label>
                          <select
                            :value="shot.transitionToNext || ''"
                            :title="shot.transitionToNext ? transitionZhLabel(shot.transitionToNext) ?? '' : ''"
                            @change="setShotTransition(shot, ($event.target as HTMLSelectElement).value)"
                          >
                            <option value="">（不设置）</option>
                            <optgroup v-for="g in transitionManualGroups" :key="g.label" :label="g.label">
                              <option v-for="(opt, i) in g.options" :key="i" :value="opt.en">{{ opt.zh }}</option>
                            </optgroup>
                          </select>
                        </div>
                      </div>

                      <!-- 三组"媒体(竖版) + 文字 + 按钮"配对行：图配画面描述，视频配运镜描述，
                           配音配台词，因果关系挨在一起，不用在一大片图和一大片文字之间来回找对应。
                           媒体预览做成竖版比例(接近 9:16)，因为 Seedance 现在默认出的是竖屏视频。
                           按钮放文字框正下方；预览框边框颜色随状态变(红=未完成/蓝=生成中/绿=完成/
                           红=失败)，跟生成按钮的状态点是同一套语义色。 -->
                      <div class="shot-stage-heading">
                        <span class="shot-stage-num">2</span>
                        <div><strong>生成画面</strong><span>填写画面描述并生成分镜图片</span></div>
                        <span class="shot-stage-status" :class="statusColorClass(assetOf(shot.id, 'image')?.status)">{{ statusLabel(assetOf(shot.id, 'image')?.status ?? 'pending') }}</span>
                      </div>
                      <div class="pair-row shot-stage-content">
                        <div class="pair-media" :class="statusColorClass(assetOf(shot.id, 'image')?.status)">
                          <img
                            v-if="assetOf(shot.id, 'image')?.url"
                            :src="`${apiBaseUrl}${assetOf(shot.id, 'image')?.url}`"
                            title="双击用系统程序打开原图"
                            @dblclick="openInSystemViewer(assetOf(shot.id, 'image')?.filePath)"
                          />
                          <span v-else-if="assetOf(shot.id, 'image')?.status === 'running'" class="skeleton-pulse pair-media-skeleton"></span>
                          <span v-else class="pair-media-empty">{{ statusLabel(assetOf(shot.id, 'image')?.status ?? 'pending') }}</span>
                        </div>
                        <div class="pair-text">
                          <label>画面描述</label>
                          <textarea v-model="shot.drawPrompt" rows="3" @change="saveShot(shot)" />
                          <select class="manual-inline-select" @change="onManualSelectChange(shot, 'drawPrompt', $event)">
                            <option value="">+ 从手册插入构图/灯光/调色/焦距/导演风格术语…</option>
                            <optgroup v-for="g in drawPromptManualGroups" :key="g.label" :label="g.label">
                              <option v-for="(opt, i) in g.options" :key="i" :value="opt.en">{{ opt.zh }}</option>
                            </optgroup>
                          </select>
                          <div class="ai-optimize-row">
                            <button
                              type="button"
                              class="ghost"
                              :disabled="promptOptimizeState(`shotDrawPrompt-${shot.id}`).optimizing"
                              @click="optimizePromptField(`shotDrawPrompt-${shot.id}`, shot.drawPrompt ?? '', '短剧分镜的画面描述', (v) => { shot.drawPrompt = v; saveShot(shot) })"
                            >
                              {{ promptOptimizeState(`shotDrawPrompt-${shot.id}`).optimizing ? '优化中…' : 'AI优化提示词' }}
                            </button>
                          </div>
                          <p v-if="promptOptimizeState(`shotDrawPrompt-${shot.id}`).error" class="ai-optimize-error">{{ promptOptimizeState(`shotDrawPrompt-${shot.id}`).error }}</p>
                          <p v-if="!refImagePathsInput[shot.id]?.trim() && !(scene.status === 'completed' && scene.refImagePath)" class="hint hint-warning">
                            这场戏的场景参考图还没生成完成，现在生成这一镜不会带环境一致性参考图（背景/光线可能跟其它镜头对不上）。
                            先去上面把场景参考图生成好，再回来点生成/重新生成。
                          </p>
                          <button
                            class="pair-action"
                            :class="statusColorClass(assetOf(shot.id, 'image')?.status)"
                            :disabled="generatingAsset[`${shot.id}:image`] || assetOf(shot.id, 'image')?.status === 'running'"
                            @click="generateAsset(shot.id, 'image')"
                          >
                            {{ genButtonLabel(shot.id, 'image', '生成图片', '重新生成图片') }}
                          </button>
                          <p v-if="assetOf(shot.id, 'image')?.error" class="error">{{ assetOf(shot.id, 'image')?.error }}</p>
                        </div>
                      </div>

                      <div class="shot-stage-heading">
                        <span class="shot-stage-num">3</span>
                        <div><strong>生成视频</strong><span>基于分镜画面和运镜描述生成动态镜头</span></div>
                        <span class="shot-stage-status" :class="statusColorClass(assetOf(shot.id, 'video')?.status)">{{ statusLabel(assetOf(shot.id, 'video')?.status ?? 'pending') }}</span>
                      </div>
                      <div class="pair-row shot-stage-content">
                        <div class="pair-media" :class="statusColorClass(assetOf(shot.id, 'video')?.status)">
                          <video
                            v-if="assetOf(shot.id, 'video')?.url"
                            :src="`${apiBaseUrl}${assetOf(shot.id, 'video')?.url}`"
                            controls
                            title="双击用系统程序打开原视频"
                            @dblclick="openInSystemViewer(assetOf(shot.id, 'video')?.filePath)"
                          />
                          <span v-else-if="assetOf(shot.id, 'video')?.status === 'running'" class="skeleton-pulse pair-media-skeleton"></span>
                          <span v-else class="pair-media-empty">{{ statusLabel(assetOf(shot.id, 'video')?.status ?? 'pending') }}</span>
                        </div>
                        <div class="pair-text">
                          <label>运镜/动态描述</label>
                          <textarea v-model="shot.motionPrompt as string" rows="3" @change="saveShot(shot)" />
                          <select class="manual-inline-select" @change="onManualSelectChange(shot, 'motionPrompt', $event)">
                            <option value="">+ 从手册插入运镜/转场/镜头功能术语…</option>
                            <optgroup v-for="g in motionPromptManualGroups" :key="g.label" :label="g.label">
                              <option v-for="(opt, i) in g.options" :key="i" :value="opt.en">{{ opt.zh }}</option>
                            </optgroup>
                          </select>
                          <button
                            class="pair-action"
                            :class="statusColorClass(assetOf(shot.id, 'video')?.status)"
                            :disabled="generatingAsset[`${shot.id}:video`] || assetOf(shot.id, 'video')?.status === 'running'"
                            @click="generateAsset(shot.id, 'video')"
                          >
                            {{ genButtonLabel(shot.id, 'video', '生成视频', '重新生成视频') }}
                          </button>
                          <p v-if="assetOf(shot.id, 'video')?.error" class="error">{{ assetOf(shot.id, 'video')?.error }}</p>
                        </div>
                      </div>

                      <div class="shot-stage-heading">
                        <span class="shot-stage-num">4</span>
                        <div><strong>生成配音</strong><span>填写台词或旁白并生成音频</span></div>
                        <span class="shot-stage-status" :class="statusColorClass(assetOf(shot.id, 'voice')?.status)">{{ statusLabel(assetOf(shot.id, 'voice')?.status ?? 'pending') }}</span>
                      </div>
                      <div class="pair-row shot-stage-content">
                        <div class="pair-media pair-media-audio" :class="statusColorClass(assetOf(shot.id, 'voice')?.status)">
                          <audio
                            v-if="assetOf(shot.id, 'voice')?.url"
                            :src="`${apiBaseUrl}${assetOf(shot.id, 'voice')?.url}`"
                            controls
                            title="双击用系统程序打开原音频"
                            @dblclick="openInSystemViewer(assetOf(shot.id, 'voice')?.filePath)"
                          />
                          <span v-else-if="assetOf(shot.id, 'voice')?.status === 'running'" class="skeleton-pulse pair-media-skeleton"></span>
                          <span v-else class="pair-media-empty">{{ statusLabel(assetOf(shot.id, 'voice')?.status ?? 'pending') }}</span>
                        </div>
                        <div class="pair-text">
                          <div class="pair-text-label-row">
                            <label>台词/旁白</label>
                            <button
                              class="ghost pair-text-clear"
                              :disabled="!(shot.dialogue ?? '').trim()"
                              title="清空台词——这一镜不需要台词/字幕时用，避免留一句对不上时间轴的旧台词"
                              @click="clearDialogue(shot)"
                            >清空台词</button>
                          </div>
                          <textarea v-model="shot.dialogue as string" rows="2" @change="saveShot(shot)" />
                          <div class="pair-action-row">
                            <button
                              class="pair-action"
                              :class="statusColorClass(assetOf(shot.id, 'voice')?.status)"
                              :disabled="generatingAsset[`${shot.id}:voice`] || assetOf(shot.id, 'voice')?.status === 'running'"
                              @click="generateAsset(shot.id, 'voice')"
                            >
                              {{ genButtonLabel(shot.id, 'voice', '生成配音', '重新生成配音') }}
                            </button>
                            <button
                              v-if="assetOf(shot.id, 'voice')"
                              class="ghost danger"
                              :disabled="deletingAsset[`${shot.id}:voice`]"
                              title="删掉已生成的配音素材"
                              @click="deleteShotAsset(shot.id, 'voice')"
                            >{{ deletingAsset[`${shot.id}:voice`] ? '删除中…' : '删除配音' }}</button>
                          </div>
                          <p v-if="assetOf(shot.id, 'voice')?.error" class="error">{{ assetOf(shot.id, 'voice')?.error }}</p>
                        </div>
                      </div>

                      <details class="shot-advanced">
                        <summary><span class="shot-stage-num">5</span> 高级参数（参考图 · 候选生成 · 模型信息）</summary>
                        <div class="advanced-grid">
                          <div class="advanced-cell">
                            <label class="advanced-cell-title">画面</label>
                            <input
                              class="consistency-duplicate"
                              v-model="refImagePathsInput[shot.id]"
                              placeholder="参考图本地路径，留空自动用角色库同名角色的设定图"
                            />
                            <button
                              :disabled="generatingAsset[`${shot.id}:image`]"
                              @click="generateAsset(shot.id, 'image', 3)"
                            >
                              生成3张候选选优
                            </button>
                            <p v-if="modelLabel(assetOf(shot.id, 'image')?.providerId, assetOf(shot.id, 'image')?.model)" class="hint model-tag">
                              {{ modelLabel(assetOf(shot.id, 'image')?.providerId, assetOf(shot.id, 'image')?.model) }}
                            </p>
                            <div v-if="candidatesOf(shot.id, 'image').length > 1" class="candidate-gallery">
                              <p class="hint">候选（{{ candidatesOf(shot.id, 'image').length }}）：点图选用，双击打开原图</p>
                              <div class="candidate-list">
                                <div
                                  v-for="c in candidatesOf(shot.id, 'image')"
                                  :key="c.id"
                                  class="candidate-item"
                                  :class="{ selected: c.selected }"
                                >
                                  <img
                                    v-if="c.url"
                                    :src="`${apiBaseUrl}${c.url}`"
                                    @click="c.status === 'completed' && selectAsset(shot.id, 'image', c.id)"
                                    @dblclick="openInSystemViewer(c.filePath)"
                                  />
                                  <div v-else class="candidate-placeholder">{{ statusLabel(c.status) }}</div>
                                  <span v-if="c.selected" class="tag">已选用</span>
                                  <p v-if="c.error" class="error">{{ c.error }}</p>
                                </div>
                              </div>
                            </div>
                          </div>

                          <div class="advanced-cell">
                            <label class="advanced-cell-title">视频</label>
                            <input class="consistency-duplicate" v-model="startImagePathInput[shot.id]" placeholder="起始帧路径，留空自动用上面生成的图片" />
                            <p v-if="modelLabel(assetOf(shot.id, 'video')?.providerId, assetOf(shot.id, 'video')?.model)" class="hint model-tag">
                              {{ modelLabel(assetOf(shot.id, 'video')?.providerId, assetOf(shot.id, 'video')?.model) }}
                            </p>
                          </div>

                          <div class="advanced-cell">
                            <label class="advanced-cell-title">配音</label>
                            <input v-model="voiceOptionsInput[shot.id].referenceAudioPath" placeholder="参考音色路径，可留空" />
                            <p v-if="modelLabel(assetOf(shot.id, 'voice')?.providerId, assetOf(shot.id, 'voice')?.model)" class="hint model-tag">
                              {{ modelLabel(assetOf(shot.id, 'voice')?.providerId, assetOf(shot.id, 'voice')?.model) }}
                            </p>
                          </div>
                        </div>
                      </details>
                      </div>
                    </div>
                  </Transition>
                </div>
                <p v-if="scene.shots.length === 0" class="hint">这场戏还没有镜头。</p>
                <button class="ghost shot-add-button shot-row-add" :disabled="addingShot[scene.id]" @click="addShot(scene.id)">
                  {{ addingShot[scene.id] ? '添加中…' : '+ 镜头' }}
                </button>
              </div>
            </div>
          </Transition>
        </div>

        <p v-if="(activeProject?.scenes.length ?? 0) === 0" class="hint">还没有场次，先去「剧本」步骤生成/添加。</p>
        <button class="ghost scene-row-add" :disabled="addingScene" @click="addScene">
          {{ addingScene ? '添加中…' : '+ 添加场次' }}
        </button>
      </div>
      </div>


      <div v-if="activeStep === 'export'" class="export-box">
        <h2>导出成片</h2>
        <p v-if="(activeProject?.scenes.length ?? 0) === 0" class="hint">
          还没有分镜——先去「剧本」步骤生成剧本，「分镜」步骤把每一镜的视频都生成出来，再回这里导出。
        </p>
        <template v-else>
          <!-- 缺视频的镜头不再拦着不让导出——直接跳过那几镜拼剩下的，但导出前先摆在这里
               提醒一下，免得成片里莫名其妙"少了一段"却不知道是哪一镜。 -->
          <div v-if="missingVideoShots.length > 0" class="warning-box">
            <strong>{{ missingVideoShots.length }} 个镜头还没有视频，导出时会被跳过：</strong>
            <span class="missing-shots-list">
              <button
                v-for="(m, i) in missingVideoShots"
                :key="i"
                class="tag tag-jump"
                title="跳到这一镜"
                @click="jumpToShot(m.sceneOrder, m.shotOrder)"
              >第{{ m.sceneOrder }}场镜{{ m.shotOrder }}</button>
            </span>
          </div>
          <label class="checkbox-field">
            <input type="checkbox" v-model="settingsForm.exportBurnSubtitles" /> 这次导出烧录字幕（默认值在设置页改）
          </label>
          <label class="checkbox-field">
            <input type="checkbox" v-model="settingsForm.exportUseBgm" :disabled="!settingsForm.exportBgmPath" />
            这次导出加背景音乐（默认值在设置页改，没配置背景音乐文件时勾不了）
          </label>
          <button :disabled="exporting" @click="exportProject">
            {{ exporting ? '拼接中…' : '按顺序拼接所有分镜视频' }}
          </button>
          <p class="hint">缺视频的镜头会被自动跳过，不会拦住整体导出</p>
          <p v-if="exportError" class="error">{{ exportError }}</p>
          <div v-if="exportSkippedShots.length > 0" class="warning-box">
            <strong>已导出，但以下 {{ exportSkippedShots.length }} 个镜头因为没有视频被跳过，不在成片里：</strong>
            <span class="missing-shots-list">
              <button
                v-for="(m, i) in exportSkippedShots"
                :key="i"
                class="tag tag-jump"
                title="跳到这一镜"
                @click="jumpToShot(m.sceneOrder, m.shotOrder)"
              >第{{ m.sceneOrder }}场镜{{ m.shotOrder }}</button>
            </span>
          </div>
          <video v-if="exportUrl" :src="`${apiBaseUrl}${exportUrl}`" controls />
          <p v-if="exportFilePath" class="hint">
            成片路径：{{ exportFilePath }}<span v-if="!exportUrl">（自定义导出目录不在预览范围内，去这个路径找文件）</span>
          </p>
        </template>
      </div>
      </template>
    </section>
    </main>
  </div>
</template>

<style>
/* 全局重置：只有这一小块不加 scoped，因为 html/body 在组件模板之外，scoped 样式的
   data-v-xxx 属性选择器根本碰不到它们。没有这个重置的话，浏览器默认的 UA 样式表会给
   body 加 8px 的默认 margin——.shell 自己是 min-height:100vh 撑满视口，body 这圈
   margin 就会在窗口四周挤出一圈可见的空白，看起来像是内容外面莫名其妙多了一层"外框"，
   本质就是没清掉的浏览器默认边距，不是刻意设计的样式。 */
html,
body {
  margin: 0;
  padding: 0;
  height: 100%;
}
#app {
  height: 100%;
}
</style>

<style scoped>
/* 经典 CMS 中控布局：左侧固定导航栏 + 右侧内容区，各自独立滚动。
   之前是顶部一条 topbar + 下面单栏内容，项目多了之后"当前在哪个项目/哪个功能"
   全靠页面顶部一行小字，现在挪到左侧常驻，切换页面时导航状态更醒目。 */
.shell {
  font-family: 'Inter', system-ui, sans-serif;
  color: #18181b;
  display: flex;
  min-height: 100vh;
  align-items: stretch;
}

.sidebar {
  width: 220px;
  flex-shrink: 0;
  border-right: 1px solid #e4e4e7;
  padding: 20px 16px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  position: sticky;
  top: 0;
  height: 100vh;
  box-sizing: border-box;
}
.sidebar-brand { display: flex; align-items: center; }
.sidebar-brand-logo {
  width: 34px; height: 34px; border-radius: 10px; object-fit: cover;
  box-shadow: 0 8px 20px rgba(24, 24, 27, 0.12);
}
.sidebar-nav { display: flex; flex-direction: column; gap: 4px; }
.sidebar-nav-group { display: flex; flex-direction: column; gap: 4px; }
.sidebar-nav-label { padding: 0 8px; color: #a1a1aa; font-size: 10px; font-weight: 700; }
.sidebar-nav button {
  display: flex; align-items: center; gap: 8px; text-align: left;
  background: none;
  color: #18181b;
  border: 1px solid transparent;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}
.nav-icon { width: 15px; height: 15px; stroke-width: 1.7; flex-shrink: 0; }
.sidebar-nav button > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sidebar-nav button.active {
  background: #18181b;
  color: white;
}
.sidebar-current {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px;
  background: #f4f4f5;
  border-radius: 8px;
  font-size: 13px;
}
.sidebar-current strong { font-size: 13px; word-break: break-all; }
.sidebar-footer { margin-top: auto; }

.main-content {
  flex: 1;
  min-width: 0;
  padding: 24px 32px 64px;
  overflow-y: auto;
  height: 100vh;
  box-sizing: border-box;
}
/* 之前这里给 .panel/.panel-wide 分别设了 800px/1400px 的宽度上限，本意是"阅读舒适
   边界"，但超过上限后内容不是居中而是贴着左边(紧挨侧边栏)，右边留一整条空白，宽屏/
   大显示器上很显眼——用户明确要求"完全不设上限，跟着窗口拉伸"，所以这里不再限宽，
   内容跟着 .main-content 的可用宽度走。 */

.api-status { font-size: 12px; }
.api-status.ok { color: #1a8a4a; }
.api-status.error { color: #b4232f; }
.api-status.checking { color: #8a8a90; }

.panel h2 { font-size: 16px; margin: 20px 0 8px; }
.panel h2:first-child { margin-top: 0; }

.field, .field-row { margin-bottom: 12px; display: flex; flex-direction: column; gap: 6px; }
.field-row { flex-direction: row; align-items: flex-start; }
.field label { font-size: 13px; color: #52525b; }
.model-ref-hint { line-height: 1.6; }
.model-ref-hint code {
  font-family: ui-monospace, monospace; font-size: 11px; color: #3f6212; background: #f0fdf4;
  padding: 1px 5px; border-radius: 4px;
}
.checkbox-field { display: flex; align-items: center; gap: 6px; font-size: 13px; color: #52525b; margin-bottom: 12px; }
.checkbox-field input { width: auto; }
.settings-page-head, .settings-group-head, .settings-actions { display: flex; }
.settings-page-head, .settings-group-head { align-items: center; justify-content: space-between; gap: 12px; }
.settings-page-head h1, .settings-group-head h2 { margin: 0; }
.settings-page-head p, .settings-group-head p { margin: 2px 0 0; }
.settings-state, .field-badge { font-size: 10px; }
.field-help { margin: 0; font-size: 11px; color: #8a8a90; }
.settings-doc summary { cursor: pointer; }
.story-gen-provider-tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.story-gen-provider-tab {
  min-height: 78px; text-align: left; white-space: normal;
  display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: 5px;
  padding: 12px 14px; border: 1px solid #e4e4e7; border-radius: 12px; background: white; color: #18181b;
}
.story-gen-provider-tab strong { font-size: 14px; line-height: 1.35; }
.story-gen-provider-tab span { color: #71717a; font-size: 12px; line-height: 1.45; }
.story-gen-provider-tab.active { border-color: #18181b; background: #18181b; color: white; }
.story-gen-provider-tab.active span { color: #d4d4d8; }
.cli-path-row { display: flex; gap: 8px; align-items: center; }
.cli-path-row input { flex: 1; }
.ai-optimize-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.ai-optimize-error { margin: 4px 0 0; font-size: 12px; color: #dc2626; }
.pending-ref-panel { display: flex; gap: 12px; align-items: flex-start; padding: 12px; margin-bottom: 12px; border: 1px solid #bfdbfe; background: #eff6ff; border-radius: 10px; }
.pending-ref-thumb { width: 72px; height: 72px; object-fit: cover; border-radius: 8px; flex-shrink: 0; }
.pending-ref-info { display: flex; flex-direction: column; gap: 4px; }
.pending-ref-actions { display: flex; gap: 8px; margin-top: 4px; flex-wrap: wrap; }
.story-gen-test-block { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; margin: 4px 0 14px; }
.story-gen-test-result { margin: 0; font-size: 12px; line-height: 1.5; }
.story-gen-test-result.ok { color: #16a34a; }
.story-gen-test-result.error { color: #dc2626; }
.update-card { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.update-card strong { font-size: 13px; }
.update-card p { margin: 4px 0 0; }
.update-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.update-progress { height: 7px; margin-top: 9px; overflow: hidden; border-radius: 999px; background: #e4e4e7; }
.update-progress span { display: block; height: 100%; border-radius: inherit; background: #18181b; transition: width .2s ease; }
input, textarea {
  font-family: inherit;
  font-size: 13px;
  padding: 6px 8px;
  border: 1px solid #d4d4d8;
  border-radius: 6px;
  width: 100%;
  box-sizing: border-box;
}
button {
  font-family: inherit;
  font-size: 13px;
  padding: 6px 14px;
  border-radius: 6px;
  border: 1px solid #18181b;
  background: #18181b;
  color: white;
  cursor: pointer;
  white-space: nowrap;
}
button:disabled { opacity: 0.5; cursor: default; }
.hint { font-size: 12px; color: #8a8a90; }
.hint-warning { color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 8px; margin: 4px 0; }
.model-tag { font-family: ui-monospace, monospace; font-size: 11px; color: #3f6212; background: #f0fdf4; display: inline-block; padding: 1px 6px; border-radius: 4px; }

.file-open-toast {
  display: flex; align-items: center; gap: 10px; justify-content: space-between;
  background: #fef2f2; color: #b4232f; border: 1px solid #fecaca; border-radius: 8px;
  padding: 8px 12px; font-size: 13px; margin-bottom: 16px;
}
.file-open-toast button { flex-shrink: 0; }

.step-bar {
  display: flex; gap: 8px; margin-bottom: 20px; padding: 10px; background: #fafafa;
  border-radius: 8px; flex-wrap: wrap; position: sticky; top: 0; z-index: 1;
}
.step-item {
  display: flex; align-items: center; gap: 4px; padding: 6px 10px; border-radius: 6px;
  font-size: 13px; font-family: inherit; color: #18181b; text-decoration: none; cursor: pointer;
  background: white; border: 1px solid #e4e4e7;
}
.step-item:hover { border-color: #a1a1aa; }
.step-item.active { background: #18181b; border-color: #18181b; color: white; }
.step-item.active .step-status { color: #d4d4d8; }
.step-num { font-weight: 600; }
.step-status { font-size: 11px; color: #71717a; margin-left: 2px; }

/* 生成产物缩略图统一给个手型光标，暗示"双击可以打开原文件" */
.pair-media img, .pair-media video, .character-cell img, .scene-ref img, .candidate-item img {
  cursor: pointer;
}
.error { font-size: 12px; color: #b4232f; white-space: pre-wrap; }
.tag {
  font-size: 11px;
  background: #f4f4f5;
  border-radius: 4px;
  padding: 2px 6px;
  margin-left: 8px;
  color: #52525b;
}

.list-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.list-head h2 { margin-bottom: 0; }
.refresh { background: white; color: #18181b; }
.projects-page-head { display: none; }
.project-list { list-style: none; padding: 0; margin: 0; }
.project-list li {
  padding: 10px 12px;
  border: 1px solid #e4e4e7;
  border-radius: 8px;
  margin-bottom: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
}
.project-list li:hover { background: #fafafa; }
.project-list li .tag { margin-left: 0; }
.project-index { flex-shrink: 0; min-width: 18px; text-align: right; color: #a1a1aa; font-size: 12px; font-variant-numeric: tabular-nums; }
.project-title-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-title-input {
  flex: 1; min-width: 0; padding: 4px 8px; border: 1px solid #18181b; border-radius: 6px;
  font-size: 14px; font-weight: 600; color: #18181b; background: white;
}
.project-title-edit { flex-shrink: 0; }
.project-delete { margin-left: auto; flex-shrink: 0; }
.project-actions { display: flex; align-items: center; gap: 6px; margin-left: auto; flex-shrink: 0; }
.project-actions .project-delete { margin-left: 0; }

.back { background: none; color: #18181b; border: none; padding: 0; margin-bottom: 12px; }
.warning-box {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  background: #fef3c7;
  color: #92400e;
  border: 1px solid #fcd34d;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
  margin-bottom: 12px;
}
.missing-shots-list { display: flex; flex-wrap: wrap; gap: 6px; }
.missing-shots-list .tag { margin-left: 0; background: white; }
/* 缺视频提示里的标签改成按钮，点了直接跳到「分镜」步骤对应的那一镜，不用自己去场次
   列表里翻——视觉上还是要长得像原来的 .tag，所以把 <button> 默认的边框/字体都重置掉。 */
.tag-jump {
  border: 1px solid #e4e4e7; font-family: inherit; cursor: pointer;
}
.tag-jump:hover { background: #eef2ff; border-color: #c7d2fe; color: #4338ca; }
.warning-box button {
  background: #92400e;
  border-color: #92400e;
  flex-shrink: 0;
}
.premise { color: #52525b; font-size: 13px; }
.project-overview { margin-bottom: 12px; }
.project-title-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.project-title-row h2 { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-title-row .tag { flex-shrink: 0; margin-left: auto; }
.premise-row { display: flex; align-items: flex-start; gap: 8px; }
.premise-row .premise { flex: 1; min-width: 0; margin: 0; }
.premise-toggle { flex-shrink: 0; }
.style-mode-label { font-size: 11px; font-weight: 600; color: #52525b; }
.style-mode-note { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.style-mode-picker { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.style-mode-picker label { display: flex; align-items: center; gap: 4px; cursor: pointer; }
.style-mode-picker-inline { margin: 4px 0 12px; }
.style-mode-picker-inline { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: thin; }
.style-mode-picker-inline > * { flex-shrink: 0; }
.story-box { display: flex; align-items: center; gap: 10px; margin: 12px 0 20px; flex-wrap: wrap; }

.ghost { background: white; color: #18181b; }
.ghost.danger { color: #b4232f; border-color: #fecaca; background: #fef2f2; }
.ghost.accent { color: white; border-color: #16a34a; background: #16a34a; }
.ghost.accent:hover:not(:disabled) { background: #15803d; border-color: #15803d; }
.ghost.accent:disabled { color: #d4d4d8; border-color: #e4e4e7; background: #f4f4f5; }
.terminal-test-button { color: #1d4ed8; border-color: #bfdbfe; background: #eff6ff; font-weight: 650; }
.terminal-test-button:hover:not(:disabled) { color: white; border-color: #2563eb; background: #2563eb; }
.ai-optimize-button { color: #c2410c; border-color: #fed7aa; background: #fff7ed; font-weight: 650; }
.ai-optimize-button:hover:not(:disabled) { color: white; border-color: #ea580c; background: #ea580c; }
.shot-add-button { color: #047857; border-color: #a7f3d0; background: #ecfdf5; font-weight: 700; }
.shot-add-button:hover:not(:disabled) { color: white; border-color: #059669; background: #059669; }
.terminal-test-button:disabled,
.ai-optimize-button:disabled,
.shot-add-button:disabled { color: #a1a1aa; border-color: #e4e4e7; background: #f4f4f5; }


.story-view-toggle { display: flex; align-items: center; gap: 8px; margin: 12px 0 16px; flex-wrap: wrap; }
.tab-btn { background: white; color: #71717a; border: 1px solid #e4e4e7; padding: 6px 12px; border-radius: 6px; font-size: 13px; }
.tab-btn.active { background: #18181b; color: white; border-color: #18181b; }

/* 剧本表格总览：场次是分组行(可展开/收起)，镜头是网格排列的子行，跟 Excel 分组/大纲
   模式一个思路——不用一场一场点进详情，格子直接改文字，@change 照旧存库。 */
.story-table { display: flex; flex-direction: column; gap: 10px; margin: 12px 0 20px; }
.story-table-group { border: 1px solid #e4e4e7; border-radius: 8px; overflow: hidden; }
.story-table-group-head {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px; background: #fafafa;
  border-bottom: 1px solid #e4e4e7; flex-wrap: wrap;
}
.story-table-scene-label {
  font-size: 12px; font-weight: 700; color: #1d4ed8; background: #eff6ff;
  padding: 2px 8px; border-radius: 999px; flex-shrink: 0;
}
.story-table-summary-input { flex: 1; min-width: 160px; }
.story-table-shots { padding: 8px 10px; overflow-x: auto; }
.story-table-row {
  display: grid;
  grid-template-columns: 36px 56px 80px 110px 100px 1.6fr 1.6fr 1fr 150px 30px;
  gap: 6px; align-items: start; padding: 4px 0;
}
.story-table-row + .story-table-row { border-top: 1px solid #f4f4f5; }
.story-table-row-header { font-size: 11px; color: #a1a1aa; padding-bottom: 6px; border-top: none !important; }
.story-table-row textarea { resize: vertical; min-height: 32px; font-size: 12px; }
.story-table-row input, .story-table-row select { font-size: 12px; }
.story-table-order { font-size: 12px; color: #71717a; padding-top: 6px; text-align: center; }
.story-table-row .ghost.danger { padding: 2px 6px; }
.story-table-character-cell { display: flex; flex-direction: column; gap: 3px; }
.import-box {
  border: 1px solid #e4e4e7; border-radius: 8px; padding: 12px; margin-bottom: 16px;
  background: #fafafa; display: flex; flex-direction: column; gap: 8px;
}
.import-box textarea { font-family: ui-monospace, monospace; font-size: 12px; }

.scene-summary-input { margin-top: 6px; font-weight: 500; flex: 1; min-width: 160px; }

.manual-page { display: flex; flex-direction: column; gap: 10px; }
.manual-page-head, .manual-search-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.manual-page-head h1 { margin: 0; }
.manual-page-head p { margin: 2px 0 0; }
.manual-result-count { font-size: 11px; }
.manual-box {
  border: 1px solid #e4e4e7; border-radius: 8px; padding: 12px; margin-bottom: 20px;
  background: #fafafa; display: flex; flex-direction: column; gap: 10px;
}
.manual-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.manual-tab { background: white; color: #18181b; border: 1px solid #e4e4e7; padding: 4px 10px; font-size: 12px; }
.manual-tab.active { background: #18181b; color: white; border-color: #18181b; }
.manual-search { max-width: 320px; }
.manual-list { display: flex; flex-direction: column; gap: 6px; max-height: 360px; overflow-y: auto; }
.manual-entry {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  background: white; border: 1px solid #e4e4e7; border-radius: 6px; padding: 8px 10px;
}
.manual-entry-text { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
.manual-entry-text code {
  font-family: ui-monospace, monospace; font-size: 12px; color: #3f6212; background: #f0fdf4;
  display: inline-block; padding: 1px 6px; border-radius: 4px; width: fit-content;
}
.manual-sub { margin-left: 0; margin-bottom: 2px; width: fit-content; }
.manual-copy { flex-shrink: 0; }

.character-box { margin-top: 20px; padding: 12px; border: 1px solid #e4e4e7; border-radius: 8px; background: #fafafa; }
.character-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
.character-cell { display: flex; flex-direction: column; gap: 6px; background: white; border: 1px solid #e4e4e7; border-radius: 8px; padding: 8px; }
.character-head { display: flex; align-items: center; justify-content: space-between; }
.character-cell img { width: 100%; border-radius: 6px; object-fit: cover; aspect-ratio: 1; }
.character-prompt-label { font-size: 11px; color: #8a8a90; }
.character-prompt-input { resize: vertical; font-family: inherit; }
.character-actions { display: flex; flex-direction: column; gap: 4px; }
.reuse-box { display: flex; flex-direction: column; gap: 6px; border-top: 1px dashed #e4e4e7; padding-top: 6px; margin-top: 2px; }
.reuse-results { display: flex; flex-direction: column; gap: 6px; max-height: 220px; overflow-y: auto; }
.reuse-item { display: flex; align-items: center; gap: 6px; border: 1px solid #e4e4e7; border-radius: 6px; padding: 4px; }
.reuse-item img { width: 36px; height: 36px; border-radius: 4px; object-fit: cover; flex-shrink: 0; }
.reuse-item-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.reuse-item-info strong { font-size: 12px; }
.reuse-item button { flex-shrink: 0; padding: 4px 8px; font-size: 12px; }

/* 生成中的缩略图用骨架屏脉冲替代纯文字状态，比"生成中…"这行字更直观地表达"这里正在处理" */
.skeleton-pulse {
  background: linear-gradient(90deg, #ececef 25%, #f7f7f8 37%, #ececef 63%);
  background-size: 400% 100%;
  animation: skeleton-pulse 1.4s ease infinite;
}
@keyframes skeleton-pulse {
  0% { background-position: 100% 50%; }
  100% { background-position: 0 50%; }
}

.scene-detail-header { margin-bottom: 12px; }
.scene-detail-title-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.scene-detail-meta-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
.scene-ref-label { font-size: 11px; color: #8a8a90; }

.fade-enter-active, .fade-leave-active { transition: opacity 0.12s ease, max-height 0.12s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* 状态语义色：红=未完成(待处理)、蓝=生成中、绿=完成、失败同样是红——未完成和失败都
   用红色提醒"这里还需要处理"，跟生成中的蓝、完成的绿区分开；用在生成按钮、媒体预览框
   边框、状态点上，三处样式统一，扫一眼颜色就知道进度。 */
.status-pending { border-color: #dc2626 !important; color: #dc2626; }
.status-running { border-color: #2563eb !important; color: #2563eb; }
.status-completed { border-color: #16a34a !important; color: #16a34a; }
.status-failed { border-color: #dc2626 !important; color: #dc2626; }
.status-dot-group { display: flex; gap: 3px; align-items: center; }
.status-dot { width: 7px; height: 7px; border-radius: 999px; background: #dc2626; flex-shrink: 0; }
.status-dot.status-pending { background: #dc2626; }
.status-dot.status-running { background: #2563eb; }
.status-dot.status-completed { background: #16a34a; }
.status-dot.status-failed { background: #dc2626; }
.shot-asset-statuses { display: inline-flex; align-items: center; gap: 3px; flex-shrink: 0; }
.asset-status-mini {
  display: inline-flex; align-items: center; justify-content: center; width: 17px; height: 17px;
  border: 1px solid #e4e4e7; border-radius: 4px; color: #a1a1aa; font-size: 9px; font-weight: 600;
}
.asset-status-mini.status-pending { color: #b4232f; border-color: #fecaca; background: #fef2f2; }
.asset-status-mini.status-running { color: #2563eb; border-color: #bfdbfe; background: #eff6ff; }
.asset-status-mini.status-completed { color: #15803d; border-color: #bbf7d0; background: #f0fdf4; }
.asset-status-mini.status-failed { color: #b4232f; border-color: #fecaca; background: #fef2f2; }
.shot-stage-heading {
  display: flex; align-items: center; gap: 8px; min-height: 34px; margin: 10px 0 6px;
  padding: 5px 8px; border: 1px solid #e4e4e7; border-radius: 6px; background: #fafafa;
}
.shot-stage-heading > div { display: flex; align-items: baseline; gap: 8px; flex: 1; min-width: 0; }
.shot-stage-heading strong { font-size: 12px; }
.shot-stage-heading div span { color: #8a8a90; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.shot-stage-num {
  display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px;
  border-radius: 999px; background: #18181b; color: white; font-size: 10px; font-weight: 700; flex-shrink: 0;
}
.shot-stage-status { font-size: 10px; flex-shrink: 0; }
.shot-stage-content { margin-bottom: 8px; }

/* 右上角"总览模式/编辑模式"切换：跟剧本步骤的 story-view-toggle 共用 .tab-btn 样式，
   保持视觉一致。 */
.view-mode-toggle { display: flex; gap: 6px; margin-left: auto; }

/* 总览模式：参考图一览 + 分镜一览两块，都是"先看完再决定要不要批量生成"的浏览体验，
   不追求跟编辑模式手风琴一样的信息密度，卡片更大、更适合扫视。 */
.overview-section { margin: 16px 0 28px; }
.overview-section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.overview-section-head h3 { margin: 0; font-size: 15px; font-weight: 600; }
.overview-section-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.overview-subhead { font-size: 12px; color: #71717a; margin: 12px 0 8px; }
.overview-ref-grid, .overview-shot-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 10px; margin-bottom: 8px;
}
.overview-ref-card, .overview-shot-card {
  display: flex; flex-direction: column; gap: 6px; padding: 0; border: none; background: none;
  cursor: pointer; text-align: left; font: inherit; color: inherit;
}
.overview-ref-thumb {
  position: relative; height: 64px; border-radius: 10px; background: #f4f4f5; border: 1px solid #e4e4e7;
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.overview-ref-thumb img { width: 100%; height: 100%; object-fit: cover; }
.overview-ref-placeholder { font-style: normal; font-size: 12px; color: #a1a1aa; }
.overview-ref-dot { position: absolute; top: 5px; right: 5px; width: 7px; height: 7px; border-radius: 50%; }
.overview-ref-label { font-size: 11px; color: #52525b; text-align: center; }
.overview-shot-media {
  position: relative; height: 84px; border-radius: 10px; background: #f4f4f5; border: 1px solid #e4e4e7;
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.overview-shot-media img { width: 100%; height: 100%; object-fit: cover; }
.overview-shot-placeholder { font-style: normal; font-size: 11px; color: #a1a1aa; padding: 0 6px; text-align: center; }
.overview-shot-dot { position: absolute; top: 6px; right: 6px; width: 8px; height: 8px; border-radius: 50%; }
.overview-shot-video-badge {
  position: absolute; bottom: 6px; right: 6px; font-size: 9px; color: #16a34a; background: white;
  border-radius: 50%; width: 16px; height: 16px; display: flex; align-items: center; justify-content: center;
}
.overview-shot-refresh {
  position: absolute; bottom: 5px; left: 5px; width: 20px; height: 20px; border-radius: 6px; font-size: 12px;
  border: 1px solid #e4e4e7; background: white; cursor: pointer; line-height: 1; padding: 0;
}
.overview-shot-refresh:disabled { opacity: 0.5; cursor: default; }
.overview-shot-label { font-size: 11px; color: #71717a; }
.overview-cta-bar {
  display: flex; align-items: center; justify-content: center; gap: 12px; padding: 14px; margin-top: 12px;
  background: #eff6ff; border-radius: 10px; font-size: 13px; color: #1d4ed8;
}

/* 场次手风琴：一行一场戏，点头部展开/收起，展开的那一场把镜头列表铺在下面，
   跟"剧本"步骤的表格总览是同一个设计语言，不用左边网格+右边详情两栏来回看。 */
.scene-accordion { display: flex; flex-direction: column; gap: 8px; margin: 12px 0 24px; }
.scene-row { border: 1px solid #e4e4e7; border-radius: 10px; overflow: hidden; background: white; }
.scene-row.dragging { opacity: 0.4; }
.scene-row.drag-over { border-color: #18181b; border-style: dashed; }
.scene-row-head {
  display: flex; align-items: center; gap: 10px; padding: 8px 12px; cursor: pointer;
  transition: background-color 0.12s ease;
}
.scene-row-head:hover { background: #fafafa; }
.scene-row.expanded > .scene-row-head { background: #fafafa; border-bottom: 1px solid #e4e4e7; }
.scene-row-thumb-wrap { display: flex; flex-direction: column; align-items: center; gap: 3px; flex-shrink: 0; }
.scene-row-thumb { width: 84px; height: 64px; border-radius: 6px; background: #f4f4f5; overflow: hidden; flex-shrink: 0; }
.scene-row-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.scene-row-thumb-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #a1a1aa; }
.scene-row-progress { font-size: 11px; font-weight: 700; line-height: 1; color: #52525b; }
.scene-row-progress.is-incomplete { color: #dc2626; }
.scene-row-title { flex-shrink: 0; font-size: 13px; }
.scene-row-summary {
  flex: 1; min-width: 0; margin: 0; font-size: 12px; color: #52525b; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.scene-row-body { padding: 14px; }
.scene-row-add, .shot-row-add { margin-top: 4px; align-self: flex-start; }

.scene-ref { display: flex; gap: 12px; align-items: flex-start; background: #fafafa; border-radius: 8px; padding: 8px; margin-bottom: 14px; }
/* 生成图(左) + 上传的参考图(右) 并排放，方便直接对比"喂进去的图"和"生成出来的图"，
   不用再去小小的路径输入框旁边找那个 28px 的缩略图。两个格子同尺寸，右边没上传时
   显示占位提示，不会因为没参考图就整体错位。 */
.scene-ref-media { display: flex; gap: 8px; flex-shrink: 0; }
.scene-ref-preview { width: 96px; height: 96px; flex-shrink: 0; position: relative; }
.scene-ref-preview > img { width: 100%; height: 100%; object-fit: cover; border-radius: 6px; }
.scene-ref-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; border: 1px solid #e4e4e7; border-radius: 6px; background: #f4f4f5; color: #a1a1aa; font-size: 11px; text-align: center; padding: 4px; box-sizing: border-box; }
.scene-ref-upload-preview .scene-ref-placeholder { border-style: dashed; background: transparent; }
.scene-ref-media-tag {
  position: absolute; left: 3px; bottom: 3px; font-size: 9px; padding: 1px 5px; border-radius: 999px;
  background: rgba(0, 0, 0, 0.55); color: white; line-height: 1.4;
}
.scene-ref-info { flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.scene-ref-title-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.scene-ref-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.ref-path-row { display: flex; gap: 6px; align-items: center; }
.ref-pick-preview { width: 28px; height: 28px; border-radius: 6px; object-fit: cover; border: 1px solid #e4e4e7; flex-shrink: 0; }
.ref-path-row input { flex: 1; min-width: 0; }
.ref-path-row button { flex-shrink: 0; }
.ref-preview-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.ref-preview-row .ref-pick-preview { width: 44px; height: 44px; }
/* 缩略图右上角叠一个删除角标，鼠标悬停时才显出来，不然一排缩略图全带个×太抢眼。 */
.ref-pick-thumb { position: relative; display: inline-flex; flex-shrink: 0; }
.ref-pick-remove {
  position: absolute; top: -6px; right: -6px; width: 16px; height: 16px; padding: 0;
  border-radius: 50%; border: 1px solid #e4e4e7; background: white; color: #71717a;
  font-size: 11px; line-height: 1; display: flex; align-items: center; justify-content: center;
  opacity: 0; transition: opacity 0.12s ease;
}
.ref-pick-thumb:hover .ref-pick-remove { opacity: 1; }
.ref-pick-remove:hover { background: #ef4444; border-color: #ef4444; color: white; }
.manual-inline-select {
  margin-top: 4px; font-size: 12px; color: #52525b; background: #fafafa;
  border: 1px dashed #d4d4d8; padding: 4px 6px;
}

/* 镜头行：默认收起成一条紧凑预览(缩略图+一行画面描述+三个状态点)，点开展开完整表单。
   视频是竖屏的，展开表单里用"竖版媒体预览(左) + 文字/按钮(右)"配对，参照 Figma/Premiere
   画布在一侧、属性面板在另一侧的惯例，因果关系(文字→结果)挨在一起，不用来回找对应。 */
/* 批量操作工具条：只在选中至少一镜时出现，仿 liblib.tv 那类"框选后弹出批量操作"的
   模式；没选中任何镜头、但这场戏有不止一镜时，给一个"全选本场镜头"的轻提示入口，
   不然用户可能压根不知道镜头前面那个复选框能干什么。 */
.batch-toolbar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 10px; margin-bottom: 8px;
  background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px;
}
.batch-toolbar-empty { background: #fafafa; border-color: #e4e4e7; }
.batch-toolbar-select-all { margin-left: auto; }
.batch-toolbar-count { font-size: 12px; font-weight: 600; color: #1d4ed8; margin-right: 4px; }
.shot-row-checkbox { flex-shrink: 0; width: auto; margin: 0; }

.shot-list { display: flex; flex-direction: column; gap: 8px; }
.shot-tabs, .shot-media-pane { display: none; }
.shot-row { border: 1px solid #e4e4e7; border-radius: 8px; overflow: hidden; }
.shot-row.dragging { opacity: 0.4; }
.shot-row.drag-over { border-color: #18181b; border-style: dashed; }
.shot-row-head { display: flex; align-items: center; gap: 8px; padding: 6px 10px; cursor: pointer; }
.shot-row-head:hover { background: #fafafa; }
.shot-row.expanded > .shot-row-head { background: #fafafa; border-bottom: 1px solid #e4e4e7; }
.shot-row-expand { width: 22px; flex-shrink: 0; padding: 2px 0; text-align: center; font-size: 11px; }
.shot-row-thumb {
  width: 42px; height: 42px; border-radius: 6px; background: #f4f4f5; display: flex; align-items: center;
  justify-content: center; font-size: 11px; color: #a1a1aa; overflow: hidden; flex-shrink: 0;
}
.shot-row-thumb img { width: 100%; height: 100%; object-fit: cover; }
.shot-row-thumb-skeleton { width: 100%; height: 100%; display: block; }
.shot-row-order { font-size: 11px; color: #71717a; flex-shrink: 0; }
.shot-row-caption {
  flex: 1; min-width: 0; margin: 0; font-size: 12px; color: #52525b; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.shot-row-body { padding: 12px; }

.shot-meta-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.shot-meta-field { display: flex; flex-direction: column; gap: 3px; min-width: 140px; flex: 1; }
.shot-meta-field label { font-size: 11px; color: #8a8a90; }
.shot-meta-field-narrow { min-width: 90px; max-width: 110px; flex: 0 0 auto; }

/* 角色缩略头像：按 characterName 的自由文本去角色库找同名头像。折叠行里的紧凑预览
   (char-thumb-row-inline)只是纯展示，不带删除。可编辑的两处(表格总览/镜头详情表单)
   做成"头像+名字+×"一整条胶囊——之前×是叠在头像正上方的圆点，15px 的按钮压在 22px
   的头像上几乎把图挡住一半，现在改成头像和×分开放在胶囊两端，中间插名字，互不遮挡。 */
.char-thumb-row { display: flex; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
.char-thumb-row img { width: 22px; height: 22px; border-radius: 999px; object-fit: cover; border: 1px solid #e4e4e7; }
.char-thumb-row-inline { flex-shrink: 0; margin-top: 0; }
.char-thumb-chip {
  display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0;
  padding: 2px 4px 2px 2px; border: 1px solid #e4e4e7; border-radius: 999px; background: white;
}
.char-thumb-chip img { width: 18px; height: 18px; border: none; }
.char-thumb-name {
  font-size: 11px; color: #52525b; white-space: nowrap; max-width: 72px; overflow: hidden; text-overflow: ellipsis;
}
.char-thumb-remove {
  width: 14px; height: 14px; padding: 0; flex-shrink: 0; border: none; background: transparent;
  border-radius: 999px; color: #a1a1aa; font-size: 12px; line-height: 1;
  display: flex; align-items: center; justify-content: center;
}
.char-thumb-remove:hover { background: #dc2626; color: white; }
.shot-row-emotion-tag { flex-shrink: 0; margin-left: 0; }

/* 每一对：竖版媒体预览在左(接近 9:16，因为 Seedance 默认出竖屏视频)，文字+按钮在右。
   预览框边框颜色跟随生成状态变化，按钮同理，两处配色呼应。 */
.pair-row { display: flex; gap: 12px; margin-bottom: 14px; align-items: flex-start; }
.pair-media {
  width: 130px; aspect-ratio: 9 / 16; flex-shrink: 0; border: 2px solid #e4e4e7; border-radius: 8px;
  background: #f4f4f5; display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.pair-media img, .pair-media video { width: 100%; height: 100%; object-fit: cover; }
.pair-media-audio { aspect-ratio: auto; width: 130px; min-height: 64px; height: auto; background: white; padding: 6px; }
.pair-media-audio audio { width: 100%; }
.pair-media-skeleton { width: 100%; height: 100%; display: block; }
.pair-media-empty { font-size: 11px; color: #a1a1aa; text-align: center; padding: 0 6px; }
.pair-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.pair-text label { font-size: 12px; color: #8a8a90; }
.pair-text-label-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.pair-text-clear { flex-shrink: 0; padding: 1px 8px; font-size: 11px; }
.pair-action-row { display: flex; align-items: center; gap: 8px; margin-top: 2px; flex-wrap: wrap; }
.pair-action { align-self: flex-start; margin-top: 2px; }
.pair-action-row .pair-action { margin-top: 0; }
/* 按钮是深色实心底，跟浅色预览框不是一套底色，同样的状态色直接套边框不够醒目，
   这里针对按钮场景单独定一套背景色：待处理/未完成=红色实心(提醒还没生成)、生成中=蓝色实心、
   已完成=白底绿边(次要动作，"已经有了，点了会重新生成")、失败=红色实心(催促重试)。 */
.pair-action.status-pending { background: #dc2626; border-color: #dc2626; color: white; }
.pair-action.status-running { background: #2563eb; border-color: #2563eb; color: white; }
.pair-action.status-completed { background: white; border-color: #16a34a; color: #16a34a; }
.pair-action.status-failed { background: #dc2626; border-color: #dc2626; color: white; }

/* 第三梯队：折叠起来的辅助控制，跟核心内容拉开视觉权重差距 */
.shot-advanced { margin-top: 12px; border-top: 1px dashed #e4e4e7; padding-top: 8px; }
.shot-advanced summary { font-size: 12px; color: #71717a; cursor: pointer; user-select: none; }
.shot-advanced summary:hover { color: #52525b; }
.advanced-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 10px; }
.advanced-cell { display: flex; flex-direction: column; gap: 6px; }
.advanced-cell-title { font-size: 11px; color: #a1a1aa; text-transform: uppercase; letter-spacing: 0.03em; }

.candidate-gallery { margin-top: 8px; }
.candidate-list { display: flex; gap: 8px; flex-wrap: wrap; }
.candidate-item { position: relative; width: 72px; }
.candidate-item img { width: 72px; height: 72px; object-fit: cover; border-radius: 6px; cursor: pointer; border: 2px solid transparent; }
.candidate-item.selected img { border-color: #18181b; }
.candidate-placeholder { width: 72px; height: 72px; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #71717a; background: #f4f4f5; border-radius: 6px; }
.candidate-item .tag { position: absolute; top: 2px; right: 2px; font-size: 9px; background: #18181b; color: white; padding: 1px 4px; border-radius: 4px; }

.export-box { margin-top: 32px; border-top: 1px solid #e4e4e7; padding-top: 16px; }

.poster-create-panel {
  display: flex; flex-direction: column; gap: 12px; padding: 14px; border: 1px solid #e4e4e7;
  border-radius: 10px; background: #fafafa; margin: 12px 0 20px; max-width: 520px;
}
.poster-preset-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.poster-template-card { position: relative; }
.poster-template-delete {
  position: absolute; top: 4px; right: 6px; cursor: pointer; font-size: 14px; line-height: 1;
  color: #a1a1aa;
}
.poster-template-delete:hover { color: #dc2626; }
.poster-card-body-lines { margin: 0; padding-left: 18px; font-size: 12px; color: #52525b; }
.poster-card-body-lines li { margin: 2px 0; }
.poster-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }
.poster-card { border: 1px solid #e4e4e7; border-radius: 10px; overflow: hidden; background: white; display: flex; flex-direction: column; }
.poster-card-media {
  aspect-ratio: 3 / 4; background: #f4f4f5; display: flex; align-items: center; justify-content: center;
  color: #a1a1aa; font-size: 12px; border-bottom: 1px solid #e4e4e7;
}
.poster-card-media img { width: 100%; height: 100%; object-fit: cover; cursor: pointer; }
.poster-card-info { display: flex; flex-direction: column; gap: 6px; padding: 10px; }
.poster-card-title-row { display: flex; align-items: center; gap: 6px; }
.poster-card-title-row .tag { margin-left: 0; }
.poster-card-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.text-image-create-panel { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 16px; align-items: start; }
.text-image-form-card {
  padding: 16px; border: 1px solid #e4e4e7; border-radius: 13px; background: white;
}
.text-image-prompt-field textarea { min-height: 150px; resize: vertical; line-height: 1.55; }
.text-image-options-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.text-image-choice-row { align-items: center; flex-wrap: wrap; gap: 8px; }
.text-image-ref-preview-wrap {
  display: inline-flex; align-items: center; gap: 8px; margin-top: 8px; padding: 6px 8px;
  border: 1px solid #e4e4e7; border-radius: 9px; background: #fafafa; color: #71717a; font-size: 12px;
}
.text-image-ref-preview { width: 52px; height: 52px; }
.text-image-preview-panel {
  position: sticky; top: 16px; display: flex; flex-direction: column; gap: 12px;
  padding: 15px; border: 1px solid #e4e4e7; border-radius: 13px; background: #fbfbfd;
}
.text-image-preview-canvas {
  position: relative; display: grid; place-items: end start; width: 100%; aspect-ratio: 3 / 4;
  overflow: hidden; border-radius: 12px; padding: 18px; box-sizing: border-box; background: #111827; color: white;
}
.text-image-preview-canvas.orientation-landscape { aspect-ratio: 4 / 3; }
.text-image-preview-bg {
  position: absolute; inset: 0;
  background:
    radial-gradient(circle at 20% 20%, rgba(56, 189, 248, .55), transparent 30%),
    radial-gradient(circle at 76% 28%, rgba(251, 191, 36, .48), transparent 26%),
    linear-gradient(145deg, #111827, #334155 52%, #0f172a);
}
.text-image-preview-canvas p {
  position: relative; margin: 0; max-width: 90%; color: rgba(255,255,255,.9);
  font-size: 12px; line-height: 1.55; text-shadow: 0 1px 8px rgba(0,0,0,.45);
  display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 6;
}
.text-image-create-actions { margin: 0; flex-direction: column; align-items: stretch; }
.text-image-create-actions button { width: 100%; }
.text-image-grid { grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.text-image-card-media.orientation-landscape { aspect-ratio: 4 / 3; }
.text-image-card-media img { object-fit: contain; background: #111827; }
.export-box video { max-width: 100%; margin-top: 8px; border-radius: 6px; }

/* 响应式：窗口窄的时候（Electron 窗口拖小、或者笔记本小屏幕）几个并排布局会挤得
   看不清，改成竖排/单列；侧边栏也收窄省地方。窗口宽的时候维持原来的并排布局，
   不额外做"大屏幕右边多显示点什么"的特殊内容——那样会让布局逻辑分裂成两套，
   目前 panel-wide 放宽到 1400px 已经能吃掉大部分空白，够用。 */
@media (max-width: 960px) {
  .sidebar { width: 180px; padding: 16px 10px; }
  .main-content { padding: 16px 16px 48px; }
  .pair-row { flex-direction: column; }
  .pair-media, .pair-media-audio { width: 100%; max-width: 220px; }
  .advanced-grid { grid-template-columns: 1fr; }
  .character-grid { grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); }
  /* 窄屏不硬挤成单列(表格挤成单列就跟"从上到下一眼看完"的初衷矛盾了)，改成让每一行
     保持最小可用宽度，容器横向滚动着看 */
  .story-table-row { min-width: 820px; }
}

@media (max-width: 640px) {
  .shell { flex-direction: column; }
  .sidebar { width: 100%; height: auto; position: static; flex-direction: row; align-items: center; gap: 12px; }
  .sidebar-nav { flex-direction: row; }
  .sidebar-current { display: none; }
  .main-content { height: auto; }
  .step-bar { position: static; }
}

/* V2 UI：苹果式圆润表面语言。V1 的样式完整保留，仅在 shell.ui-v2 下覆盖视觉层。 */
.sidebar-footer { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }

.shell.ui-v2 {
  min-height: 100vh; padding: 8px; box-sizing: border-box; background: #f5f5f7;
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
}
.ui-v2 .sidebar {
  width: 190px; height: calc(100vh - 16px); padding: 14px 10px; gap: 18px; background: #fbfbfd;
  border: 1px solid #dedee3; border-right-color: #e4e4e7; border-radius: 18px 0 0 18px;
}
.ui-v2 .main-content {
  height: calc(100vh - 16px); padding: 22px 26px 56px; background: white;
  border: 1px solid #dedee3; border-left: 0; border-radius: 0 18px 18px 0;
}
.ui-v2 .sidebar-brand { padding: 0 6px; font-size: 15px; }
.ui-v2 .sidebar-nav { gap: 16px; }
.ui-v2 .sidebar-nav-group { gap: 3px; }
.ui-v2 .sidebar-nav-label {
  padding: 0 8px 4px; color: #8a8a90; font-size: 10px; font-weight: 750;
  text-transform: uppercase; letter-spacing: .06em;
}
.ui-v2 .sidebar-nav-group-settings { padding-top: 12px; border-top: 1px solid #e4e4e7; }
.ui-v2 .sidebar-nav button { min-height: 34px; padding: 6px 9px; border-radius: 9px; }
.ui-v2 .nav-icon { width: 16px; height: 16px; stroke-width: 1.65; }
.ui-v2 .sidebar-nav button.active {
  color: #18181b; background: #ececf0; border-color: transparent;
}
.ui-v2 .sidebar-current {
  padding: 10px; background: white; border: 1px solid #e4e4e7; border-radius: 12px;
}
.ui-v2 .sidebar-current strong {
  display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  line-height: 1.45;
}
.ui-v2 .api-status {
  display: inline-flex; align-items: center; min-height: 20px; padding: 1px 7px;
  border: 1px solid currentColor; border-radius: 999px; font-size: 10px; font-weight: 600;
}
.ui-v2 .api-status.ok { color: #047857; background: #ecfdf5; border-color: #a7f3d0; }
.ui-v2 .api-status.error { color: #b4232f; background: #fef2f2; border-color: #fecaca; }
.ui-v2 .api-status.checking { color: #71717a; background: #f4f4f5; border-color: #d4d4d8; }

.ui-v2 button, .ui-v2 input, .ui-v2 textarea, .ui-v2 select { border-radius: 8px; font-size: 13px; }
.ui-v2 button { min-height: 32px; padding: 6px 12px; }
.ui-v2 input, .ui-v2 textarea, .ui-v2 select { min-height: 34px; padding: 7px 9px; background: #fbfbfd; }
.ui-v2 button:focus-visible, .ui-v2 input:focus-visible,
.ui-v2 textarea:focus-visible, .ui-v2 select:focus-visible {
  outline: 2px solid #18181b; outline-offset: 1px;
}
.ui-v2 .step-bar {
  gap: 4px; margin-bottom: 10px; padding: 4px; background: #f2f2f4;
  border: 0; border-radius: 12px;
}
.ui-v2 .step-item {
  min-height: 32px; padding: 5px 11px; border: 0; border-radius: 9px; background: transparent;
}
.ui-v2 .step-item:hover { background: #e9e9ed; border-color: transparent; }
.ui-v2 .step-item.active { color: #18181b; background: white; border-color: transparent; }
.ui-v2 .step-item.active .step-status { color: #71717a; }
.ui-v2 .project-overview {
  padding: 14px 16px; margin-bottom: 14px; border: 1px solid #e4e4e7;
  border-radius: 12px; background: #fbfbfd;
}
.ui-v2 .projects-page-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-bottom: 14px; padding-bottom: 12px; border-bottom: 1px solid #e4e4e7;
}
.ui-v2 .projects-page-head h1 { margin: 0 0 2px; font-size: 20px; line-height: 1.2; }
.ui-v2 .projects-page-head p { margin: 0; }
.ui-v2 .compact-page-head > div {
  display: flex; align-items: baseline; gap: 10px; min-width: 0; flex: 1;
}
.ui-v2 .compact-page-head h1 { flex-shrink: 0; margin: 0; }
.ui-v2 .compact-page-head p {
  min-width: 0; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ui-v2 .compact-page-head > button,
.ui-v2 .compact-page-head > span { flex-shrink: 0; }
.ui-v2 .projects-page-head h1,
.ui-v2 .settings-page-head h1,
.ui-v2 .manual-page-head h1 {
  color: #0f766e;
}
.ui-v2 .project-create-panel {
  padding: 16px; margin-bottom: 22px; border: 1px solid #e4e4e7; border-radius: 12px; background: #fbfbfd;
}
.ui-v2 .project-create-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
.ui-v2 .project-create-head > div {
  display: flex; align-items: baseline; gap: 8px; min-width: 0;
}
.ui-v2 .project-create-panel h2 { margin: 0; font-size: 16px; }
.ui-v2 .project-create-head h2,
.ui-v2 .project-list-head h2 {
  color: #2563eb;
}
.ui-v2 .project-create-head p {
  min-width: 0; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ui-v2 .project-create-head > span { padding: 3px 8px; border-radius: 999px; background: #ececf0; color: #71717a; font-size: 10px; font-weight: 700; }
.ui-v2 .project-create-panel > .hint { display: block; margin: 10px 0 0; line-height: 1.55; }
.ui-v2 .project-create-form {
  display: grid; grid-template-columns: minmax(320px, 1fr) auto auto; align-items: center;
  gap: 14px; margin: 0;
}
.ui-v2 .project-create-form textarea { min-height: 68px; padding: 10px 11px; line-height: 1.5; resize: vertical; }
/* 新建项目表单：改成"配置一行一类 -> 简介输入 -> 创建"的纵向结构，不再是选项和
   文本框挤在同一行——每组单选(项目类型/内容类型/出图风格)各占一整行，看着不串。 */
.ui-v2 .project-create-form { display: flex; flex-direction: column; gap: 12px; }
.ui-v2 .project-create-config { display: flex; flex-direction: column; gap: 12px; width: 100%; }
.ui-v2 .project-create-step-title { display: flex; align-items: center; gap: 9px; width: 100%; margin-top: 2px; }
.ui-v2 .project-create-step-title > span {
  display: inline-flex; align-items: center; justify-content: center; width: 25px; height: 25px;
  border-radius: 999px; background: #2563eb; color: white; font-size: 11px; font-weight: 800; flex-shrink: 0;
}
.ui-v2 .project-create-step-title > div { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.ui-v2 .project-create-step-title strong { font-size: 13px; }
.ui-v2 .project-create-step-title small {
  min-width: 0; color: #8a8a90; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ui-v2 .project-template-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; width: 100%; }
.ui-v2 .project-template-card {
  position: relative; display: flex; flex-direction: column; align-items: flex-start; justify-content: center;
  min-height: 82px; padding: 12px 36px 12px 13px; border: 1px solid #e4e4e7; border-radius: 11px;
  background: white; color: #3f3f46; text-align: left; white-space: normal;
}
.ui-v2 .project-template-card:hover { border-color: #a1a1aa; background: #fafafa; }
.ui-v2 .project-template-card.active { border-color: #18181b; background: white; color: #18181b; }
.ui-v2 .project-template-card strong { font-size: 14px; }
.ui-v2 .project-template-card small { margin-top: 5px; color: #8a8a90; font-size: 11px; }
.ui-v2 .project-template-check {
  position: absolute; top: 10px; right: 10px; display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border: 1px solid #d4d4d8; border-radius: 999px; color: white; font-size: 11px;
}
.ui-v2 .project-template-card.active .project-template-check { border-color: #18181b; background: #18181b; }
.ui-v2 .project-custom-options { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; padding: 10px; border: 1px solid #dbeafe; border-radius: 10px; background: #f8fbff; }
.ui-v2 .project-create-form .style-mode-picker {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  padding: 9px 11px; border: 1px solid #e4e4e7; border-radius: 9px; background: white;
}
.ui-v2 .project-create-form .style-mode-picker label {
  display: inline-flex; flex-direction: row; align-items: center; gap: 4px;
  width: max-content; min-width: max-content; white-space: nowrap; color: #52525b;
}
.ui-v2 .project-create-form .style-mode-picker input { width: 14px; height: 14px; margin: 0; }
.ui-v2 .project-create-input-row { display: flex; align-items: stretch; gap: 14px; width: 100%; }
.ui-v2 .project-create-input-row textarea { flex: 1; min-width: 0; }
.ui-v2 .project-create-input-row > button { min-width: 84px; align-self: stretch; font-weight: 600; }
.ui-v2 .project-list-head { margin-bottom: 10px; }
.ui-v2 .project-list {
  overflow: hidden; border: 1px solid #e4e4e7; border-radius: 12px; background: white;
}
.ui-v2 .project-list li {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto auto auto auto auto; gap: 7px;
  min-height: 52px; margin: 0; padding: 10px 13px; border: 0; border-bottom: 1px solid #e4e4e7; border-radius: 0;
}
.ui-v2 .project-delete { margin-left: 0; }
.ui-v2 .project-actions { margin-left: 0; }
.ui-v2 .project-list li:last-child { border-bottom: 0; }
.ui-v2 .project-list li strong {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px;
}
.ui-v2 .project-title-input { font-size: 13px; }
.ui-v2 .project-list li .tag { margin-left: 0; white-space: nowrap; }
.ui-v2 .posters-page { max-width: 1180px; }
.ui-v2 .poster-create-panel { max-width: none; padding: 0; border: 0; background: transparent; margin: 0; }
.ui-v2 .poster-create-layout { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 20px; align-items: start; }
.ui-v2 .poster-form-main { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.ui-v2 .poster-form-section { padding: 16px; border: 1px solid #e4e4e7; border-radius: 13px; background: white; }
.ui-v2 .poster-step-head { display: flex; align-items: center; gap: 10px; padding-bottom: 13px; margin-bottom: 15px; border-bottom: 1px solid #e4e4e7; }
.ui-v2 .poster-step-head > span, .ui-v2 .poster-step-number {
  display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px;
  border-radius: 999px; background: #ea580c; color: white; font-size: 11px; font-weight: 800; flex-shrink: 0;
}
.ui-v2 .poster-step-head > div, .ui-v2 .poster-visual-settings summary > div {
  display: flex; align-items: baseline; gap: 8px; min-width: 0;
}
.ui-v2 .poster-step-head strong, .ui-v2 .poster-visual-settings summary strong { font-size: 14px; }
.ui-v2 .poster-step-head strong,
.ui-v2 .poster-visual-settings summary strong,
.ui-v2 .poster-preview-head strong,
.ui-v2 .poster-create-actions > div > strong {
  color: #c2410c;
}
.ui-v2 .poster-step-head small, .ui-v2 .poster-visual-settings summary small {
  min-width: 0; color: #8a8a90; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ui-v2 .poster-form-section .field { margin-bottom: 16px; gap: 7px; }
.ui-v2 .poster-form-section .field:last-child { margin-bottom: 0; }
.ui-v2 .poster-form-section .field > label { font-weight: 650; color: #27272a; }

/* 图生视频表单精简：label 和单行输入内容放一行，不再各占一行浪费竖向空间。 */
.video-gen-form-main { max-width: 720px; }
.ui-v2 .field-inline { display: flex; flex-direction: row; align-items: center; gap: 12px; }
.ui-v2 .field-inline > label { flex-shrink: 0; min-width: 64px; margin: 0; }
.ui-v2 .field-inline > .field-inline-content { flex: 1; min-width: 0; }
.video-gen-prompt-row { display: flex; flex-direction: column; gap: 8px; }
.ref-pick-preview-inline { height: 34px; width: 34px; object-fit: cover; border-radius: 6px; flex-shrink: 0; }
.ui-v2 .poster-orientation-picker { display: flex; gap: 8px; }
.ui-v2 .poster-orientation-picker label {
  display: inline-flex; align-items: center; gap: 6px; min-height: 38px; padding: 7px 12px;
  border: 1px solid #e4e4e7; border-radius: 9px; background: #fbfbfd; white-space: nowrap;
}
.ui-v2 .poster-preset-grid { grid-template-columns: repeat(auto-fill, minmax(112px, 1fr)); gap: 7px; }
.ui-v2 .poster-template-card { min-height: 60px; padding: 9px 30px 9px 32px; }
.ui-v2 .poster-template-card strong { font-size: 12px; line-height: 1.3; }
.ui-v2 .poster-template-card .project-template-check { top: 8px; left: 8px; right: auto; width: 18px; height: 18px; }
.ui-v2 .poster-template-delete { top: 9px; right: 10px; }
.ui-v2 .poster-custom-template { padding: 14px; border: 1px solid #dbeafe; border-radius: 11px; background: #f8fbff; }
.ui-v2 .poster-custom-template .field:last-child { margin-bottom: 0; }
.ui-v2 .poster-custom-template .style-mode-picker,
.ui-v2 .poster-visual-settings .style-mode-picker {
  display: flex; align-items: center; flex-wrap: nowrap; gap: 12px; overflow-x: auto;
}
.ui-v2 .poster-custom-template .style-mode-picker > *,
.ui-v2 .poster-visual-settings .style-mode-picker > * { flex-shrink: 0; }
.ui-v2 .poster-custom-template .style-mode-picker label,
.ui-v2 .poster-visual-settings .style-mode-picker label {
  display: inline-flex; flex-direction: row; align-items: center; gap: 5px;
  width: max-content; min-width: max-content; white-space: nowrap;
}
.ui-v2 .poster-custom-template .style-mode-picker input,
.ui-v2 .poster-visual-settings .style-mode-picker input { width: 14px; height: 14px; margin: 0; }
.ui-v2 .poster-visual-settings { padding: 13px 16px; border: 1px solid #e4e4e7; border-radius: 13px; background: #fbfbfd; }
.ui-v2 .poster-visual-settings summary { display: flex; align-items: center; gap: 10px; cursor: pointer; list-style: none; }
.ui-v2 .poster-visual-settings summary::-webkit-details-marker { display: none; }
.ui-v2 .poster-visual-settings-body { display: flex; flex-direction: column; gap: 14px; padding-top: 15px; }
.ui-v2 .poster-create-actions {
  display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 15px 16px;
  border: 1px solid #d4d4d8; border-radius: 13px; background: white;
}
.ui-v2 .poster-create-actions > div { display: flex; flex-direction: column; gap: 3px; }
.ui-v2 .poster-create-actions > div > span { color: #8a8a90; font-size: 11px; }
.ui-v2 .poster-create-actions button { min-width: 126px; background: #18181b; color: white; font-weight: 700; }
.ui-v2 .poster-preview-panel { position: sticky; top: 16px; padding: 15px; border: 1px solid #e4e4e7; border-radius: 13px; background: #fbfbfd; }
.ui-v2 .poster-preview-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 13px; }
.ui-v2 .poster-preview-head strong { font-size: 14px; }
.ui-v2 .poster-preview-head span { font-size: 11px; color: #71717a; }
.ui-v2 .poster-preview-canvas { position: relative; width: 100%; aspect-ratio: 3 / 4; overflow: hidden; border-radius: 9px; background: #d8dde4; }
.ui-v2 .poster-preview-canvas.orientation-landscape { aspect-ratio: 4 / 3; }
.ui-v2 .poster-preview-bg { position: absolute; inset: 0; display: grid; place-items: center; color: rgba(255,255,255,.75); font-size: 11px; letter-spacing: .08em; }
.ui-v2 .poster-preview-copy { position: absolute; inset: auto 14px 16px; display: flex; flex-direction: column; gap: 5px; color: white; text-shadow: 0 1px 3px rgba(0,0,0,.35); }
.ui-v2 .poster-preview-copy strong { font-size: 20px; line-height: 1.2; }
.ui-v2 .poster-preview-copy span { font-size: 11px; }
.ui-v2 .poster-preview-copy ul { margin: 5px 0 0; padding-left: 17px; font-size: 10px; }
.ui-v2 .poster-preview-panel > p { margin: 10px 0 0; color: #8a8a90; font-size: 11px; line-height: 1.5; }
.ui-v2 .text-images-page { max-width: 1240px; }
.ui-v2 .text-image-form-card .field { margin-bottom: 16px; gap: 7px; }
.ui-v2 .text-image-form-card .field:last-child { margin-bottom: 0; }
.ui-v2 .text-image-form-card .field > label { font-weight: 650; color: #27272a; }
.ui-v2 .text-image-choice-row label {
  display: inline-flex; flex-direction: row; align-items: center; gap: 6px;
  width: max-content; min-width: max-content; min-height: 36px; padding: 7px 10px;
  border: 1px solid #e4e4e7; border-radius: 9px; background: #fbfbfd; white-space: nowrap;
}
.ui-v2 .text-image-choice-row input { width: 14px; height: 14px; margin: 0; }
.ui-v2 .text-image-ref-row { align-items: stretch; }
.ui-v2 .text-image-ref-row button { min-height: 38px; }
.ui-v2 .text-image-card { border-radius: 13px; }
.ui-v2 .text-image-card .poster-card-title-row { flex-wrap: wrap; }
.ui-v2 .story-command-panel {
  padding: 16px; margin-bottom: 14px; border: 1px solid #e4e4e7; border-radius: 13px; background: #fbfbfd;
}
.ui-v2 .story-command-summary { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.ui-v2 .story-command-summary > div { display: flex; align-items: baseline; gap: 10px; }
.ui-v2 .story-command-label { font-size: 16px; font-weight: 750; }
.ui-v2 .story-command-label,
.ui-v2 .story-table-scene-label,
.ui-v2 .character-box > h2,
.ui-v2 .export-box > h2 {
  color: #2563eb;
}
.ui-v2 .story-command-summary strong { font-size: 12px; color: #6b7280; font-weight: 500; }
.ui-v2 .story-status { font-size: 11px; font-weight: 700; }
.ui-v2 .story-command-actions { display: flex; gap: 8px; margin-top: 14px; }
.ui-v2 .story-command-actions > button:first-child { min-width: 130px; font-weight: 700; }
.ui-v2 .story-context-panel { margin-top: 14px; padding-top: 11px; border-top: 1px solid #e4e4e7; }
.ui-v2 .story-context-panel summary { color: #52525b; cursor: pointer; font-size: 11px; font-weight: 650; }
.ui-v2 .story-context-panel .premise-row { margin-top: 10px; }
.ui-v2 .story-context-panel .style-mode-picker-inline { margin-top: 10px; }
.ui-v2 .import-box { margin: 0 0 14px; padding: 16px; background: #fff; }
.ui-v2 .import-box-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.ui-v2 .import-box-head > div { display: flex; align-items: baseline; gap: 8px; }
.ui-v2 .import-box-head strong { font-size: 14px; }
.ui-v2 .import-box-head span { color: #8a8a90; font-size: 11px; }
.ui-v2 .import-help { margin: 10px 0; padding: 8px 10px; border: 1px solid #e4e4e7; border-radius: 8px; background: #fbfbfd; }
.ui-v2 .import-help summary { color: #52525b; cursor: pointer; font-size: 11px; font-weight: 650; }
.ui-v2 .import-help p { margin: 8px 0 0; line-height: 1.6; }
.ui-v2 .story-empty { display: flex; flex-direction: column; gap: 4px; align-items: center; justify-content: center; min-height: 150px; border: 1px dashed #d4d4d8; border-radius: 12px; color: #71717a; }
.ui-v2 .story-empty strong { color: #3f3f46; font-size: 14px; }
.ui-v2 .story-outline { display: flex; flex-direction: column; gap: 7px; }
.ui-v2 .story-outline-row { display: grid; grid-template-columns: 64px minmax(0, 1fr) auto auto auto; gap: 9px; align-items: center; padding: 9px 11px; border: 1px solid #e4e4e7; border-radius: 10px; background: white; }
.ui-v2 .story-outline-index { font-size: 12px; font-weight: 700; }
.ui-v2 .story-outline-row input { min-height: 34px; }
.ui-v2 .story-outline-count { color: #71717a; white-space: nowrap; }
.ui-v2 .settings-page {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px;
}
.ui-v2 .settings-page > .back, .ui-v2 .settings-page-head,
.ui-v2 .settings-group-primary, .ui-v2 .settings-actions,
.ui-v2 .settings-tabs { grid-column: 1 / -1; }
.ui-v2 .settings-tabs { margin-bottom: 2px; }
.ui-v2 .settings-page > .back { justify-self: start; margin-bottom: -4px; }
.ui-v2 .settings-page-head {
  padding-bottom: 14px; border-bottom: 1px solid #e4e4e7;
}
.ui-v2 .settings-page-head { align-items: center; }
.ui-v2 .settings-page-head h1 { font-size: 22px; line-height: 1.2; }
.ui-v2 .settings-page-head p { font-size: 12px; }
.ui-v2 .settings-state {
  display: inline-flex; align-items: center; min-height: 25px; padding: 3px 9px;
  border: 1px solid; border-radius: 999px; font-weight: 700;
}
.ui-v2 .settings-state.ready { color: #047857; border-color: #a7f3d0; background: #ecfdf5; }
.ui-v2 .settings-state.missing { color: #b45309; border-color: #fde68a; background: #fffbeb; }
.ui-v2 .settings-group {
  padding: 18px; border: 1px solid #e4e4e7; border-radius: 13px; background: #fbfbfd;
}
.ui-v2 .settings-group-primary { background: white; }
.ui-v2 .settings-group-update { grid-column: 1 / -1; background: white; }
.ui-v2 .settings-group-head { margin-bottom: 18px; padding-bottom: 12px; border-bottom: 1px solid #e4e4e7; }
.ui-v2 .settings-group-head > div {
  display: flex; align-items: baseline; gap: 8px; min-width: 0;
}
.ui-v2 .settings-group-head h2 { font-size: 16px; }
.ui-v2 .settings-group-head h2 { color: #7c3aed; }
.ui-v2 .settings-group-head p {
  min-width: 0; color: #8a8a90; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ui-v2 .settings-group-head > span {
  padding: 3px 7px; border-radius: 999px; background: #f0f0f2; color: #71717a; font-size: 10px; font-weight: 700;
}
.ui-v2 .settings-group-primary .settings-group-head > span { color: #b4232f; background: #fef2f2; }
.ui-v2 .settings-group .field { gap: 7px; margin-bottom: 16px; }
.ui-v2 .settings-group .field:last-child { margin-bottom: 0; }
.ui-v2 .settings-group .field > label { color: #27272a; font-size: 13px; font-weight: 650; }
.ui-v2 .field-badge {
  display: inline-flex; margin-left: 5px; padding: 2px 6px; border-radius: 999px;
  background: #f0f0f2; color: #71717a; font-weight: 600; vertical-align: middle;
}
.ui-v2 .field-help { line-height: 1.5; }
.ui-v2 .field-help code { color: #3f6212; font-family: ui-monospace, monospace; }
.ui-v2 .custom-template-rows { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.ui-v2 .custom-template-row {
  display: grid; grid-template-columns: 1.2fr 1.6fr 120px 120px auto; gap: 8px; align-items: center;
}
.ui-v2 .custom-template-row input, .ui-v2 .custom-template-row select {
  padding: 7px 9px; border: 1px solid #e4e4e7; border-radius: 8px; font-size: 13px;
}
@media (max-width: 720px) {
  .ui-v2 .custom-template-row { grid-template-columns: 1fr; }
}
.ui-v2 .settings-doc {
  padding: 7px 9px; border: 1px solid #e4e4e7; border-radius: 8px; background: #f7f7f9;
}
.ui-v2 .settings-doc summary { color: #52525b; font-size: 11px; font-weight: 600; }
.ui-v2 .settings-doc .model-ref-hint { margin: 9px 0 2px; color: #71717a; line-height: 1.65; }
.ui-v2 .settings-actions {
  align-items: center; justify-content: space-between; gap: 16px; padding: 14px 16px;
  border: 1px solid #d4d4d8; border-radius: 13px; background: white;
}
.ui-v2 .settings-actions > div { display: flex; flex-direction: column; gap: 2px; }
.ui-v2 .settings-actions > button { min-width: 132px; background: #18181b; color: white; font-weight: 700; }
.ui-v2 .update-card { align-items: flex-start; }
.ui-v2 .update-actions button { min-height: 34px; }
.ui-v2 .project-title-row h2 { margin: 0; font-size: 15px; }
.ui-v2 .premise-row { margin-top: 8px; align-items: center; }
.ui-v2 .premise {
  display: -webkit-box; margin: 0; overflow: hidden; -webkit-box-orient: vertical;
  -webkit-line-clamp: 2; line-height: 1.5; color: #52525b;
}
.ui-v2 .premise.expanded { display: block; overflow: visible; }
.ui-v2 .premise-toggle { min-height: 24px; padding: 2px 7px; font-size: 11px; }
.ui-v2 .style-mode-picker-inline {
  margin: 12px 0 0; padding-top: 11px; border-top: 1px solid #e4e4e7;
  overflow-x: auto; gap: 15px;
}
.ui-v2 .style-mode-picker-inline label {
  display: inline-flex; flex-direction: row; align-items: center; gap: 5px;
  width: max-content; min-width: max-content; white-space: nowrap; color: #3f3f46;
}
.ui-v2 .style-mode-picker-inline input { width: 14px; height: 14px; margin: 0; }
.ui-v2 .style-mode-note { flex: 1 1 auto; min-width: 260px; color: #8a8a90; }

.ui-v2 .tag { border: 1px solid #e4e4e7; border-radius: 999px; background: white; }
/* 标签配色：项目生命周期(draft/active/archived)、内容类型、出图风格、已导出，
   四种语义各给一个固定色，跟生成状态那套红/蓝/绿(status-*)区分开——那套是"进度"，
   这几个是"属性/里程碑"，用色相区分而不是用红黄绿这种容易被当成"警告/正常"的颜色。 */
.ui-v2 .tag-status-draft { color: #71717a; border-color: #e4e4e7; background: #f4f4f5; }
.ui-v2 .tag-status-active { color: #2563eb; border-color: #bfdbfe; background: #eff6ff; }
.ui-v2 .tag-status-archived { color: #a16207; border-color: #fde68a; background: #fffbeb; }
.ui-v2 .tag-content-type { color: #7c3aed; border-color: #ddd6fe; background: #f5f3ff; }
.ui-v2 .tag-style-mode { color: #0f766e; border-color: #99f6e4; background: #f0fdfa; }
.ui-v2 .tag-exported { color: #15803d; border-color: #bbf7d0; background: #f0fdf4; font-weight: 600; }
.ui-v2 .warning-box, .ui-v2 .file-open-toast,
.ui-v2 .import-box, .ui-v2 .manual-box, .ui-v2 .character-box {
  border-radius: 12px; background: #fff;
}
.ui-v2 .warning-box { border-left: 3px solid #f59e0b; }
.ui-v2 .story-table { gap: 8px; }
.ui-v2 .story-table-group, .ui-v2 .scene-row, .ui-v2 .shot-row {
  border-radius: 12px;
}
.ui-v2 .story-table-group-head, .ui-v2 .scene-row.expanded > .scene-row-head,
.ui-v2 .shot-row.expanded > .shot-row-head { background: #f7f7f9; }
.ui-v2 .tab-btn, .ui-v2 .manual-tab { border-radius: 9px; }
.ui-v2 .story-view-toggle {
  width: fit-content; padding: 3px; gap: 3px; border-radius: 11px; background: #f2f2f4;
}
.ui-v2 .story-view-toggle .tab-btn { border-color: transparent; background: transparent; }
.ui-v2 .story-view-toggle .tab-btn.active { color: #18181b; background: white; border-color: transparent; }
.ui-v2 .character-box { padding: 16px; }
.ui-v2 .character-grid { grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; }
.ui-v2 .character-cell { padding: 11px; gap: 8px; }
.ui-v2 .character-cell, .ui-v2 .reuse-item, .ui-v2 .manual-entry { border-radius: 12px; }
.ui-v2 .manual-box, .ui-v2 .import-box { padding: 16px; gap: 12px; }
.ui-v2 .manual-entry { padding: 10px 12px; }
.ui-v2 .character-cell img, .ui-v2 .scene-ref-preview > img,
.ui-v2 .scene-row-thumb, .ui-v2 .shot-row-thumb { border-radius: 9px; }
.ui-v2 .manual-page { gap: 14px; }
.ui-v2 .manual-page-head { padding-bottom: 14px; border-bottom: 1px solid #e4e4e7; }
.ui-v2 .manual-page-head h1 { font-size: 22px; line-height: 1.2; }
.ui-v2 .manual-result-count {
  display: inline-flex; align-items: center; min-height: 26px; padding: 3px 9px;
  border: 1px solid #e4e4e7; border-radius: 999px; background: #f7f7f9; color: #52525b; font-weight: 700;
}
.ui-v2 .manual-page-help {
  width: fit-content; padding: 7px 10px; border: 1px solid #e4e4e7; border-radius: 9px; background: #fbfbfd;
}
.ui-v2 .manual-page-help summary { cursor: pointer; color: #52525b; font-size: 12px; font-weight: 650; }
.ui-v2 .manual-page-help p { max-width: 820px; margin: 8px 0 1px; line-height: 1.65; }
.ui-v2 .manual-box { padding: 0; border: 0; background: transparent; }
.ui-v2 .manual-search-row {
  padding: 12px; border: 1px solid #e4e4e7; border-radius: 12px; background: #fbfbfd;
}
.ui-v2 .manual-search { max-width: none; min-height: 40px; font-size: 14px; }
.ui-v2 .manual-tabs {
  gap: 7px; padding: 4px 0 10px; border-bottom: 1px solid #e4e4e7; flex-wrap: wrap;
}
.ui-v2 .manual-tab { min-height: 36px; padding: 7px 13px; font-size: 13px; }
.ui-v2 .manual-tab.active { background: #18181b; color: white; }
.ui-v2 .manual-list {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px;
  max-height: none; overflow: visible;
}
.ui-v2 .manual-entry {
  align-items: flex-start; min-height: 104px; padding: 14px; border-radius: 11px; background: white;
}
.ui-v2 .manual-entry-text { gap: 5px; min-width: 0; }
.ui-v2 .manual-entry-text strong { font-size: 14px; line-height: 1.45; }
.ui-v2 .manual-entry-text code { max-width: 100%; overflow-wrap: anywhere; font-size: 12px; }
.ui-v2 .manual-entry-text .hint { line-height: 1.5; }
.ui-v2 .manual-copy { min-width: 60px; flex-shrink: 0; }
.ui-v2 .scene-accordion { gap: 6px; margin-top: 8px; }
.ui-v2 .scene-accordion {
  display: grid; grid-template-columns: repeat(12, minmax(82px, 1fr)); gap: 0;
  border: 1px solid #e4e4e7; border-radius: 12px; overflow: hidden; background: white;
}
.ui-v2 .scene-row { display: contents; }
.ui-v2 .scene-row-head {
  grid-row: 1; min-height: 56px; padding: 8px 10px; border: 0; border-right: 1px solid #e4e4e7;
  border-bottom: 1px solid #e4e4e7; background: #fbfbfd; border-radius: 0; flex-wrap: wrap;
}
.ui-v2 .scene-row-head:hover { background: #f2f2f4; }
.ui-v2 .scene-row-head .scene-row-thumb { width: 36px; height: 30px; }
.ui-v2 .scene-row-head .scene-row-summary { display: none; }
.ui-v2 .scene-row-head .scene-row-title { font-size: 14px; font-weight: 700; }
.ui-v2 .scene-row-head .tag { margin-left: auto; }
.ui-v2 .scene-row.expanded .scene-row-head { background: white; box-shadow: inset 0 -2px #18181b; }
.ui-v2 .scene-row-body {
  grid-row: 2; grid-column: 1 / -1; min-width: 0; padding: 16px; border: 0; background: white;
}
.ui-v2 .scene-row-add { grid-column: 1 / -1; grid-row: 3; margin: 8px; }
.ui-v2 .scene-row-head { min-height: 56px; padding: 8px 10px; }
.ui-v2 .scene-row-body { padding: 16px; background: white; }
/* 之前"场次公共参数"标题单独一行、场次描述输入框又在下面另起一个叫 modal-header 的
   区块——两层标题看起来是两码事，其实场次描述才是这场戏真正的"标题"。现在改成：
   场次描述输入框独占第一行，是视觉上的主标题；下面第二行放次要的公共参数提示文字 +
   所有导航/排序/新增操作，一行放不下就整体换行，不会挤压/裁切按钮。 */
.ui-v2 .scene-detail-header {
  padding-bottom: 10px; border-bottom: 1px solid #e4e4e7; margin-bottom: 12px;
}
.ui-v2 .scene-detail-title-row { min-height: 30px; }
.ui-v2 .scene-detail-meta-row .hint { font-size: 12px; white-space: normal; flex: 1 1 220px; min-width: 0; }
.ui-v2 .scene-structure-actions {
  display: flex; align-items: center; justify-content: flex-end; gap: 7px; flex-wrap: wrap; flex-shrink: 0;
}
.ui-v2 .scene-structure-actions button { min-height: 30px; }
/* 上一场/下一场(导航，不改变场次顺序) 和 前移/后移(改变场次顺序) 是两组不同性质的操作，
   之前混在一排容易看不出区别；现在各自成组，中间一条竖线分隔。 */
.scene-action-group { display: inline-flex; align-items: center; gap: 7px; }
.scene-actions-divider { width: 1px; height: 18px; background: #e4e4e7; flex-shrink: 0; }
.ui-v2 .modal-header { display: grid; grid-template-columns: minmax(200px, 1fr) auto; gap: 10px; margin-bottom: 12px; }
.ui-v2 .scene-summary-input { margin-top: 0; min-height: 30px; }
.ui-v2 .scene-ref {
  display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 14px;
  padding: 13px; margin-bottom: 12px; border: 1px solid #e4e4e7; border-radius: 11px; background: #fbfbfd;
}
.ui-v2 .scene-ref-media { gap: 8px; }
.ui-v2 .scene-ref-preview { width: 100px; height: 132px; }
.ui-v2 .scene-ref-preview > img, .ui-v2 .scene-ref-placeholder { border-radius: 9px; }
.ui-v2 .scene-ref-media-tag { font-size: 9px; }
.ui-v2 .scene-ref-placeholder { border-style: dashed; background: white; }
.ui-v2 .scene-ref-info { gap: 8px; min-width: 0; }
.ui-v2 .scene-ref-info p { margin: 0; }
.ui-v2 .scene-ref-title-row { min-height: 28px; }
.ui-v2 .scene-ref-label { color: #27272a; font-size: 14px; font-weight: 700; }
.ui-v2 .scene-ref-title-row .tag { margin-left: 0; }
.ui-v2 .scene-ref-info .model-tag { display: block; width: 100%; box-sizing: border-box; }
.ui-v2 .ref-path-row { gap: 8px; }
.ui-v2 .ref-path-row > input { min-height: 38px; }
.ui-v2 .ref-path-row > .ref-pick-preview { width: 38px; height: 38px; border-radius: 7px; object-fit: cover; }
.ui-v2 .scene-ref-actions { align-items: center; padding-top: 2px; }
.ui-v2 .scene-ref-actions > button:first-child { min-width: 148px; font-weight: 650; }
.ui-v2 .batch-toolbar { margin: 10px 0; padding: 9px 11px; }
.ui-v2 .shot-tabs {
  display: flex; align-items: stretch; gap: 0; margin: 16px 0; overflow-x: auto;
  border: 1px solid #e4e4e7; border-radius: 11px; background: #f7f7f9;
}
.ui-v2 .shot-tab-group { position: relative; display: flex; align-items: center; flex-shrink: 0; border-right: 1px solid #e4e4e7; padding-right: 6px; }
.ui-v2 .shot-tab {
  display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: 7px;
  min-width: 172px; min-height: 76px; padding: 12px 44px 12px 16px; border: 0; border-radius: 0;
  background: transparent; color: #52525b; font-size: 17px; font-weight: 650;
}
.ui-v2 .shot-tab.active { color: #18181b; background: white; box-shadow: inset 0 -2px #18181b; font-weight: 600; }
.ui-v2 .shot-tab-statuses { display: inline-flex; gap: 5px; }
.ui-v2 .shot-tab-statuses i {
  display: inline-flex; align-items: center; justify-content: center; width: 23px; height: 23px;
  border: 1px solid #e4e4e7; border-radius: 6px; font-size: 11px; font-style: normal; font-weight: 700;
}
.ui-v2 .shot-tab-add {
  position: absolute; top: 9px; right: 9px; z-index: 2; width: 28px; min-width: 28px; min-height: 28px;
  padding: 0; border-radius: 999px; background: white; color: #52525b; font-size: 17px;
}
.ui-v2 .shot-tab-add.shot-add-button { color: #047857; border-color: #a7f3d0; background: #ecfdf5; }
.ui-v2 .shot-tab-add.shot-add-button:hover:not(:disabled) { color: white; border-color: #059669; background: #059669; }
/* 转场连接点：默认是个不起眼的灰色 →；一旦这一镜设置了转场效果，就换成蓝色高亮的
   小徽标并显示转场名称，一眼能看出"这两镜之间有没有设置转场、设置的是什么"。 */
.ui-v2 .shot-tab-arrow {
  flex-shrink: 0; display: inline-flex; align-items: center; white-space: nowrap; max-width: 96px;
  overflow: hidden; text-overflow: ellipsis; margin: 0 8px 0 -4px; padding: 4px 10px; border-radius: 999px;
  border: 1px solid #e4e4e7; background: white; color: #a1a1aa; font-size: 12px; font-weight: 600;
}
.ui-v2 .shot-tab-arrow.has-transition { border-color: #93c5fd; background: #eff6ff; color: #1d4ed8; }
.ui-v2 .shot-list { display: block; }
.ui-v2 .shot-row { display: none; border: 0; border-radius: 0; overflow: visible; }
.ui-v2 .shot-row.expanded { display: block; }
.ui-v2 .shot-row > .shot-row-head { display: none; }
.ui-v2 .shot-row-body {
  display: grid; grid-template-columns: minmax(330px, 40%) minmax(0, 60%); gap: 24px; padding: 0;
}
.ui-v2 .shot-media-pane {
  display: flex; flex-direction: column; gap: 14px; align-self: start; padding: 18px;
  border: 1px solid #e4e4e7; border-radius: 12px; background: #f7f7f9;
}
.ui-v2 .shot-media-pane-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.ui-v2 .shot-media-pane-head strong { font-size: 15px; }
.ui-v2 .shot-media-pane-head strong { color: #0f766e; }
.ui-v2 .shot-media-card {
  padding: 13px; border: 1px solid #e4e4e7; border-radius: 10px; background: white;
}
.ui-v2 .shot-media-card-title { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; font-size: 14px; font-weight: 600; }
.ui-v2 .shot-media-card-title span:last-child { font-size: 11px; }
.ui-v2 .shot-media-preview {
  width: 100%; aspect-ratio: 9 / 12; display: flex; align-items: center; justify-content: center;
  overflow: hidden; border: 1px solid #e4e4e7; border-radius: 9px; background: #f4f4f5; color: #a1a1aa;
}
.ui-v2 .shot-media-preview img, .ui-v2 .shot-media-preview video { width: 100%; height: 100%; object-fit: cover; }
.ui-v2 .shot-media-card-audio audio { width: 100%; }
.ui-v2 .media-correspondence {
  display: flex; align-items: center; justify-content: center; gap: 8px; margin: -3px 0;
  color: #71717a; font-size: 12px; font-weight: 500;
}
.ui-v2 .media-correspondence span { color: #18181b; font-size: 16px; }
.ui-v2 .shot-settings-pane { min-width: 0; }
.ui-v2 .consistency-panel {
  padding: 15px; margin-bottom: 16px; border: 1px solid #bfdbfe; border-radius: 12px; background: #f8fbff;
}
.ui-v2 .consistency-panel-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.ui-v2 .consistency-panel-head > div { display: flex; align-items: baseline; gap: 9px; }
.ui-v2 .consistency-panel-head strong { font-size: 15px; }
.ui-v2 .consistency-panel-head strong { color: #1d4ed8; }
.ui-v2 .consistency-panel-head div span { font-size: 12px; color: #6b7280; }
.shot-move-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.ui-v2 .consistency-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.ui-v2 .consistency-item { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.ui-v2 .consistency-item label { font-size: 11px; font-weight: 700; color: #52525b; }
.ui-v2 .consistency-item > span { color: #6b7280; font-size: 12px; }
.ui-v2 .consistency-item-wide { grid-column: 1 / -1; }
.ui-v2 .consistency-duplicate { display: none; }
.ui-v2 .shot-row-head { min-height: 52px; padding: 8px 11px; }
.ui-v2 .shot-asset-statuses { gap: 4px; }
.ui-v2 .asset-status-mini { width: 20px; height: 20px; border-radius: 6px; }
.ui-v2 .shot-stage-heading {
  min-height: 44px; margin: 14px 0 8px; padding: 8px 11px; border: 0; border-left: 3px solid #18181b;
  border-radius: 8px; background: #f7f7f9;
}
.ui-v2 .shot-stage-heading > div { gap: 7px; }
.ui-v2 .shot-stage-heading strong { font-size: 12px; }
.ui-v2 .shot-stage-num { width: 22px; height: 22px; background: #18181b; }
.ui-v2 .shot-row-body > .shot-meta-row {
  margin-bottom: 14px; padding: 12px; gap: 14px; border: 1px solid #e4e4e7; border-radius: 10px; background: #fbfbfd;
}
.ui-v2 .shot-stage-content { margin-bottom: 14px; padding: 12px; border: 1px solid #e4e4e7; border-radius: 10px; background: #fbfbfd; }
.ui-v2 .pair-row.shot-stage-content { display: block; }
.ui-v2 .pair-row.shot-stage-content > .pair-media { display: none; }
.ui-v2 .pair-row.shot-stage-content .pair-text { width: 100%; }
.ui-v2 .pair-row.shot-stage-content .pair-media { width: 100%; }
.ui-v2 .pair-row.shot-stage-content .pair-media-audio { min-height: 76px; aspect-ratio: auto; }
.ui-v2 .shot-advanced { margin-top: 10px; padding-top: 8px; }
.ui-v2 .shot-advanced summary { display: flex; align-items: center; gap: 7px; }
.ui-v2 .batch-toolbar { border-radius: 11px; }
.ui-v2 .pair-row {
  padding: 10px; border: 1px solid #e4e4e7; border-radius: 12px; background: #fbfbfd;
}
.ui-v2 .pair-media { border-width: 1px; border-radius: 10px; }
.ui-v2 .candidate-item img, .ui-v2 .candidate-placeholder { border-radius: 9px; }
.ui-v2 .model-tag, .ui-v2 .model-ref-hint code { border-radius: 6px; }

@media (max-width: 640px) {
  .shell.ui-v2 { padding: 0; }
  .ui-v2 .sidebar { width: 100%; height: auto; border-radius: 0; border-width: 0 0 1px; }
  .ui-v2 .sidebar-nav { flex: 1; flex-direction: row; gap: 12px; overflow-x: auto; }
  .ui-v2 .sidebar-nav-group { min-width: max-content; }
  .ui-v2 .sidebar-nav-group-settings { padding-top: 0; padding-left: 12px; border-top: 0; border-left: 1px solid #e4e4e7; }
  .ui-v2 .main-content { height: auto; border: 0; border-radius: 0; }
  .ui-v2 .sidebar-footer { margin-left: auto; }
  .ui-v2 .scene-ref { grid-template-columns: auto minmax(0, 1fr); }
  .ui-v2 .scene-ref-preview { width: 64px; height: 88px; }
  .ui-v2 .scene-accordion { grid-template-columns: repeat(12, minmax(74px, 1fr)); overflow-x: auto; }
  .ui-v2 .shot-row-body { grid-template-columns: 1fr; }
  .ui-v2 .shot-media-pane { display: grid; grid-template-columns: 1fr 1fr; }
  .ui-v2 .shot-media-pane-head, .ui-v2 .shot-media-card-audio { grid-column: 1 / -1; }
  .ui-v2 .pair-row.shot-stage-content { grid-template-columns: 1fr; }
  .ui-v2 .pair-row.shot-stage-content .pair-media { max-width: 180px; }
  .ui-v2 .project-create-form { grid-template-columns: 1fr; }
  .ui-v2 .project-create-form > button { min-height: 34px; }
  .ui-v2 .projects-page-head { align-items: flex-start; }
  .ui-v2 .project-template-grid { grid-template-columns: 1fr 1fr; }
  .ui-v2 .project-custom-options { grid-template-columns: 1fr; }
  .ui-v2 .project-create-input-row { flex-direction: column; }
  .ui-v2 .story-command-summary > div { align-items: flex-start; flex-direction: column; gap: 2px; }
  .ui-v2 .story-command-actions { flex-wrap: wrap; }
  .ui-v2 .story-outline-row { grid-template-columns: 1fr auto; }
  .ui-v2 .story-outline-index { grid-column: 1 / -1; }
  .ui-v2 .scene-detail-meta-row { align-items: flex-start; flex-direction: column; }
  .ui-v2 .scene-structure-actions { justify-content: flex-start; }
  .ui-v2 .consistency-grid { grid-template-columns: 1fr; }
  .ui-v2 .consistency-item-wide { grid-column: auto; }
  .ui-v2 .settings-page { grid-template-columns: 1fr; }
  .ui-v2 .settings-page > .back, .ui-v2 .settings-page-head,
  .ui-v2 .settings-group-primary, .ui-v2 .settings-actions,
  .ui-v2 .settings-tabs { grid-column: auto; }
  .ui-v2 .story-gen-provider-tabs { grid-template-columns: 1fr; }
  .ui-v2 .settings-actions { align-items: stretch; flex-direction: column; }
  .ui-v2 .update-card { align-items: stretch; flex-direction: column; }
  .ui-v2 .update-actions { justify-content: flex-start; }
  .ui-v2 .manual-list { grid-template-columns: 1fr; }
  .ui-v2 .manual-search-row { align-items: stretch; flex-direction: column; }
  .ui-v2 .poster-create-layout { grid-template-columns: 1fr; }
  .ui-v2 .poster-preview-panel { position: static; }
  .ui-v2 .poster-preset-grid { grid-template-columns: 1fr 1fr; }
  .ui-v2 .poster-create-actions { align-items: stretch; flex-direction: column; }
  .ui-v2 .text-image-create-panel { grid-template-columns: 1fr; }
  .ui-v2 .text-image-preview-panel { position: static; }
  .ui-v2 .text-image-options-grid { grid-template-columns: 1fr; }
  .ui-v2 .text-image-ref-row { flex-direction: column; }
}
</style>
