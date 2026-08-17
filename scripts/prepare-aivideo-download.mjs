import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, unlinkSync } from 'node:fs'
import { join, resolve } from 'node:path'

const root = resolve(new URL('..', import.meta.url).pathname)
const sourceDir = join(root, 'local-release')
const outputDir = join(root, 'web-dist', 'aivideo')
const pageFile = join(root, 'web', 'aivideo', 'index.html')

const generatedFilePatterns = [
  /^AI-Manju-Setup-.+\.exe$/,
  /^AI-Manju-Setup-.+\.exe\.blockmap$/,
  /^AI-Manju-.+-x64\.zip$/,
  /^AI-Manju-.+-universal\.dmg$/,
  /^AI-Manju-.+-universal\.dmg\.blockmap$/,
  /^AI-Manju-.+-universal\.zip$/,
  /^AI-Manju-.+-universal\.zip\.blockmap$/,
  /^latest\.yml$/,
  /^latest-mac\.yml$/
]

function readYamlValue(file, key) {
  const yamlPath = join(sourceDir, file)
  if (!existsSync(yamlPath)) return ''
  const yaml = readFileSync(yamlPath, 'utf8')
  const match = yaml.match(new RegExp(`(?:^|\\n)${key}:\\s*['"]?([^'"\\n]+)`, 'm'))
  return match ? match[1].trim() : ''
}

function copyIfExists(name) {
  if (!name) return 0
  const from = join(sourceDir, name)
  if (!existsSync(from)) return 0
  copyFileSync(from, join(outputDir, name))
  console.log(`复制 ${name}`)
  return 1
}

if (!existsSync(sourceDir)) {
  throw new Error(`找不到产物目录：${sourceDir}`)
}

mkdirSync(outputDir, { recursive: true })
for (const name of readdirSync(outputDir)) {
  if (generatedFilePatterns.some((pattern) => pattern.test(name))) {
    unlinkSync(join(outputDir, name))
  }
}

copyFileSync(pageFile, join(outputDir, 'index.html'))
console.log('复制 index.html')

let copied = 1

copied += copyIfExists('latest.yml')
const winVersion = readYamlValue('latest.yml', 'version')
const winPath = readYamlValue('latest.yml', 'path') || (winVersion ? `AI-Manju-Setup-${winVersion}.exe` : '')
for (const name of [winPath, `${winPath}.blockmap`, winVersion ? `AI-Manju-${winVersion}-x64.zip` : '']) {
  copied += copyIfExists(name)
}

copied += copyIfExists('latest-mac.yml')
const macVersion = readYamlValue('latest-mac.yml', 'version')
const macZipPath = readYamlValue('latest-mac.yml', 'path') || (macVersion ? `AI-Manju-${macVersion}-universal.zip` : '')
for (const name of [
  macZipPath,
  `${macZipPath}.blockmap`,
  macVersion ? `AI-Manju-${macVersion}-universal.dmg` : '',
  macVersion ? `AI-Manju-${macVersion}-universal.dmg.blockmap` : ''
]) {
  copied += copyIfExists(name)
}

console.log(`已生成 ${outputDir}`)
console.log(`共复制 ${copied} 个文件；把这个目录里的内容上传到 https://api.yesgangnam.com/aivideo/`)
