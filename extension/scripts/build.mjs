import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = resolve(root, 'dist')

await rm(output, { recursive: true, force: true })
await mkdir(output, { recursive: true })
await cp(resolve(root, 'src'), output, { recursive: true })
await cp(resolve(root, 'manifest.json'), resolve(output, 'manifest.json'))

const manifest = JSON.parse(await readFile(resolve(output, 'manifest.json'), 'utf8'))
if (manifest.manifest_version !== 3) throw new Error('The extension must build as Manifest V3.')
await writeFile(resolve(output, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`)

console.log(`Built unpacked extension: ${output}`)
