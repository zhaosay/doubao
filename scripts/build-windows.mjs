import { spawnSync } from 'node:child_process'

// A macOS Developer ID certificate is not a Windows Authenticode certificate.
// Clear shared macOS signing variables so local Windows builds cannot inherit it.
const env = {
  ...process.env,
  CSC_LINK: '',
  CSC_KEY_PASSWORD: '',
  WIN_CSC_LINK: '',
  WIN_CSC_KEY_PASSWORD: ''
}
const command = process.platform === 'win32' ? 'npx.cmd' : 'npx'

for (const args of [['electron-vite', 'build'], ['electron-builder', '--win', '--publish', 'never']]) {
  const result = spawnSync(command, args, { cwd: process.cwd(), env, stdio: 'inherit' })
  if (result.status !== 0) process.exit(result.status ?? 1)
}
