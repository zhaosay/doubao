import { contextBridge, ipcRenderer } from 'electron'
import type { IpcRendererEvent } from 'electron'

// 渲染进程直接 fetch 本地 FastAPI。端口不再写死 8000——主进程在 createWindow()
// 之前会先探测一个可用端口（8000 被别的程序占用就换一个），写进
// process.env.AI_SERVICE_PORT；这里同步读取，preload 早于渲染进程加载，读的时候
// 这个环境变量必然已经写好了。process.env.AI_SERVICE_PORT 缺失时(理论上不会发生，
// 兜底防止类型报错)才退回 8000。
// 文件系统访问只开放这一个"用系统默认程序打开本地文件"的窄接口，不做通用透传
// （渲染进程不该有更大的文件系统权限）。
contextBridge.exposeInMainWorld('aiManju', {
  apiBaseUrl: `http://127.0.0.1:${process.env.AI_SERVICE_PORT ?? '8000'}`,
  openPath: (filePath: string): Promise<string> => ipcRenderer.invoke('open-path', filePath),
  showItemInFolder: (filePath: string): Promise<string | null> =>
    ipcRenderer.invoke('show-item-in-folder', filePath),
  pickImageFile: (): Promise<{ path: string; dataUrl: string | null; error?: string | null } | null> =>
    ipcRenderer.invoke('pick-image-file'),
  readImagePreview: (filePath: string): Promise<string | null> =>
    ipcRenderer.invoke('read-image-preview', filePath),
  getUpdateStatus: () => ipcRenderer.invoke('app-update-status'),
  checkForUpdates: () => ipcRenderer.invoke('app-update-check'),
  downloadUpdate: () => ipcRenderer.invoke('app-update-download'),
  installUpdate: () => ipcRenderer.invoke('app-update-install'),
  openLatestRelease: () => ipcRenderer.invoke('app-update-open-release'),
  onUpdateStatus: (callback: (status: unknown) => void): (() => void) => {
    const listener = (_event: IpcRendererEvent, status: unknown) => callback(status)
    ipcRenderer.on('app-update-status', listener)
    return () => ipcRenderer.removeListener('app-update-status', listener)
  }
})
