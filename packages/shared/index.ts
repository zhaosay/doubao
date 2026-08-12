// 跨 Electron 渲染进程 / FastAPI 共用的约定值。
// Python 侧(apps/ai-service)没有直接 import 这个包，靠约定保持字符串一致，
// 改这里的值时记得同步检查 apps/ai-service 里写死的同名字符串。

export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed'

export type AssetKind = 'image' | 'video' | 'voice' | 'export'

export interface ShotAsset {
  id: string
  shotId: string
  type: AssetKind
  status: TaskStatus
  filePath: string | null
  providerId: string | null
  error: string | null
}

export interface Shot {
  id: string
  sceneId: string
  order: number
  sceneType: string | null
  drawPrompt: string
  motionPrompt: string | null
  dialogue: string | null
  durationSec: number
  characterName: string | null
}
