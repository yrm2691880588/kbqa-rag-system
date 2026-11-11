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
      <!-- 会话搜索：按**消息正文**搜，不只搜标题。
           会话标题是自动取"问题前 18 字"，时间一长根本认不出来；
           真正好用的是"我记得问过某个词"。 -->
      <div class="search-bar">
        <el-input
          v-model="sessionKeyword"
          size="small"
          placeholder="搜索历史对话内容"
          clearable
          @keyup.enter="loadSessions()"
          @clear="loadSessions()"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
      </div>
      <div v-if="loadingSessions" v-loading="true" class="side-loading" />
      <div v-else-if="!sessions.length" class="side-empty">
        <el-empty
          :description="sessionKeyword ? '没有匹配的对话' : '还没有会话'"
          :image-size="60"
        />
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
              <el-tooltip
                :content="m.feedback === -1 && m.feedbackReason ? `已反馈：${reasonLabel(m.feedbackReason)}` : '回答不准确'"
                placement="top"
              >
                <el-icon class="fb" :class="{ disliked: m.feedback === -1 }" @click="setFeedback(m, -1)"><Bottom /></el-icon>
              </el-tooltip>
              <!-- 重新生成：对同一问题再答一次。
                   只在最后一条回答上显示 —— 用户通常是想"这条不满意，换个答案"，
                   给所有历史回答都挂按钮反而干扰阅读。 -->
              <el-tooltip
                v-if="m === lastAssistantMessage && !sending"
                content="换个答案重新生成"
                placement="top"
              >
                <el-icon class="fb" @click="regenerate"><RefreshRight /></el-icon>
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

    <!-- 点踩时的原因收集弹窗。
         只在用户表达"不满意"时出现，不打断正常阅读。 -->
    <FeedbackDialog
      v-model="feedbackDialog.visible"
      :submitting="feedbackDialog.submitting"
      @submit="onFeedbackSubmit"
    />
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
import {
  createTypingMessage,
  createUserMessage,
  toMsgItem,
  type MsgItem,
} from '@/utils/chatMessage'
import { formatChatTime } from '@/utils/datetime'
import { renderAnswer } from '@/utils/answerRender'
import FeedbackDialog from '@/components/chat/FeedbackDialog.vue'

const auth = useAuthStore()

/** 原因 key -> 中文标签（与后端 FEEDBACK_REASONS 一致） */
const REASON_LABELS: Record<string, string> = {
  inaccurate: '回答不准确',
  no_reference: '没有引用到相关资料',
  irrelevant: '答非所问',
  incomplete: '回答不完整',
  wrong_citation: '引用错了文档',
  outdated: '资料过时',
  other: '其他',
}
function reasonLabel(key?: string | null): string {
  return key ? (REASON_LABELS[key] ?? key) : ''
}

// ---------------- 会话状态 ----------------
const sessions = ref<ChatSession[]>([])
const currentSession = ref<ChatSession | null>(null)
const loadingSessions = ref(true)
/** 会话搜索关键词（按消息正文搜） */
const sessionKeyword = ref('')
const loadingHistory = ref(false)
const activeKbs = ref<KnowledgeBase[]>([])
const scopeKbId = ref(0) // 0=全部库；>0=指定库（仅新会话生效）

// ---------------- 消息展示 ----------------

const displayMessages = ref<MsgItem[]>([])
const inputText = ref('')
const sending = ref(false)
let abortCtrl: AbortController | null = null

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
    sessions.value = await listSessions(sessionKeyword.value.trim() || undefined)
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
/**
 * 发送一轮提问。
 *
 * @param opts.question   指定问题（重新生成时传原问题）。不传则取输入框内容。
 * @param opts.regenerate 是否为"重新生成"：后端据此不重复存消息、不重复扣额度。
 */
async function send(opts: { question?: string; regenerate?: boolean } = {}) {
  const isRegen = !!opts.regenerate
  const q = isRegen ? (opts.question ?? '') : inputText.value.trim()
  if (!q || sending.value || !currentSession.value) return
  if (!isRegen) inputText.value = ''

  const sid = currentSession.value.id
  // 本地先展示 AI 占位（思考中动画）。
  // 重新生成时**不再插一条用户消息** —— 那个问题已经在界面上，
  // 再插一条会出现两条一模一样的问题。
  if (!isRegen) {
    displayMessages.value.push(createUserMessage(q))
  }
  const aiItem = createTypingMessage()
  aiItem.thinkingLabel = '正在思考'
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
      const item = displayMessages.value.find((m) => m.key === aiItem.key)
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
      const item = displayMessages.value.find((m) => m.key === aiItem.key)
      if (item && !item.content && !item.noHit) {
        item.noHit = true
        item.noHitText = msg
      }
      if (item) item.typing = false
      finishSending()
    },
    () => {},          // onComplete：本轮不需要额外收尾，留空
    { regenerate: isRegen },
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

/** 最后一条 AI 回答（"重新生成"按钮只在它下面显示） */
const lastAssistantMessage = computed(() => {
  const list = displayMessages.value
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].role === 'assistant' && !list[i].typing) return list[i]
  }
  return null
})

/**
 * 重新生成当前问题。
 *
 * 先把这条提问之后的 AI 回答从界面移除（它们马上会被新答案取代，
 * 留着会短暂出现"两条回答"的错觉），再带 regenerate 标记重发。
 */
async function regenerate() {
  if (sending.value || !currentSession.value) return
  const list = displayMessages.value
  let lastUserIdx = -1
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].role === 'user') {
      lastUserIdx = i
      break
    }
  }
  if (lastUserIdx < 0) {
    ElMessage.warning('还没有提问，无法重新生成')
    return
  }
  const question = list[lastUserIdx].content
  list.splice(lastUserIdx + 1, list.length - lastUserIdx - 1)
  await send({ question, regenerate: true })
}

/** 反馈弹窗状态。点踩时才打开，问"哪里不准确"。 */
const feedbackDialog = ref({
  visible: false,
  submitting: false,
  target: null as MsgItem | null,
})

/**
 * 用户反馈（赞/踩）。
 *
 * 行为差异（这是刻意的）：
 *   · 点赞  —— 一键完成。满意是简单信号，不需要追问。
 *   · 点踩  —— 弹出原因选择。不满意必须问清"哪里不对"，
 *              否则后台只看到"有人不满"却不知道该改什么。
 *   · 再点同侧 —— 取消评价（不弹窗，直接清空）。
 */
async function setFeedback(m: MsgItem, v: number) {
  // 刚生成完的回答，编号可能还没送达（后端 saved 事件在路上）
  if (!m.msgId || !currentSession.value) {
    ElMessage.warning('回答还在保存中，请稍候一秒再点')
    return
  }

  const isCancel = m.feedback === v

  // 点踩且不是取消 -> 先问原因
  if (v === -1 && !isCancel) {
    feedbackDialog.value.target = m
    feedbackDialog.value.visible = true
    return
  }

  // 其余情况（点赞 / 取消）直接提交
  await submitFeedback(m, isCancel ? 0 : v)
}

/** 真正发请求的地方（赞、踩带原因、取消都走这里）。 */
async function submitFeedback(
  m: MsgItem,
  feedback: number,
  reason?: string,
  note?: string,
) {
  const prev = m.feedback
  const prevReason = m.feedbackReason
  // 乐观更新：先改界面，失败再回滚，避免点击后没反应
  m.feedback = feedback === 0 ? null : feedback
  m.feedbackReason = feedback === -1 ? (reason ?? null) : null

  try {
    await setMessageFeedback(
      currentSession.value!.id,
      m.msgId!,
      feedback,
      note,
      reason,
    )
    if (feedback === -1) ElMessage.success('感谢反馈，我们会据此改进')
    else if (feedback === 0) ElMessage.info('已取消评价')
  } catch {
    m.feedback = prev
    m.feedbackReason = prevReason
  }
}

/** 弹窗提交：带上用户选的原因与补充说明 */
async function onFeedbackSubmit(payload: { reason: string; note: string }) {
  const m = feedbackDialog.value.target
  if (!m) return
  feedbackDialog.value.submitting = true
  try {
    await submitFeedback(m, -1, payload.reason, payload.note)
    feedbackDialog.value.visible = false
  } finally {
    feedbackDialog.value.submitting = false
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
  background: var(--bg-page);
  border-right: 1px solid var(--border-2);
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
.search-bar {
  padding: 0 12px 8px;
}

.scope-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 14px 10px;
}
.scope-label {
  color: var(--text-4);
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
  border-radius: var(--radius-sm);
  cursor: pointer;
  margin-bottom: 2px;
  transition: background var(--transition-fast);
  border: 1px solid transparent;
}
.session-item:hover {
  background: var(--bg-hover);
}
/* 选中态：白色卡片 + 主色左边条，比整块浅蓝更精致，也不与深色导航打架 */
.session-item.active {
  background: var(--bg-card);
  border-color: var(--border-2);
  box-shadow: var(--shadow-sm);
}
.session-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 60%;
  border-radius: 0 2px 2px 0;
  background: var(--brand-500);
}
.s-title {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--text-1);
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
  color: var(--brand-500);
  flex-shrink: 0;
}
.s-meta {
  color: var(--text-4);
  font-size: 11px;
  margin-top: 3px;
}
.s-del {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-5);
  padding: 4px;
  border-radius: 4px;
  opacity: 0;
  transition: opacity 0.15s;
}
.session-item:hover .s-del {
  opacity: 1;
}
.s-del:hover {
  color: var(--danger);
  background: var(--danger-bg);
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
  color: var(--text-3);
}
.welcome h2 {
  color: var(--text-1);
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
  background: var(--bg-hover);
  border: 1px solid var(--border-1);
  border-radius: 10px;
  padding: 12px 14px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}
.tip-card:hover {
  border-color: var(--brand-500);
  color: var(--brand-500);
  background: var(--brand-50);
}

.chat-head {
  padding: 12px 20px;
  border-bottom: 1px solid var(--border-1);
  display: flex;
  align-items: center;
  gap: 6px;
}
.chat-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-1);
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
  width: 34px;
  height: 34px;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  flex-shrink: 0;
  /* AI 头像：品牌渐变 */
  background: linear-gradient(135deg, var(--brand-400), var(--brand-600));
  box-shadow: 0 2px 6px rgba(79, 110, 247, 0.25);
}
/* 用户头像：中性深灰。
   原来用绿色是为了区分，但绿色与全站蓝色冲突；
   改用中性色既有区分度，又不会引入第三种色系。 */
.msg-row.user .avatar {
  background: linear-gradient(135deg, var(--text-3), var(--text-3));
  box-shadow: 0 2px 6px rgba(75, 85, 99, 0.25);
}
.bubble {
  max-width: min(76%, 780px);
}
.user-text {
  /* 原来是微信绿，与全站蓝色系冲突。
     改成品牌蓝实色 + 白字：对比度高、一眼能分清"我说的"和"AI 说的"。 */
  background: linear-gradient(135deg, var(--brand-500), var(--brand-600));
  color: #fff;
  padding: 11px 15px;
  border-radius: var(--radius-md) var(--radius-sm) var(--radius-md) var(--radius-md);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.65;
  font-size: var(--font-base);
  box-shadow: 0 2px 8px rgba(79, 110, 247, 0.22);
}
.answer {
  /* 原来是 var(--bg-hover) 灰底，与页面底色几乎一样，"浮"不起来。
     改成白底 + 极淡阴影，与用户气泡形成明确对比。 */
  background: var(--bg-card);
  padding: 13px 16px;
  border-radius: var(--radius-sm) var(--radius-md) var(--radius-md) var(--radius-md);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.75;
  font-size: var(--font-base);
  color: var(--text-2);
  border: 1px solid var(--border-2);
  box-shadow: var(--shadow-sm);
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
  color: var(--brand-500);
  background: var(--brand-50);
  border: 1px solid var(--brand-100);
  cursor: pointer;
  vertical-align: super;
}
.cite-sup:hover {
  background: var(--brand-500);
  color: #fff;
}

/* 思考过程 */
.think-box {
  margin-bottom: 8px;
  background: var(--warning-bg);
  border: 1px solid var(--warning);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 12px;
}
.think-hint {
  color: var(--warning);
  cursor: pointer;
  user-select: none;
}
.think-text {
  color: var(--warning);
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
  background: var(--bg-hover);
  border-radius: 2px 12px 12px 12px;
  padding: 14px 16px;
  width: fit-content;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--brand-500);
  animation: bounce 1.2s infinite ease-in-out;
}
.dot:nth-child(2) { animation-delay: 0.15s; }
.dot:nth-child(3) { animation-delay: 0.3s; }
.thinking-label {
  margin-left: 6px;
  color: var(--text-4);
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
  border: 1px solid var(--brand-100);
  border-radius: 6px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 12px;
  color: var(--brand-500);
  transition: all 0.15s;
}
.ref-item:hover {
  background: var(--brand-50);
}
.ref-badge {
  background: var(--brand-500);
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
  color: var(--text-5);
  font-size: 11px;
}

.no-hit-tip {
  margin-top: 8px;
  font-size: 13px;
  color: var(--warning);
}

/* 内容安全提示条：说明这条回答被系统处理过，避免用户以为是系统坏了 */
.blocked-tip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  padding: 4px 10px;
  font-size: 12.5px;
  color: var(--danger);
  background: var(--danger-bg);
  border: 1px solid var(--danger);
  border-radius: 6px;
}

.feedback {
  display: flex;
  align-items: center;
  gap: 2px;
  align-self: flex-end;
  margin-top: 6px;
  /* 默认半透明，悬停到气泡上时才明显 —— 避免每条回答下面都挂一排图标干扰阅读 */
  opacity: 0.45;
  transition: opacity var(--transition-fast);
}
.msg-row:hover .feedback {
  opacity: 1;
}
.fb {
  color: var(--text-4);
  font-size: 15px;
  cursor: pointer;
  padding: 5px;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}
.fb:hover {
  background: var(--bg-hover);
  color: var(--brand-500);
}
.fb.liked { color: var(--brand-500); }
.fb.disliked { color: var(--danger); }

/* ---- 输入区 ----
   原来就是一个普通 textarea 贴底。现在做成"浮动输入卡"：
   白底 + 圆角 + 阴影 + 聚焦时主色描边，视觉上更集中。 */
.input-bar {
  border-top: 1px solid var(--border-2);
  padding: 14px 20px 16px;
  background: var(--bg-card);
}
.input-wrap {
  max-width: 860px;
  margin: 0 auto;
}
.input-wrap :deep(.el-textarea__inner) {
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  box-shadow: none;
  padding: 12px 14px;
  font-size: var(--font-base);
  line-height: 1.65;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.input-wrap :deep(.el-textarea__inner:hover) {
  border-color: var(--brand-300);
}
.input-wrap :deep(.el-textarea__inner:focus) {
  border-color: var(--brand-500);
  box-shadow: 0 0 0 3px var(--brand-50);
}
.input-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}
.input-tip {
  color: var(--text-4);
  font-size: var(--font-xs);
}

.cite-detail .cite-meta {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
}
.cite-score {
  color: var(--brand-500);
  font-size: 13px;
}
.cite-page {
  color: var(--text-4);
  font-size: 13px;
}
.cite-text {
  background: var(--bg-hover);
  border-radius: 8px;
  padding: 14px;
  font-size: 13px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
