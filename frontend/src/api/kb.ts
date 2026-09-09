// 知识库管理相关接口封装
import http from './http'

export interface KnowledgeBase {
  id: number
  name: string
  description: string | null
  is_active: boolean
  doc_count: number
  chunk_count: number
  created_at: string
}

export interface DocItem {
  id: number
  kb_id: number
  filename: string
  doc_type: string
  file_size: number
  status: string
  progress: number
  version: number
  chunk_count: number
  error_msg: string | null
  created_at: string
  updated_at: string
}

export interface ChunkItem {
  chunk_index: number | null
  page: number | null
  text: string
}

export interface EmbeddingHealth {
  ok: boolean
  message: string
}

// ---- 知识库 ----
export const listKBs = (all = false) => http.get<KnowledgeBase[]>('/knowledge-bases', { params: { all } })
export const createKB = (data: { name: string; description?: string }) =>
  http.post<KnowledgeBase>('/knowledge-bases', data)
export const updateKB = (id: number, data: { description?: string; is_active?: boolean }) =>
  http.put<KnowledgeBase>(`/knowledge-bases/${id}`, data)
export const deleteKB = (id: number) => http.delete(`/knowledge-bases/${id}`)

// ---- 文档 ----
export const listDocs = (kbId: number) => http.get<DocItem[]>(`/knowledge-bases/${kbId}/documents`)
export const uploadDoc = (kbId: number, file: File) => {
  const form = new FormData()
  form.append('file', file)
  return http.post<DocItem>(`/knowledge-bases/${kbId}/documents`, form, {
    timeout: 180000, // 上传大文件放宽超时
  })
}
export const getDoc = (docId: number) => http.get<DocItem>(`/documents/${docId}`)
export const deleteDoc = (docId: number) => http.delete(`/documents/${docId}`)
export const retryDoc = (docId: number) => http.post<DocItem>(`/documents/${docId}/retry`)
export const getDocChunks = (docId: number) => http.get<ChunkItem[]>(`/documents/${docId}/chunks`)
export const embeddingHealth = () => http.get<EmbeddingHealth>('/ai/embedding-health')
