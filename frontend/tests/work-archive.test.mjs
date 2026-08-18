import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const page = readFileSync(resolve(root, 'pages/work-archive.vue'), 'utf8')
const sidebar = readFileSync(resolve(root, 'components/SidebarRail.vue'), 'utf8')
const api = readFileSync(resolve(root, 'composables/useApi.js'), 'utf8')

test('work archive page exposes adoption, explicit enable, status and pending decisions', () => {
  assert.match(page, /接管预检/)
  assert.match(page, /条待追赶/)
  assert.match(page, /自动归档/)
  assert.match(page, /待补媒体/)
  assert.match(page, /待确认会话/)
  assert.match(page, /window\.confirm/)
  assert.match(page, /EventSource/)
})

test('work archive route is reachable and uses the API base once', () => {
  assert.match(sidebar, /goWorkArchive/)
  assert.match(sidebar, /\/work-archive/)
  assert.match(api, /listWorkArchiveProfiles/)
  assert.doesNotMatch(api, /\/api\/work-archive/)
})
