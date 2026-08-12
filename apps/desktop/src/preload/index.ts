import { contextBridge, ipcRenderer } from 'electron'
import type { IpcRendererEvent } from 'electron'

// 渲染进程直接 fetch 本地 FastAPI(http://127.0.0.1:8000)。
// 文件系统访问只开放这一个"用系统默认程序打开本地文件"的窄接口，不做通用透传
// （渲染进程不该有更大的文件系统权限）。
contextBridge.exposeInMainWorld('aiManju', {
  apiBaseUrl: 'http://127.0.0.1:8000',
  openPath: (filePath: string): Promise<string> => ipcRenderer.invoke('open-path', filePath),
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
