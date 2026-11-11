// 会话与问答 API（M3）：会话 CRUD + 历史消息 + SSE 流式问答
// 注：令牌已改为 HttpOnly Cookie，前端不再需要读取令牌，故不引入 auth store。
import http from './http'

export interface ChatSession {
  id: number
  title: string
  kb_scope: number[] | null
  message_count: number
  created_at: string
  updated_at: string
}

export interface Citation {
  index: number
  doc_name: string
  page: number | null
  text: string
  score: number
  kb_id: number
}

export interface ChatMessage {
  id: number
  session_id: number
  role: 'user' | 'assistant'
  content: string
  citations: Citation[] | null
  feedback: number | null
  created_at: string
}

// ---- 会话管理 ----
export function listSessions() {
  return http.get<ChatSession[]>('/sessions')
}

export function createSession(kbIds?: number[] | null) {
  return http.post<ChatSession>('/sessions', { kb_ids: kbIds ?? null })
}

export function renameSession(id: number, title: string) {
  return http.patch<ChatSession>(`/sessions/${id}`, { title })
}

export function deleteSession(id: number) {
  return http.delete(`/sessions/${id}`)
}

export function listMessages(sessionId: number) {
  return http.get<ChatMessage[]>(`/sessions/${sessionId}/messages`)
}

/**
 * 给某条 AI 回答打「赞 / 踩 / 取消」。
 * feedback：1=赞，-1=踩，0=取消之前的评价。
 */
export function setMessageFeedback(
  sessionId: number,
  messageId: number,
  feedback: number,
  note?: string,
  reason?: string,
) {
  return http.post<ChatMessage>(`/sessions/${sessionId}/messages/${messageId}/feedback`, {
    feedback,
    note: note ?? null,
    reason: reason ?? null,
  })
}

// ---- SSE 流式问答 ----
export type StreamEvent =
  | { type: 'meta'; citations: Citation[] }
  | { type: 'thinking'; text: string }
  | { type: 'answer'; text: string }
  | { type: 'no_hit'; message: string; citations: Citation[] }
  | { type: 'done'; latency_ms?: number }
  // 后端把刚落库的回答编号回传，前端据此才能给"这一轮"的回答点赞/点踩
  | { type: 'saved'; message_id: number }
  // 回答命中内容安全策略：后端已停止生成，前端要用 text 把已显示的内容整体覆盖掉
  | { type: 'blocked'; text: string; mode: string }
  | { type: 'error'; message: string }

/**
 * 发送问题并消费 SSE 事件流。
 * onEvent 逐个回调事件；
 * onError 在网络/解析出错时回调（不包含用户主动 abort）；
 * onComplete 在浏览器读到流末尾时调用,无论后端 done 事件是否到达都会触发,
 *   用于关闭"正在回答中"等全局状态,避免后端 done 丢失时前端卡死。
 * 返回的 AbortController 可用于"停止生成"。
 */
export function streamChat(
  sessionId: number,
  question: string,
  onEvent: (ev: StreamEvent) => void,
  onError: (msg: string) => void,
  onComplete: () => void = () => {},
): AbortController {
  const controller = new AbortController()

  // 【安全改造】不再手工拼 Authorization 头。
  // 令牌在 HttpOnly Cookie 里，fetch 同源请求会自动携带。
  // credentials: 'include' 是为了兼容前后端不同源部署的场景；
  // 同源时它等同于默认行为，没有副作用。
  fetch(`/api/v1/chat`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ session_id: sessionId, question }),
    signal: controller.signal,
  })
    .then(async (resp) => {
      if (!resp.ok) {
        // 非 200：读后端错误信息
        let msg = `请求失败（${resp.status}）`
        try {
          const data = await resp.json()
          if (data?.detail) msg = data.detail
        } catch {
          /* 忽略解析失败 */
        }
        onError(msg)
        onComplete() // 非 200 也要走兜底,关掉 sending
        return
      }
      if (!resp.body) {
        onError('浏览器不支持流式读取')
        onComplete()
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      // 按 SSE 块解析：每块以空行分隔，块内 "event: xxx" 与 "data: {...}" 两行
      const handleChunk = (chunk: string) => {
        buffer += chunk
        let sep: number
        while ((sep = buffer.indexOf('\n\n')) !== -1) {
          const raw = buffer.slice(0, sep)
          buffer = buffer.slice(sep + 2)
          const lines = raw.split('\n')
          let evName = 'message'
          let dataStr = ''
          for (const line of lines) {
            if (line.startsWith('event:')) evName = line.slice(6).trim()
            else if (line.startsWith('data:')) dataStr += line.slice(5).trim()
          }
          if (!dataStr) continue
          try {
            const data = JSON.parse(dataStr)
            switch (evName) {
              case 'meta':
                onEvent({ type: 'meta', citations: data.citations ?? [] })
                break
              case 'thinking':
                onEvent({ type: 'thinking', text: data.text ?? '' })
                break
              case 'answer':
                onEvent({ type: 'answer', text: data.text ?? '' })
                break
              case 'no_hit':
                onEvent({ type: 'no_hit', message: data.message ?? '', citations: data.citations ?? [] })
                break
              case 'done':
                onEvent({ type: 'done', latency_ms: data.latency_ms })
                break
              case 'saved':
                onEvent({ type: 'saved', message_id: Number(data.message_id) })
                break
              case 'blocked':
                onEvent({ type: 'blocked', text: data.text ?? '', mode: data.mode ?? 'replace' })
                break
              case 'error':
                onEvent({ type: 'error', message: data.message ?? '生成失败' })
                break
              default:
                break
            }
          } catch {
            /* 忽略无法解析的块 */
          }
        }
      }
      let completed = false
      const safeComplete = () => {
        if (completed) return
        completed = true
        onComplete()
      }
      // 读取循环
      const pump = (): Promise<void> =>
        reader.read().then(({ done, value }) => {
          if (done) {
            handleChunk('') // 冲刷剩余 buffer（一般无）
            safeComplete()   // 浏览器读到流末尾兜底，必触发
            return
          }
          handleChunk(decoder.decode(value, { stream: true }))
          return pump()
        })
      return pump().catch((e) => {
        if (e?.name !== 'AbortError') {
          onError(e?.message || '读取流失败')
        }
        safeComplete()
      })
    })
    .catch((err) => {
      if (err?.name === 'AbortError') return // 用户主动停止：不算错误
      onError(err?.message || '网络错误，请检查服务是否在运行')
      onComplete()
    })

  return controller
}
