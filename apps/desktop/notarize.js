// electron-builder afterSign hook: notarize and staple the signed macOS .app.
// Local release scripts must provide NOTARY_PROFILE; CI/manual unsigned builds skip it.
const { execSync } = require('child_process')
const path = require('path')

exports.default = async function notarizing(context) {
  if (process.platform !== 'darwin') return
  if (process.env.SKIP_NOTARIZE === '1') {
    console.log('  • [notarize] skipped because SKIP_NOTARIZE=1')
    return
  }

  const keychainProfile = process.env.NOTARY_PROFILE
  if (!keychainProfile) {
    console.log('  • [notarize] skipped because NOTARY_PROFILE is not set')
    return
  }

  const { notarize } = require('@electron/notarize')
  const appName = context.packager.appInfo.productFilename
  const appPath = path.join(context.appOutDir, `${appName}.app`)

  console.log(`  • [notarize] submitting ${appPath} with profile=${keychainProfile}`)
  await notarize({ tool: 'notarytool', appPath, keychainProfile })

  console.log('  • [notarize] accepted, stapling ticket to .app')
  const maxAttempts = 5
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      execSync(`xcrun stapler staple "${appPath}"`, { stdio: 'inherit' })
      console.log('  • [notarize] done')
      return
    } catch (error) {
      if (attempt === maxAttempts) throw error
      const waitSec = attempt * 15
      console.log(`  • [notarize] staple failed (${attempt}/${maxAttempts}), retrying in ${waitSec}s`)
      execSync(`sleep ${waitSec}`)
    }
  }
}
