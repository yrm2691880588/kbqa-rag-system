/**
 * 聊天消息的数据结构与转换（从 ChatView.vue 抽出）。
 *
 * 为什么抽出来：
 *   1) MsgItem 是个有 13 个字段的结构，ChatView 里多处都在拼它。
 *      字段一多，随手少填一个就变成 undefined，且 TypeScript 未必报错
 *      （可选字段）。集中在这里创建，能保证结构完整。
 *   2) toMsgItem 是**纯数据转换**，不碰组件状态，最适合先抽出来。
 *   3) key 生成依赖一个自增计数器，原本散在组件里（3 处使用），
 *      抽到模块里变成私有状态，外部不用关心。
 */

/** 引用来源（与 api/chat.ts 的 Citation 对应，这里避免跨模块循环引用）。 */
export interface CitationLike {
  index: number
  doc_name: string
  page: number | null
  text: string
  score: number
  kb_id: number
}

/** 来自后端的聊天消息（与 api/chat.ts 的 ChatMessage 对应）。 */
export interface ApiMessageLike {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations?: CitationLike[] | null
  feedback?: number | null
}

/** 界面展示用的消息条目。 */
export interface MsgItem {
  key: string
  role: 'user' | 'assistant'
  content: string
  thinking: string
  showThink: boolean
  citations: CitationLike[] | null
  feedback: number | null
  /** 点踩时选的原因（与后端 FEEDBACK_REASONS 的 key 对应） */
  feedbackReason?: string | null
  typing: boolean
  noHit: boolean
  noHitText: string
  thinkingLabel: string
  /** 内容安全命中：这条回答被系统处理过（界面上给个提示） */
  blocked: boolean
  /** 已入库消息的真实 id（历史加载时有值） */
  msgId?: number
  /**
   * 答案自检多轮重做后仍不合格。
   *
   * 为 true 时 content 已被替换成"无法给出可靠回答"的说明 ——
   * 界面上可以额外标一个提示，让用户知道这不是普通回答而是拒答，
   * 避免误以为系统只是回答得简短。
   */
  verifiedFailed?: boolean
}

/** 消息 key 的自增序号。放在模块内，避免组件里再多一个可变状态。 */
let keySeq = 0

/** 生成一个唯一 key（Vue 列表渲染用）。 */
export function nextMessageKey(prefix: string): string {
  return `${prefix}-${keySeq++}`
}

/** 把后端返回的一条历史消息转成界面条目。 */
export function toMsgItem(m: ApiMessageLike): MsgItem {
  return {
    key: `h-${m.id}-${keySeq++}`,
    role: m.role,
    content: m.content,
    thinking: '',
    showThink: false,
    citations: m.citations ?? null,
    feedback: m.feedback ?? null,
    feedbackReason: null,
    typing: false,
    noHit: false,
    noHitText: '',
    thinkingLabel: '',
    blocked: false,
    verifiedFailed: false,
    msgId: m.id,
  }
}

/** 创建一条"正在输入中"的 AI 占位消息。 */
export function createTypingMessage(): MsgItem {
  return {
    key: nextMessageKey('a'),
    role: 'assistant',
    content: '',
    thinking: '',
    showThink: false,
    citations: null,
    feedback: null,
    feedbackReason: null,
    typing: true,
    noHit: false,
    noHitText: '',
    thinkingLabel: '',
    blocked: false,
  }
}

/** 创建一条用户消息（尚未入库，所以没有 msgId）。 */
export function createUserMessage(content: string): MsgItem {
  return {
    key: nextMessageKey('u'),
    role: 'user',
    content,
    thinking: '',
    showThink: false,
    citations: null,
    feedback: null,
    typing: false,
    noHit: false,
    noHitText: '',
    thinkingLabel: '',
    blocked: false,
  }
}
