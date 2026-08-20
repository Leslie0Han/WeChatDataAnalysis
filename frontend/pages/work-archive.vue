<template>
  <main class="archive-page">
    <header class="archive-header">
      <div>
        <p class="eyebrow">REALTIME ARCHIVE</p>
        <h1>工作归档</h1>
        <p>微信与本软件保持运行时增量写入；退出期间的消息会在下次启动后追赶。</p>
      </div>
      <div class="header-actions">
        <button v-if="profiles.length && !showNewArchive" class="secondary" @click="openNewArchive">新建归档</button>
        <button class="secondary" :disabled="loading" @click="refreshAll">刷新状态</button>
      </div>
    </header>

    <section v-if="profiles.length && !showNewArchive" class="panel profile-toolbar">
      <label>
        <span>当前归档</span>
        <select :value="profile?.id || ''" @change="switchProfile">
          <option v-for="item in profiles" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
      </label>
      <small>每个归档使用独立目录、会话清单和同步开关。</small>
    </section>

    <section v-if="showNewArchive" class="panel setup-panel">
      <div class="panel-title">
        <div>
          <h2>新建归档</h2>
          <p>创建独立目录后，只扫描会话身份；确认纳入前不读取聊天内容。</p>
        </div>
        <span class="step-badge">空白归档</span>
      </div>

      <label>
        <span>归档名称</span>
        <input v-model="newArchive.name" placeholder="例如：装修归档" />
      </label>
      <label>
        <span>微信账号</span>
        <select v-model="newArchive.account">
          <option value="">请选择账号</option>
          <option v-for="account in accounts" :key="account" :value="account">{{ account }}</option>
        </select>
      </label>
      <label>
        <span>归档根目录</span>
        <div class="path-row">
          <input v-model="newArchive.archiveRoot" placeholder="选择一个与其他归档不同的目录" />
          <button class="secondary" :disabled="picking" @click="chooseNewDirectory">{{ picking ? '选择中…' : '选择目录' }}</button>
        </div>
      </label>
      <div class="actions">
        <button class="secondary" @click="cancelNewArchive">取消</button>
        <button class="primary" :disabled="!canCreateArchive || working" @click="createNewArchive">创建空白归档</button>
      </div>
    </section>

    <section v-else-if="!profile" class="panel setup-panel">
      <div class="panel-title">
        <div>
          <h2>接管现有归档</h2>
          <p>先只读核验目录、八字段 JSON、manifest、本地媒体与实时源消息数。</p>
        </div>
        <div class="pending-head-actions">
          <button class="secondary compact" @click="openNewArchive">新建空白归档</button>
          <span class="step-badge">首次设置</span>
        </div>
      </div>

      <label>
        <span>微信账号</span>
        <select v-model="setup.account">
          <option value="">请选择账号</option>
          <option v-for="account in accounts" :key="account" :value="account">{{ account }}</option>
        </select>
      </label>
      <label>
        <span>归档根目录</span>
        <div class="path-row">
          <input v-model="setup.archiveRoot" placeholder="选择包含 _archive_plan.json 的归档目录" />
          <button class="secondary" :disabled="picking" @click="chooseDirectory">{{ picking ? '选择中…' : '选择目录' }}</button>
        </div>
      </label>

      <div class="actions">
        <button class="secondary" :disabled="!canPreflight || working" @click="runPreflight">接管预检</button>
        <button class="primary" :disabled="!preflight?.ok || working" @click="adopt">确认接管</button>
      </div>

      <div v-if="preflight" class="result" :class="preflight.ok ? 'success' : 'error'">
        <strong>{{ preflight.ok ? '核验通过' : '暂不能接管' }}</strong>
        <span>
          {{ preflight.conversationCount }} 个会话，{{ preflight.messageCount }} 条已归档消息
          <template v-if="preflight.pendingMessageCount">，{{ preflight.pendingMessageCount }} 条待追赶</template>
        </span>
        <ul v-if="!preflight.ok">
          <li v-for="item in blockedDetails" :key="item.username">
            {{ item.displayName }}：{{ item.errors.join('；') }}
          </li>
        </ul>
      </div>
    </section>

    <template v-else>
      <section class="status-grid">
        <article class="metric-card">
          <span>运行状态</span>
          <strong>{{ stateLabel }}</strong>
          <small>最后检测 {{ formatTime(status.lastDetectedAt) }}</small>
        </article>
        <article class="metric-card">
          <span>已纳入会话</span>
          <strong>{{ status.included ?? profile.includedUsernames?.length ?? 0 }}</strong>
          <small>稳定 username 作为归档身份</small>
        </article>
        <article class="metric-card">
          <span>本次新增</span>
          <strong>{{ status.added || 0 }}</strong>
          <small>最后归档 {{ formatTime(status.lastArchiveAt || status.finishedAt) }}</small>
        </article>
        <article class="metric-card" :class="{ warning: status.pendingMedia }">
          <span>待补媒体</span>
          <strong>{{ status.pendingMedia || 0 }}</strong>
          <small>仅查找本地文件与本地密钥</small>
        </article>
      </section>

      <section class="panel control-panel">
        <div>
          <h2>{{ profile.name }}</h2>
          <p class="root-path" :title="profile.archiveRoot">{{ profile.archiveRoot }}</p>
        </div>
        <div class="control-actions">
          <label class="switch-label">
            <input type="checkbox" :checked="profile.enabled" :disabled="working" @change="toggleEnabled" />
            <span>自动归档</span>
          </label>
          <button v-if="status.state === 'running'" class="danger" @click="cancelSync">取消</button>
          <button v-else class="primary" :disabled="working" @click="syncNow">立即同步</button>
          <button class="secondary" :disabled="working" @click="verifyArchive">完整核验</button>
        </div>
      </section>

      <section class="panel pending-panel">
        <div class="panel-title">
          <div>
            <h2>待确认会话</h2>
            <p>这里只展示会话身份；确认纳入前不会读取内容或创建归档目录。</p>
          </div>
          <div class="pending-head-actions">
            <button
              v-if="pending.length && profile.adoptedAt"
              class="secondary compact"
              :disabled="working"
              @click="excludeCurrentPendingAsBaseline"
            >全部按接管前旧会话排除</button>
            <span class="count-badge">{{ pending.length }}</span>
          </div>
        </div>
        <div v-if="pending.length" class="pending-list">
          <article v-for="item in pending" :key="item.username" class="pending-item">
            <div>
              <strong>{{ item.displayName || item.username }}</strong>
              <small>{{ item.isGroup ? '群聊' : '单聊' }} · {{ item.username }}</small>
            </div>
            <div class="pending-actions">
              <button class="secondary" @click="decide(item.username, 'excluded')">排除</button>
              <button class="primary" @click="includeConversation(item)">纳入</button>
            </div>
          </article>
        </div>
        <div v-else class="empty-state">当前没有待确认的新会话。</div>
      </section>
    </template>

    <div v-if="message" class="toast" :class="messageType">{{ message }}</div>
  </main>
</template>

<script setup>
import { storeToRefs } from 'pinia'
import { useChatAccountsStore } from '~/stores/chatAccounts'

const api = useApi()
const apiBase = useApiBase()
const accountStore = useChatAccountsStore()
const { switchableAccounts, selectedAccount } = storeToRefs(accountStore)

const profiles = ref([])
const profile = ref(null)
const status = ref({})
const pending = ref([])
const preflight = ref(null)
const loading = ref(false)
const working = ref(false)
const picking = ref(false)
const message = ref('')
const messageType = ref('success')
const setup = reactive({ account: '', archiveRoot: '' })
const newArchive = reactive({ name: '', account: '', archiveRoot: '' })
const showNewArchive = ref(false)
let eventSource = null

const accounts = computed(() => Array.isArray(switchableAccounts.value) ? switchableAccounts.value : [])
const canPreflight = computed(() => setup.account.trim() && setup.archiveRoot.trim())
const canCreateArchive = computed(() => (
  newArchive.name.trim() && newArchive.account.trim() && newArchive.archiveRoot.trim()
))
const blockedDetails = computed(() => (preflight.value?.details || []).filter((item) => item.errors?.length))
const stateLabel = computed(() => ({
  disabled: '已关闭', idle: '等待变化', running: '正在归档', debouncing: '等待静默',
  waiting_realtime: '等待实时连接', error: '需要处理',
}[status.value.state] || status.value.state || '未知'))

const notify = (text, type = 'success') => {
  message.value = text
  messageType.value = type
  window.setTimeout(() => { if (message.value === text) message.value = '' }, 3500)
}

const formatTime = (value) => {
  const timestamp = Number(value || 0)
  if (!timestamp) return '—'
  return new Date(timestamp * 1000).toLocaleString('zh-CN')
}

const chooseDirectory = async () => {
  if (!process.client) return
  picking.value = true
  try {
    if (window.wechatDesktop?.chooseDirectory) {
      const result = await window.wechatDesktop.chooseDirectory({ title: '选择现有聊天记录归档目录' })
      if (!result?.canceled && result?.filePaths?.[0]) setup.archiveRoot = String(result.filePaths[0])
    } else {
      const result = await api.pickSystemDirectory({ title: '选择现有聊天记录归档目录', initial_dir: setup.archiveRoot })
      if (result?.path) setup.archiveRoot = String(result.path)
    }
    preflight.value = null
  } catch (error) {
    notify(error?.message || '选择目录失败', 'error')
  } finally {
    picking.value = false
  }
}

const chooseNewDirectory = async () => {
  if (!process.client) return
  picking.value = true
  try {
    if (window.wechatDesktop?.chooseDirectory) {
      const result = await window.wechatDesktop.chooseDirectory({ title: '选择新归档目录' })
      if (!result?.canceled && result?.filePaths?.[0]) newArchive.archiveRoot = String(result.filePaths[0])
    } else {
      const result = await api.pickSystemDirectory({ title: '选择新归档目录', initial_dir: newArchive.archiveRoot })
      if (result?.path) newArchive.archiveRoot = String(result.path)
    }
  } catch (error) {
    notify(error?.message || '选择目录失败', 'error')
  } finally {
    picking.value = false
  }
}

const loadProfiles = async (preferredId = '') => {
  const result = await api.listWorkArchiveProfiles()
  profiles.value = Array.isArray(result?.profiles) ? result.profiles : []
  const wanted = preferredId || profile.value?.id || ''
  profile.value = profiles.value.find((item) => item.id === wanted) || profiles.value[0] || null
  if (profile.value) setup.account = profile.value.account
}

const switchProfile = async (event) => {
  const selected = profiles.value.find((item) => item.id === event.target.value)
  if (!selected || selected.id === profile.value?.id) return
  eventSource?.close()
  profile.value = selected
  status.value = {}
  pending.value = []
  await loadRuntime()
  connectEvents()
}

const openNewArchive = () => {
  newArchive.name = ''
  newArchive.account = profile.value?.account || setup.account || selectedAccount.value || accounts.value[0] || ''
  newArchive.archiveRoot = ''
  showNewArchive.value = true
}

const cancelNewArchive = () => {
  showNewArchive.value = false
}

const createNewArchive = async () => {
  working.value = true
  try {
    const id = `archive-${Date.now().toString(36)}`
    const result = await api.createWorkArchiveProfile({
      id,
      name: newArchive.name.trim(),
      account: newArchive.account.trim(),
      archiveRoot: newArchive.archiveRoot.trim(),
      enabled: false,
      includedUsernames: [],
      pendingUsernames: [],
      excludedUsernames: [],
      conversations: {},
      pendingMeta: {},
      mediaPolicy: 'local_only',
    })
    showNewArchive.value = false
    await loadProfiles(result.profile.id)
    await api.runWorkArchiveSync(result.profile.id)
    status.value = { state: 'running' }
    connectEvents()
    notify('归档已创建，正在扫描可供选择的会话身份。自动归档仍为关闭。')
  } catch (error) {
    notify(error?.message || '创建归档失败', 'error')
  } finally {
    working.value = false
  }
}

const loadRuntime = async () => {
  if (!profile.value) return
  const [statusResult, pendingResult] = await Promise.all([
    api.getWorkArchiveStatus(profile.value.id),
    api.getWorkArchivePending(profile.value.id),
  ])
  status.value = statusResult || {}
  pending.value = pendingResult?.items || []
}

const refreshAll = async () => {
  loading.value = true
  try {
    await accountStore.ensureLoaded()
    if (!setup.account) setup.account = selectedAccount.value || accounts.value[0] || ''
    await loadProfiles()
    await loadRuntime()
    connectEvents()
  } catch (error) {
    notify(error?.message || '读取归档状态失败', 'error')
  } finally {
    loading.value = false
  }
}

const runPreflight = async () => {
  working.value = true
  preflight.value = null
  try {
    preflight.value = await api.preflightWorkArchive({ account: setup.account, archiveRoot: setup.archiveRoot })
  } catch (error) {
    notify(error?.message || '接管预检失败', 'error')
  } finally {
    working.value = false
  }
}

const adopt = async () => {
  working.value = true
  try {
    const result = await api.adoptWorkArchive({
      id: 'work-chats', name: '工作聊天', account: setup.account, archiveRoot: setup.archiveRoot,
    })
    profile.value = result.profile
    await loadProfiles(result.profile.id)
    await loadRuntime()
    connectEvents()
    notify('接管完成。自动归档仍为关闭状态，请明确开启。')
  } catch (error) {
    notify(error?.message || '接管失败', 'error')
  } finally {
    working.value = false
  }
}

const toggleEnabled = async (event) => {
  const enabled = !!event.target.checked
  if (enabled && !window.confirm('开启后将按实时数据自动更新已纳入会话。确认开启吗？')) {
    event.target.checked = false
    return
  }
  working.value = true
  try {
    const result = await api.updateWorkArchiveProfile(profile.value.id, { enabled })
    profile.value = result.profile
    await loadRuntime()
  } catch (error) {
    event.target.checked = !enabled
    notify(error?.message || '更新自动归档开关失败', 'error')
  } finally {
    working.value = false
  }
}

const syncNow = async () => {
  working.value = true
  try {
    await api.runWorkArchiveSync(profile.value.id)
    status.value = { ...status.value, state: 'running' }
    notify('已开始增量同步')
  } catch (error) {
    notify(error?.message || '启动同步失败', 'error')
  } finally {
    working.value = false
  }
}

const cancelSync = async () => {
  await api.cancelWorkArchiveSync(profile.value.id)
  notify('已请求取消，将在当前安全写入点停止')
}

const verifyArchive = async () => {
  working.value = true
  try {
    const result = await api.verifyWorkArchive(profile.value.id)
    notify(result.status === 'success' ? `核验通过，共 ${result.messages} 条消息` : `发现 ${result.issues?.length || 0} 项问题`, result.status === 'success' ? 'success' : 'error')
  } catch (error) {
    notify(error?.message || '核验失败', 'error')
  } finally {
    working.value = false
  }
}

const decide = async (username, decision) => {
  await api.setWorkArchiveConversationStatus(profile.value.id, username, decision)
  await loadProfiles(profile.value.id)
  await loadRuntime()
}

const excludeCurrentPendingAsBaseline = async () => {
  if (!profile.value || !pending.value.length) return
  const confirmed = window.confirm(
    `将当前 ${pending.value.length} 个待确认会话标记为接管前旧会话并排除？这不会读取或删除聊天内容。`
  )
  if (!confirmed) return
  working.value = true
  try {
    const existing = Array.isArray(profile.value.excludedUsernames) ? profile.value.excludedUsernames : []
    const pendingNames = pending.value.map((item) => item.username).filter(Boolean)
    const result = await api.updateWorkArchiveProfile(profile.value.id, {
      excludedUsernames: [...new Set([...existing, ...pendingNames])],
      pendingUsernames: [],
      pendingMeta: {},
    })
    profile.value = result.profile
    pending.value = []
    notify('已将当前待确认列表记为接管前旧会话。')
  } catch (error) {
    notify(error?.message || '批量排除旧会话失败', 'error')
  } finally {
    working.value = false
  }
}

const includeConversation = async (item) => {
  if (!window.confirm(`确认把“${item.displayName || item.username}”纳入“${profile.value?.name || '当前归档'}”吗？`)) return
  await decide(item.username, 'included')
}

const connectEvents = () => {
  if (!process.client || !profile.value) return
  eventSource?.close()
  const base = String(apiBase || '').replace(/\/$/, '')
  eventSource = new EventSource(`${base}/work-archive/profiles/${encodeURIComponent(profile.value.id)}/events`)
  const refresh = () => {
    const profileId = profile.value?.id || ''
    void loadProfiles(profileId).then(loadRuntime)
  }
  for (const event of ['sync_started', 'sync_progress', 'sync_finished', 'sync_error', 'sync_cancelled', 'pending_discovered']) {
    eventSource.addEventListener(event, refresh)
  }
}

onMounted(refreshAll)
onBeforeUnmount(() => eventSource?.close())
</script>

<style scoped>
.archive-page { min-height: 100%; padding: 36px clamp(22px, 4vw, 58px) 64px; background: var(--app-bg, #f5f7f6); color: var(--app-text, #1f2924); }
.archive-header, .panel-title, .control-panel, .pending-item { display: flex; align-items: center; justify-content: space-between; gap: 24px; }
.header-actions { display: flex; align-items: center; gap: 10px; }
.archive-header { margin: 0 auto 28px; max-width: 1120px; align-items: flex-end; }
.archive-header h1 { margin: 4px 0 8px; font-size: clamp(28px, 4vw, 42px); letter-spacing: -.04em; }
.archive-header p, .panel p { margin: 0; color: #718078; }
.eyebrow { color: #07a653 !important; font-size: 11px; font-weight: 800; letter-spacing: .18em; }
.panel, .metric-card { border: 1px solid #dfe6e2; background: rgba(255,255,255,.92); box-shadow: 0 12px 34px rgba(31,55,43,.06); }
.panel { max-width: 1120px; margin: 0 auto 18px; padding: 24px; border-radius: 18px; }
.panel h2 { margin: 0 0 5px; font-size: 18px; }
.setup-panel { display: grid; gap: 20px; }
.setup-panel label { display: grid; gap: 8px; font-size: 12px; font-weight: 700; }
.profile-toolbar { display: flex; align-items: end; justify-content: space-between; gap: 20px; }
.profile-toolbar label { display: grid; gap: 7px; min-width: min(360px, 100%); font-size: 12px; font-weight: 700; }
.profile-toolbar small { color: #8c9992; }
input, select { width: 100%; min-width: 0; border: 1px solid #d9e1dd; border-radius: 10px; background: #fff; padding: 11px 12px; outline: none; }
input:focus, select:focus { border-color: #07b75b; box-shadow: 0 0 0 3px rgba(7,183,91,.1); }
.path-row, .actions, .control-actions, .pending-actions { display: flex; gap: 10px; align-items: center; }
button { border: 0; border-radius: 9px; padding: 10px 15px; font-size: 12px; font-weight: 700; cursor: pointer; white-space: nowrap; }
button:disabled { cursor: not-allowed; opacity: .48; }
.primary { background: #07b75b; color: white; }
.secondary { background: #edf2ef; color: #33453b; }
.danger { background: #fff0f0; color: #b83232; }
.step-badge, .count-badge { border-radius: 999px; background: #e9fff3; color: #078c47; padding: 6px 10px; font-size: 11px; font-weight: 800; }
.result { display: grid; gap: 5px; border-radius: 12px; padding: 14px; font-size: 12px; }
.result.success { background: #effbf4; color: #17643c; }
.result.error { background: #fff2f2; color: #8d3030; }
.result ul { margin: 6px 0 0; padding-left: 18px; max-height: 220px; overflow: auto; }
.status-grid { max-width: 1120px; margin: 0 auto 18px; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.metric-card { display: grid; gap: 7px; min-height: 132px; padding: 20px; border-radius: 16px; }
.metric-card span { color: #75827b; font-size: 12px; }
.metric-card strong { font-size: 25px; }
.metric-card small, .pending-item small { color: #8c9992; }
.metric-card.warning { border-color: #e5c878; background: #fffbef; }
.root-path { max-width: 620px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, monospace; font-size: 11px; }
.switch-label { display: flex; gap: 8px; align-items: center; font-size: 12px; font-weight: 700; }
.switch-label input { width: 18px; height: 18px; accent-color: #07b75b; }
.pending-list { display: grid; gap: 10px; margin-top: 18px; }
.pending-head-actions { display: flex; align-items: center; gap: 10px; }
.compact { padding: 7px 10px; }
.pending-item { border: 1px solid #e7ece9; border-radius: 12px; padding: 13px 14px; }
.pending-item > div:first-child { display: grid; gap: 4px; min-width: 0; }
.pending-item small { overflow: hidden; text-overflow: ellipsis; }
.empty-state { margin-top: 18px; border: 1px dashed #d7e0db; border-radius: 12px; padding: 28px; text-align: center; color: #8b9891; }
.toast { position: fixed; right: 24px; bottom: 24px; z-index: 30; max-width: min(420px, calc(100vw - 48px)); border-radius: 10px; padding: 12px 16px; background: #163c29; color: #fff; box-shadow: 0 10px 35px rgba(0,0,0,.18); }
.toast.error { background: #8f2f2f; }
@media (max-width: 860px) { .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .control-panel, .archive-header, .profile-toolbar { align-items: flex-start; flex-direction: column; } }
@media (max-width: 560px) { .archive-page { padding: 22px 14px 50px; } .status-grid { grid-template-columns: 1fr; } .path-row, .actions, .control-actions, .pending-item { align-items: stretch; flex-direction: column; } .pending-actions button { flex: 1; } }
</style>
