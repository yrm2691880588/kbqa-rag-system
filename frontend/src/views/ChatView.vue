<template>
  <div class="chat-layout">
    <!-- ════════ 左：会话列表 ════════ -->
    <aside class="side">
      <div class="side-head">
        <el-button type="primary" class="new-btn" @click="newSession">
          <el-icon style="margin-right: 4px"><Plus /></el-icon>新对话
        </el-button>
      </div>
      <!-- 新会话的检索范围选择 -->
      <div class="scope-bar">
        <span class="scope-label">范围</span>
        <el-select v-model="scopeKbId" size="small" placeholder="全部知识库">
          <el-option label="全部知识库" :value="0" />
          <el-option v-for="kb in activeKbs" :key="kb.id" :label="kb.name" :value="kb.id" />
        </el-select>
      </div>
      <div v-if="loadingSessions" v-loading="true" class="side-loading" />
      <div v-else-if="!sessions.length" class="side-empty">
        <el-empty description="还没有会话" :image-size="60" />
      </div>
      <div v-else class="session-list">
        <div
          v-for="s in sessions"
          :key="s.id"
          class="session-item"
          :class="{ active: currentSession?.id === s.id }"
          @click="switchSession(s)"
        >
          <div class="s-title">
            <el-icon v-if="s.id === currentSession?.id" class="s-icon"><ChatDotRound /></el-icon>
            <span class="s-name">{{ s.title }}</span>
          </div>
          <div class="s-meta">
            {{ formatChatTime(s.updated_at) }} · {{ s.message_count }} 条消息
          </div>
          <div class="s-del" @click.stop="confirmDelete(s)">
            <el-icon><Delete /></el-icon>
          </div>
        </div>
      </div>
    </aside>

    <!-- ════════ 右：对话主区 ════════ -->
    <main class="main">
      <div v-if="!currentSession" class="welcome">
        <h2>商品知识库智能问答</h2>
        <p>点击左侧「新对话」开始提问。AI 将依据知识库资料回答，并标注引用来源。</p>
        <div class="tips">
          <div class="tip-card" v-for="(t, i) in demoQuestions" :key="i" @click="quickAsk(t)">
            {{ t }}
          </div>
        </div>
      </div>

      <template v-else>
        <div class="chat-head">
          <div class="chat-title">
            {{ scopeName }}
            <el-tooltip content="点击重命名" placement="bottom">
              <el-button link size="small" class="rename-btn" @click="startRename">
                <el-icon><EditPen /></el-icon>
              </el-button>
            </el-tooltip>
          </div>
        </div>

        <!-- 消息区 -->
        <div ref="msgListRef" class="msg-list">
          <div
            v-for="(m, i) in displayMessages"
            :key="m.key"
            class="msg-row"
            :class="m.role"
          >
            <div class="avatar">{{ m.role === 'user' ? '我' : 'AI' }}</div>
            <div class="bubble">
              <!-- 思考过程（折叠展示） -->
              <div v-if="m.thinking && m.thinking.length" class="think-box" @click="toggleThink(m.key)">
                <span class="think-hint">{{ m.showThink ? '▾ 已展开思考过程' : '▸ 查看思考过程' }}</span>
                <div v-if="m.showThink" class="think-text">{{ m.thinking }}</div>
              </div>

              <!-- 正式回答 / 用户消息 -->
              <div v-if="m.role === 'user'" class="user-text">{{ m.content }}</div>
              <div v-else class="answer-wrap">
                <!-- 流式打字光标 -->
                <div v-if="m.typing && !m.content && !m.noHit" class="thinking-anim">
                  <span class="dot"></span><span class="dot"></span><span class="dot"></span>
                  <span class="thinking-label">{{ m.thinkingLabel }}</span>
                </div>
                <!-- 内容安全提示：这条回答被系统处理过，说清楚"为什么"比闷着不解释好 -->
                <div v-if="m.blocked" class="blocked-tip">
                  <el-icon><WarningFilled /></el-icon>
                  该回答已被内容安全策略处理
                </div>
                <!-- 用 v-html 渲染带 [n] 角标的回答 -->
                <div class="answer" @click="onAnswerClick" v-html="renderAnswer(m.content)" />
                <!-- 引用条目（回答下方列出来源） -->
                <div v-if="m.citations && m.citations.length" class="ref-list">
                  <div
                    v-for="c in m.citations"
                    :key="c.index"
                    class="ref-item"
                    @click="openCitation(c, m)"
                  >
                    <span class="ref-badge">{{ c.index }}</span>
                    <span class="ref-doc">{{ c.doc_name }}</span>
                    <span class="ref-score">{{ (c.score * 100).toFixed(0) }}%</span>
                  </div>
                </div>
                <div v-if="m.noHit" class="no-hit-tip">{{ m.noHitText }}</div>
              </div>
            </div>
            <!-- 反馈（仅已完成回答） -->
            <div v-if="m.role === 'assistant' && !m.typing && m.content" class="feedback">
              <el-tooltip content="回答有帮助" placement="top">
                <el-icon class="fb" :class="{ liked: m.feedback === 1 }" @click="setFeedback(m, 1)"><Top /></el-icon>
              </el-tooltip>
              <el-tooltip content="回答不准确" placement="top">
                <el-icon class="fb" :class="{ disliked: m.feedback === -1 }" @click="setFeedback(m, -1)"><Bottom /></el-icon>
              </el-tooltip>
            </div>
          </div>

          <!-- 底部滚动锚点 -->
          <div ref="bottomAnchor" />
        </div>

        <!-- 输入区 -->
        <div class="input-bar">
          <div class="input-wrap">
            <el-input
              v-model="inputText"
              type="textarea"
              :rows="2"
              resize="none"
              :placeholder="placeholder"
              :disabled="sending"
              @keydown="onKeydown"
            />
            <div class="input-actions">
              <span class="input-tip">Enter 发送 · Shift+Enter 换行</span>
              <el-button v-if="sending" type="danger" @click="stopGenerate">
                <el-icon style="margin-right: 4px"><VideoPause /></el-icon>停止
              </el-button>
              <el-button v-else type="primary" :disabled="!inputText.trim() || loadingHistory" @click="send">
                <el-icon style="margin-right: 4px"><Promotion /></el-icon>发送
              </el-button>
            </div>
          </div>
        </div>
      </template>
    </main>

    <!-- 引用原文抽屉 -->
    <el-drawer v-model="drawer.visible" :title="drawer.title" size="420px">
      <div v-if="drawer.citation" class="cite-detail">
        <div class="cite-meta">
          <span class="cite-score">相关度 {{ (drawer.citation.score * 100).toFixed(0) }}%</span>
          <span v-if="drawer.citation.page" class="cite-page">第 {{ drawer.citation.page }} 页</span>
        </div>
        <div class="cite-text">{{ drawer.citation.text }}</div>
      </div>
    </el-drawer>

    <!-- 删除确认 -->
    <el-dialog v-model="delDialog.visible" title="删除会话" width="360px">
      <p>删除后该会话的所有对话记录将无法恢复，确定删除吗？</p>
      <template #footer>
        <el-button @click="delDialog.visible = false">取消</el-button>
        <el-button type="danger" @click="doDelete">删除</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
// 问答主页（M3）：会话列表 + 流式对话 + 引用展示 + 反馈
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ChatDotRound, Delete, EditPen, Plus, Promotion, Top, Bottom, VideoPause, WarningFilled,
} from '@element-plus/icons-vue'
import { listKBs, type KnowledgeBase } from '@/api/kb'
import {
  createSession, deleteSession, listMessages, listSessions, renameSession,
  setMessageFeedback,
  streamChat, type ChatMessage, type ChatSession, type Citation,
} from '@/api/chat'
import { useAuthStore } from '@/stores/auth'
import { formatChatTime } from '@/utils/datetime'
import { renderAnswer } from '@/utils/answerRender'

const auth = useAuthStore()

// ---------------- 会话状态 ----------------
const sessions = ref<ChatSession[]>([])
const currentSession = ref<ChatSession | null>(null)
const loadingSessions = ref(true)
const loadingHistory = ref(false)
const activeKbs = ref<KnowledgeBase[]>([])
const scopeKbId = ref(0) // 0=全部库；>0=指定库（仅新会话生效）

// ---------------- 消息展示 ----------------
interface MsgItem {
  key: string
  role: 'user' | 'assistant'
  content: string
  thinking: string
  showThink: boolean
  citations: Citation[] | null
  feedback: number | null
  typing: boolean
  noHit: boolean
  noHitText: string
  thinkingLabel: string
  blocked: boolean // 内容安全命中：这条回答被系统处理过（界面上给个提示）
  msgId?: number // 已入库消息的真实 id（历史加载时）
}

const displayMessages = ref<MsgItem[]>([])
const inputText = ref('')
const sending = ref(false)
let abortCtrl: AbortController | null = null
let keySeq = 0

const msgListRef = ref<HTMLElement>()
const bottomAnchor = ref<HTMLElement>()

const demoQuestions = [
  '星跃S10手机的电池容量和快充是多少？',
  '星翼蓝牙耳机支持防水吗？',
  '星耀空气净化器A8适用多大面积？',
  '星翼智能手表能带着游泳吗？',
]

const placeholder = computed(() => (sending.value ? 'AI 正在回答中…' : `向 ${scopeName.value} 提问…`))
const scopeName = computed(() => {
  if (!currentSession.value) return '商品知识库'
  const scope = currentSession.value.kb_scope
  if (!scope || !scope.length) return '全部知识库'
  const kb = activeKbs.value.find((k) => k.id === scope[0])
  return kb ? kb.name : '指定知识库'
})

// ---------------- 生命周期 ----------------
onMounted(async () => {
  try {
    activeKbs.value = (await listKBs(false)).data
  } catch { /* 忽略 */ }
  await loadSessions()
  // 若只有 0 个会话不自动建；有历史会话则自动打开最近一个
  if (sessions.value.length) switchSession(sessions.value[0])
})

onBeforeUnmount(() => {
  abortCtrl?.abort()
})

// ---------------- 会话操作 ----------------
async function loadSessions() {
  loadingSessions.value = true
  try {
    sessions.value = (await listSessions()).data
  } catch { /* 拦截器已提示 */ } finally {
    loadingSessions.value = false
  }
}

async function newSession() {
  const kbIds = scopeKbId.value > 0 ? [scopeKbId.value] : null
  try {
    const s = (await createSession(kbIds)).data
    sessions.value.unshift(s)
    await switchSession(s, true)
  } catch { /* 拦截器已提示 */ }
}

async function switchSession(s: ChatSession, force = false) {
  if (currentSession.value?.id === s.id && !force) return
  abortCtrl?.abort()
  sending.value = false
  currentSession.value = s
  displayMessages.value = []
  loadingHistory.value = true
  try {
    const msgs = (await listMessages(s.id)).data
    displayMessages.value = msgs.map((m) => toMsgItem(m))
    await scrollBottom(false)
  } catch { /* 拦截器已提示 */ } finally {
    loadingHistory.value = false
  }
}

function toMsgItem(m: ChatMessage): MsgItem {
  return {
    key: `h-${m.id}-${keySeq++}`,
    role: m.role,
    content: m.content,
    thinking: '',
    showThink: false,
    citations: m.citations ?? null,
    feedback: m.feedback,
    typing: false,
    noHit: false,
    noHitText: '',
    thinkingLabel: '',
    blocked: false,
    msgId: m.id,
  }
}

function startRename() {
  ElMessageBox.prompt('输入新的会话标题', '重命名会话', {
    inputValue: currentSession.value?.title,
    inputValidator: (v: string) => (v?.trim() ? true : '标题不能为空'),
  })
    .then(async ({ value }) => {
      if (!currentSession.value) return
      await renameSession(currentSession.value.id, value.trim())
      currentSession.value.title = value.trim()
      await loadSessions() // 保持列表顺序一致
    })
    .catch(() => {})
}

const delDialog = ref({ visible: false, target: null as ChatSession | null })
function confirmDelete(s: ChatSession) {
  delDialog.value = { visible: true, target: s }
}
async function doDelete() {
  const t = delDialog.value.target
  if (!t) return
  delDialog.value.visible = false
  try {
    await deleteSession(t.id)
    sessions.value = sessions.value.filter((x) => x.id !== t.id)
    if (currentSession.value?.id === t.id) {
      currentSession.value = null
      displayMessages.value = []
      if (sessions.value.length) switchSession(sessions.value[0])
    }
  } catch { /* 拦截器已提示 */ }
}

// ---------------- 发送 / 流式 ----------------
async function send() {
  const q = inputText.value.trim()
  if (!q || sending.value || !currentSession.value) return
  inputText.value = ''

  const sid = currentSession.value.id
  // 本地先展示用户消息与 AI 占位（思考中动画）
  const uKey = `u-${keySeq++}`
  const aKey = `a-${keySeq++}`
  displayMessages.value.push({
    key: uKey, role: 'user', content: q, thinking: '', showThink: false,
    citations: null, feedback: null, typing: false, noHit: false, noHitText: '', thinkingLabel: '',
    blocked: false,
  })
  const aiItem: MsgItem = {
    key: aKey, role: 'assistant', content: '', thinking: '', showThink: false,
    citations: null, feedback: null, typing: true, noHit: false, noHitText: '',
    thinkingLabel: '正在思考',
    blocked: false,
  }
  displayMessages.value.push(aiItem)
  await scrollBottom(true)
  sending.value = true

  let hasStreamedAnswer = false
  let errorMsg = ''
  /** 唯一关掉全局"正在回答中"状态的地方，确保每条路径都走到 */
  const finishSending = () => {
    sending.value = false
    abortCtrl = null
  }
  abortCtrl = streamChat(
    sid,
    q,
    (ev) => {
      const item = displayMessages.value.find((m) => m.key === aKey)
      if (!item) return
      switch (ev.type) {
        case 'meta':
          item.citations = ev.citations
          break
        case 'thinking':
          item.thinking += ev.text
          item.thinkingLabel = '思考中'
          if (item.thinking.length > 40) item.showThink = false
          break
        case 'answer':
          item.content += ev.text
          hasStreamedAnswer = true
          item.typing = false
          break
        case 'no_hit':
          item.noHit = true
          item.noHitText = ev.message
          item.typing = false
          hasStreamedAnswer = false
          finishSending() // ✅ 无匹配也属完成，必须解除"正在回答中"
          break
        case 'done': {
          item.typing = false
          if (item.thinking && !item.showThink) item.showThink = false
          // 完成时刷新会话（标题/消息数可能变化）
          loadSessions().catch(() => {})
          finishSending() // ✅ 关键修复：正常回答完毕时关闭全局 sending 状态
          break
        }
        case 'saved':
          // 后端告知刚落库的回答编号：补上之后，这条回答才能被点赞/点踩
          item.msgId = ev.message_id
          break
        case 'blocked':
          // 内容安全命中：后端已经停止生成，并把"该显示什么"算好了（可能是标准提示语，
          // 也可能是把敏感处打码后的原文）。这里必须**整体覆盖**已显示的内容——
          // 因为违规文字刚才已经流到屏幕上了，只追加不覆盖等于没拦。
          item.content = ev.text
          item.blocked = true
          item.typing = false
          finishSending()
          loadSessions().catch(() => {})
          break
        case 'error':
          errorMsg = ev.message
          if (!item.content) {
            item.noHit = true
            item.noHitText = ev.message
          }
          item.typing = false
          finishSending() // ✅ 错误事件也兜底复位
          break
      }
      scrollBottom(true)
    },
    (msg) => {
      errorMsg = msg
      const item = displayMessages.value.find((m) => m.key === aKey)
      if (item && !item.content && !item.noHit) {
        item.noHit = true
        item.noHitText = msg
      }
      if (item) item.typing = false
      finishSending()
    },
  )
}

function stopGenerate() {
  abortCtrl?.abort()
  const item = [...displayMessages.value].reverse().find((m) => m.typing)
  if (item) {
    item.typing = false
    if (!item.content) {
      item.noHit = true
      item.noHitText = '已停止生成'
    }
  }
  sending.value = false
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    if (!sending.value) send()
  }
}

// ---------------- 渲染与交互 ----------------
// 回答渲染（含 XSS 转义）已抽到 @/utils/answerRender，便于单独审查与测试

function onAnswerClick(e: MouseEvent) {
  const target = (e.target as HTMLElement).closest?.('.cite-sup')
  if (!target) return
  const idx = Number(target.getAttribute('data-idx'))
  const item = displayMessages.value.find(
    (m) => m.role === 'assistant' && m.citations?.some((c) => c.index === idx),
  )
  if (item) {
    const c = item.citations!.find((x) => x.index === idx)
    if (c) openCitation(c, item)
  }
}

const drawer = ref({
  visible: false,
  title: '',
  citation: null as Citation | null,
})
function openCitation(c: Citation, m: MsgItem) {
  drawer.value = {
    visible: true,
    title: `引用来源 ${c.index} · ${c.doc_name}`,
    citation: c,
  }
}

function toggleThink(key: string) {
  const m = displayMessages.value.find((x) => x.key === key)
  if (m) m.showThink = !m.showThink
}

/** 用户反馈（赞/踩）：落库保存，供管理员在后台看"哪些回答没答好" */
const feedbackSupported = true
async function setFeedback(m: MsgItem, v: number) {
  // 点同一侧 = 取消（互斥：赞和踩只能留一个）
  const next = m.feedback === v ? 0 : v
  const prev = m.feedback          // 记下原值，请求失败时回滚，避免界面和库里对不上
  m.feedback = next === 0 ? null : next
  if (!feedbackSupported) return

  // 刚生成完的回答，编号可能还没送达（后端 saved 事件在路上）；
  // 这时先不做乐观更新以外的动作，避免把请求发到一个不存在的 id 上
  if (!m.msgId || !currentSession.value) {
    m.feedback = prev
    ElMessage.warning('回答还在保存中，请稍候一秒再点')
    return
  }
  try {
    await setMessageFeedback(currentSession.value.id, m.msgId, next)
  } catch {
    m.feedback = prev // 回滚：不能让界面显示"已点赞"但库里其实没记上
  }
}

function quickAsk(q: string) {
  if (!currentSession.value) {
    newSession().then(() => {
      setTimeout(() => {
        inputText.value = q
        send()
      }, 50)
    })
  } else {
    inputText.value = q
    send()
  }
}

async function scrollBottom(smooth = false) {
  await nextTick()
  const el = bottomAnchor.value
  el?.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'end' })
}
</script>

<style scoped>
.chat-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}

/* ---- 左栏 ---- */
.side {
  width: 280px;
  min-width: 280px;
  background: #f7f8fa;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  height: 100%;
}
.side-head {
  padding: 14px 14px 6px;
}
.new-btn {
  width: 100%;
}
.scope-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 14px 10px;
}
.scope-label {
  color: #909399;
  font-size: 12px;
  white-space: nowrap;
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px 12px;
}
.session-item {
  position: relative;
  padding: 10px 30px 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  transition: background 0.15s;
}
.session-item:hover {
  background: #eef0f3;
}
.session-item.active {
  background: #ecf5ff;
}
.s-title {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #303133;
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
}
.s-name {
  overflow: hidden;
  text-overflow: ellipsis;
}
.s-icon {
  color: #409eff;
  flex-shrink: 0;
}
.s-meta {
  color: #a8abb2;
  font-size: 11px;
  margin-top: 3px;
}
.s-del {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  color: #c0c4cc;
  padding: 4px;
  border-radius: 4px;
  opacity: 0;
  transition: opacity 0.15s;
}
.session-item:hover .s-del {
  opacity: 1;
}
.s-del:hover {
  color: #f56c6c;
  background: #fde2e2;
}
.side-loading {
  flex: 1;
}
.side-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* ---- 主区 ---- */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.welcome {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 24px;
  color: #606266;
}
.welcome h2 {
  color: #303133;
  font-size: 24px;
}
.tips {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-top: 18px;
  max-width: 560px;
}
.tip-card {
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 10px;
  padding: 12px 14px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}
.tip-card:hover {
  border-color: #409eff;
  color: #409eff;
  background: #ecf5ff;
}

.chat-head {
  padding: 12px 20px;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  gap: 6px;
}
.chat-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}
.rename-btn {
  margin-left: 4px;
}

/* ---- 消息流 ---- */
.msg-list {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  background: #fff;
}
.msg-row {
  display: flex;
  gap: 10px;
  margin-bottom: 18px;
  position: relative;
}
.msg-row.user {
  flex-direction: row-reverse;
}
.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  color: #fff;
  flex-shrink: 0;
  background: linear-gradient(135deg, #409eff, #79bbff);
}
.msg-row.user .avatar {
  background: linear-gradient(135deg, #67c23a, #95d475);
}
.bubble {
  max-width: min(76%, 780px);
}
.user-text {
  background: #95ec69;
  background: linear-gradient(135deg, #b3e19d, #95ec69);
  color: #1f2d3d;
  padding: 10px 14px;
  border-radius: 12px 2px 12px 12px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
  font-size: 14px;
}
.answer {
  background: #f5f7fa;
  padding: 12px 16px;
  border-radius: 2px 12px 12px 12px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.75;
  font-size: 14px;
  color: #303133;
}
.cite-sup {
  display: inline-block;
  min-width: 16px;
  text-align: center;
  margin: 0 2px;
  padding: 0 3px;
  font-size: 11px;
  line-height: 16px;
  border-radius: 8px;
  color: #409eff;
  background: #ecf5ff;
  border: 1px solid #d9ecff;
  cursor: pointer;
  vertical-align: super;
}
.cite-sup:hover {
  background: #409eff;
  color: #fff;
}

/* 思考过程 */
.think-box {
  margin-bottom: 8px;
  background: #fdf6ec;
  border: 1px solid #f3d19e;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 12px;
}
.think-hint {
  color: #e6a23c;
  cursor: pointer;
  user-select: none;
}
.think-text {
  color: #8d6a3c;
  margin-top: 6px;
  line-height: 1.6;
  max-height: 160px;
  overflow-y: auto;
  white-space: pre-wrap;
}

/* 思考中动画 */
.thinking-anim {
  display: flex;
  align-items: center;
  gap: 5px;
  background: #f5f7fa;
  border-radius: 2px 12px 12px 12px;
  padding: 14px 16px;
  width: fit-content;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #409eff;
  animation: bounce 1.2s infinite ease-in-out;
}
.dot:nth-child(2) { animation-delay: 0.15s; }
.dot:nth-child(3) { animation-delay: 0.3s; }
.thinking-label {
  margin-left: 6px;
  color: #909399;
  font-size: 13px;
}
@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.5; }
  40% { transform: scale(1); opacity: 1; }
}

/* 引用列表 */
.ref-list {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ref-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: #fff;
  border: 1px solid #d9ecff;
  border-radius: 6px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 12px;
  color: #409eff;
  transition: all 0.15s;
}
.ref-item:hover {
  background: #ecf5ff;
}
.ref-badge {
  background: #409eff;
  color: #fff;
  border-radius: 50%;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
}
.ref-doc {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ref-score {
  color: #c0c4cc;
  font-size: 11px;
}

.no-hit-tip {
  margin-top: 8px;
  font-size: 13px;
  color: #e6a23c;
}

/* 内容安全提示条：说明这条回答被系统处理过，避免用户以为是系统坏了 */
.blocked-tip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  padding: 4px 10px;
  font-size: 12.5px;
  color: #c0392b;
  background: #fdecea;
  border: 1px solid #f3c8c1;
  border-radius: 6px;
}

.feedback {
  display: flex;
  align-items: center;
  gap: 4px;
  align-self: flex-end;
  margin-top: 4px;
}
.fb {
  color: #c0c4cc;
  font-size: 16px;
  cursor: pointer;
  padding: 4px;
}
.fb.liked { color: #409eff; }
.fb.disliked { color: #f56c6c; }

/* ---- 输入区 ---- */
.input-bar {
  border-top: 1px solid #e4e7ed;
  padding: 12px 20px;
  background: #fff;
}
.input-wrap {
  max-width: 860px;
  margin: 0 auto;
}
.input-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}
.input-tip {
  color: #c0c4cc;
  font-size: 12px;
}

.cite-detail .cite-meta {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
}
.cite-score {
  color: #409eff;
  font-size: 13px;
}
.cite-page {
  color: #909399;
  font-size: 13px;
}
.cite-text {
  background: #f5f7fa;
  border-radius: 8px;
  padding: 14px;
  font-size: 13px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
