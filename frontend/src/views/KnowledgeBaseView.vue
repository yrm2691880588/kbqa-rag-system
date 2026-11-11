<template>
  <div class="kb-page">
    <!-- 顶部：标题 + 向量服务状态 + 新建知识库 -->
    <div class="page-head">
      <div class="head-left">
        <h2 class="title">知识库管理</h2>
        <el-tag v-if="health" :type="health.ok ? 'success' : 'danger'" effect="light" size="small">
          向量服务：{{ health.ok ? '正常' : '异常' }}
        </el-tag>
        <el-tooltip v-if="health && !health.ok" :content="health.message" placement="top">
          <el-icon class="warn-icon"><WarningFilled /></el-icon>
        </el-tooltip>
      </div>
      <el-button type="primary" @click="openCreate">
        <el-icon style="margin-right: 4px"><Plus /></el-icon>新建知识库
      </el-button>
    </div>

    <div class="body">
      <!-- 左：知识库列表 -->
      <el-card shadow="never" class="kb-panel">
        <template #header>
          <div class="panel-head"><span>知识库（{{ kbs.length }}）</span></div>
        </template>
        <el-empty v-if="!kbs.length" description="还没有知识库，点右上角新建" :image-size="70" />
        <div
          v-for="kb in kbs"
          :key="kb.id"
          class="kb-item"
          :class="{ active: currentKb?.id === kb.id }"
          @click="selectKb(kb)"
        >
          <div class="kb-item-top">
            <span class="kb-name" :title="kb.description || kb.name">{{ kb.name }}</span>
            <el-tag v-if="!kb.is_active" type="info" size="small">停用</el-tag>
            <el-dropdown trigger="click" @command="(cmd: string | number | object) => onKbCommand(String(cmd), kb)">
              <el-icon class="more-btn" @click.stop><MoreFilled /></el-icon>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="edit">编辑简介</el-dropdown-item>
                  <el-dropdown-item command="toggle">
                    {{ kb.is_active ? '停用（问答不可选）' : '启用' }}
                  </el-dropdown-item>
                  <el-dropdown-item command="delete" divided>删除知识库</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
          <div class="kb-meta">{{ kb.doc_count }} 个文档 · {{ kb.chunk_count }} 个片段</div>
        </div>
      </el-card>

      <!-- 右：文档管理 -->
      <el-card shadow="never" class="doc-panel">
        <template #header>
          <div class="panel-head doc-head">
            <div class="doc-title">
              <span v-if="currentKb" class="doc-kb-name">{{ currentKb.name }}</span>
              <span v-else class="doc-placeholder">← 先在左侧选择一个知识库</span>
              <span v-if="currentKb?.description" class="doc-desc">{{ currentKb.description }}</span>
            </div>
            <div class="doc-actions">
              <!-- 拖拽/点击上传，选中即自动上传 -->
              <el-upload
                :show-file-list="false"
                :http-request="doUploadBatch"
                :disabled="!currentKb || !currentKb.is_active"
                accept=".docx,.pdf,.xlsx,.txt,.md,.html,.htm"
                multiple
              >
                <el-button type="primary" :disabled="!currentKb || !currentKb.is_active" :loading="uploading">
                  <el-icon style="margin-right: 4px"><UploadFilled /></el-icon>上传文档
                </el-button>
              </el-upload>
              <div v-if="currentKb && !currentKb.is_active" class="disabled-tip">该库已停用，不能上传</div>
            </div>
          </div>
        </template>

        <el-empty v-if="!currentKb" description="选择左侧知识库后，在此上传和管理文档" :image-size="90" />
        <el-table v-else :data="docs" v-loading="loadingDocs" size="default">
          <el-table-column label="文件名" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="file-cell">
                <el-icon class="file-icon"><Document /></el-icon>{{ row.filename }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="70" align="center">
            <template #default="{ row }">
              <el-tag size="small" type="info" effect="plain">{{ row.doc_type.toUpperCase() }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="大小" width="90" align="right">
            <template #default="{ row }">{{ fmtSize(row.file_size) }}</template>
          </el-table-column>
          <el-table-column label="状态 / 进度" width="170">
            <template #default="{ row }">
              <div class="status-cell">
                <el-progress
                  v-if="row.status === 'embedding' || row.status === 'parsing'"
                  :percentage="row.progress"
                  :stroke-width="8"
                  :status="row.status === 'embedding' ? undefined : 'warning'"
                  :text-inside="false"
                />
                <el-tag v-else-if="row.status === 'ready'" type="success" size="small">已完成</el-tag>
                <el-tag v-else-if="row.status === 'failed'" type="danger" size="small">入库失败</el-tag>
                <el-tag v-else type="warning" size="small">
                  {{ row.status === 'pending' ? '排队中' : row.status }}
                </el-tag>
                <div v-if="row.status === 'embedding'" class="progress-label">向量化 {{ row.progress }}%</div>
                <div v-else-if="row.status === 'parsing'" class="progress-label">解析中…</div>
                <el-tooltip v-if="row.error_msg" :content="row.error_msg" placement="top">
                  <el-icon class="warn-icon" style="margin-left: 4px"><WarningFilled /></el-icon>
                </el-tooltip>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="片段数" width="80" align="center">
            <template #default="{ row }">
              <span v-if="row.status === 'ready'">{{ row.chunk_count }}</span>
              <span v-else class="muted">-</span>
            </template>
          </el-table-column>
          <el-table-column label="上传时间" width="160">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="190" align="center">
            <template #default="{ row }">
              <el-button v-if="row.status === 'ready'" link type="primary" size="small" @click="preview(row)">
                分块预览
              </el-button>
              <el-button
                v-if="row.status === 'failed'"
                link
                type="warning"
                size="small"
                :loading="retryingId === row.id"
                @click="retry(row)"
              >
                重试
              </el-button>
              <el-button link type="danger" size="small" @click="removeDoc(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description='该知识库还没有文档，点右上角"上传文档"' :image-size="80" />
          </template>
        </el-table>
      </el-card>
    </div>

    <!-- 新建/编辑知识库 -->
    <el-dialog v-model="kbDialog.visible" :title="kbDialog.isEdit ? '编辑知识库' : '新建知识库'" width="440px">
      <el-form label-width="70px">
        <el-form-item label="名称" required>
          <el-input v-model="kbDialog.name" :disabled="kbDialog.isEdit" maxlength="100" placeholder="如：手机商品库" />
        </el-form-item>
        <el-form-item label="简介">
          <el-input v-model="kbDialog.description" type="textarea" :rows="3" maxlength="500" show-word-limit placeholder="这个库放什么资料，方便以后识别" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="kbDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="savingKb" @click="saveKb">{{ kbDialog.isEdit ? '保存' : '创建' }}</el-button>
      </template>
    </el-dialog>

    <!-- 分块预览 -->
    <el-dialog v-model="chunkDialog.visible" :title="`分块预览：${chunkDialog.filename}`" width="720px" top="6vh">
      <div class="chunk-tip">共 {{ chunks.length }} 个片段（入库时自动切好的最小知识单元，问答时从中检索）</div>
      <el-scrollbar height="55vh">
        <div v-for="(c, i) in chunks" :key="c.chunk_index ?? i" class="chunk-card">
          <div class="chunk-head">
            <el-tag size="small" type="primary" effect="plain">片段 #{{ c.chunk_index }}</el-tag>
            <el-tag v-if="c.page" size="small" effect="plain">第 {{ c.page }} 页</el-tag>
          </div>
          <div class="chunk-text">{{ c.text }}</div>
        </div>
      </el-scrollbar>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
// 知识库管理页（M2）：多库 CRUD + 文档上传/进度/预览。仅管理员可进入（路由 + 后端双重校验）
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, MoreFilled, Plus, UploadFilled, WarningFilled } from '@element-plus/icons-vue'
import type { UploadRequestOptions } from 'element-plus'
import {
  createKB,
  deleteDoc,
  deleteKB,
  embeddingHealth,
  getDocChunks,
  listDocs,
  listKBs,
  retryDoc,
  updateKB,
  uploadDoc,
  uploadDocsBatch,
  type ChunkItem,
  type DocItem,
  type EmbeddingHealth,
  type KnowledgeBase,
} from '@/api/kb'
import { formatDateTime } from '@/utils/datetime'

// 知识库列表与选中项
const kbs = ref<KnowledgeBase[]>([])
const currentKb = ref<KnowledgeBase | null>(null)
const docs = ref<DocItem[]>([])
const loadingDocs = ref(false)
const health = ref<EmbeddingHealth | null>(null)
const uploading = ref(false)
const retryingId = ref<number | null>(null)

const kbDialog = ref({
  visible: false,
  isEdit: false,
  id: 0,
  name: '',
  description: '',
})
const savingKb = ref(false)

const chunkDialog = ref({ visible: false, filename: '' })
const chunks = ref<ChunkItem[]>([])

// 是否有文档还在处理中（驱动轮询）
const hasBusy = computed(() => docs.value.some((d) => ['pending', 'parsing', 'embedding'].includes(d.status)))
let pollTimer: ReturnType<typeof setInterval> | null = null

// ---- 加载 ----
async function loadKBs() {
  try {
    kbs.value = (await listKBs(true)).data
    // 选中项不存在时（删除/刷新）自动选第一个
    if (!currentKb.value || !kbs.value.some((k) => k.id === currentKb.value?.id)) {
      selectKb(kbs.value[0] ?? null)
    }
  } catch {
    /* 错误已由拦截器提示 */
  }
}

function selectKb(kb: KnowledgeBase | null) {
  currentKb.value = kb
  docs.value = []
  if (kb) loadDocs()
}

async function loadDocs() {
  if (!currentKb.value) return
  loadingDocs.value = true
  try {
    docs.value = (await listDocs(currentKb.value.id)).data
    schedulePoll()
  } finally {
    loadingDocs.value = false
  }
}

// ---- 轮询：有文档在入库时每 2 秒刷新一次，全完成即停 ----
function schedulePoll() {
  if (hasBusy.value && !pollTimer) {
    pollTimer = setInterval(async () => {
      if (!currentKb.value) return stopPoll()
      try {
        docs.value = (await listDocs(currentKb.value.id)).data
        // 顺带刷新库的片段计数
        await loadKBs()
        if (!hasBusy.value) stopPoll()
      } catch {
        stopPoll()
      }
    }, 2000)
  }
  if (!hasBusy.value) stopPoll()
}
function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// ---- 新建 / 编辑 ----
function openCreate() {
  kbDialog.value = { visible: true, isEdit: false, id: 0, name: '', description: '' }
}
function onKbCommand(cmd: string, kb: KnowledgeBase) {
  if (cmd === 'edit') {
    kbDialog.value = { visible: true, isEdit: true, id: kb.id, name: kb.name, description: kb.description ?? '' }
  } else if (cmd === 'toggle') {
    toggleKb(kb)
  } else if (cmd === 'delete') {
    removeKb(kb)
  }
}

async function saveKb() {
  const { isEdit, id, name, description } = kbDialog.value
  if (!name.trim()) return ElMessage.warning('请填写知识库名称')
  savingKb.value = true
  try {
    if (isEdit) {
      await updateKB(id, { description: description.trim() || undefined })
      ElMessage.success('已保存')
    } else {
      await createKB({ name: name.trim(), description: description.trim() || undefined })
      ElMessage.success('知识库已创建')
    }
    kbDialog.value.visible = false
    await loadKBs()
  } catch {
    /* 拦截器已提示 */
  } finally {
    savingKb.value = false
  }
}

async function toggleKb(kb: KnowledgeBase) {
  try {
    await updateKB(kb.id, { is_active: !kb.is_active })
    ElMessage.success(kb.is_active ? '已停用（普通用户问答不可选）' : '已启用')
    await loadKBs()
  } catch {
    /* 已提示 */
  }
}

async function removeKb(kb: KnowledgeBase) {
  try {
    await ElMessageBox.confirm(
      `删除知识库「${kb.name}」将同时删除其中 ${kb.doc_count} 个文档和全部 ${kb.chunk_count} 个片段，且不可恢复。确定删除？`,
      '危险操作',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteKB(kb.id)
    ElMessage.success('知识库已删除')
    currentKb.value = null
    await loadKBs()
  } catch {
    /* 已提示 */
  }
}

// ---- 上传 ----
/**
 * 批量上传：把用户这次选中的所有文件一次性提交。
 *
 * 为什么不用 el-upload 的 http-request（它是逐文件调用的）：
 *   拖 20 个文件 = 20 次串行请求，慢且中途失败会留下半截状态。
 *   这里改成先收集、再一次性调批量接口，服务端逐个处理并返回
 *   "哪几个成功、哪几个失败、为什么失败"。
 */
async function doUploadBatch(opt: UploadRequestOptions) {
  if (!currentKb.value) return

  // el-upload 的 http-request 会被每个文件调一次，
  // 这里把文件先攒起来，等同一批的多个文件都到了再一起发。
  pendingFiles.push(opt.file)
  if (uploadTimer) clearTimeout(uploadTimer)
  uploadTimer = window.setTimeout(() => flushUploads(), 120)
}

let uploadTimer: number | undefined
const pendingFiles: File[] = []

async function flushUploads() {
  if (!currentKb.value || !pendingFiles.length) return
  const files = pendingFiles.splice(0, pendingFiles.length)
  uploading.value = true
  try {
    const res = await uploadDocsBatch(currentKb.value.id, files)
    if (res.failed === 0) {
      ElMessage.success(`${res.succeeded} 个文档已上传，开始入库`)
    } else {
      // 部分成功：把失败原因逐条列出来，用户才知道该改哪个文件
      const detail = res.failures
        .slice(0, 3)
        .map((f) => `${f.filename}：${f.reason}`)
        .join('；')
      const more = res.failures.length > 3 ? ` 等 ${res.failures.length} 个` : ''
      ElMessage.warning(`${res.succeeded} 个成功，${res.failed} 个失败${more}。${detail}`)
    }
    await loadDocs()
  } catch {
    /* 整体失败（如网络/权限）由拦截器提示 */
  } finally {
    uploading.value = false
  }
}

// ---- 文档操作 ----
async function removeDoc(row: DocItem) {
  try {
    await ElMessageBox.confirm(`确定删除文档「${row.filename}」？其 ${row.chunk_count} 个片段会一并清理。`, '删除文档', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await deleteDoc(row.id)
    ElMessage.success('文档已删除')
    await loadDocs()
  } catch {
    /* 已提示 */
  }
}

async function retry(row: DocItem) {
  retryingId.value = row.id
  try {
    await retryDoc(row.id)
    ElMessage.success('已重新入队')
    await loadDocs()
  } finally {
    retryingId.value = null
  }
}

async function preview(row: DocItem) {
  chunkDialog.value = { visible: true, filename: row.filename }
  chunks.value = []
  try {
    chunks.value = (await getDocChunks(row.id)).data
  } catch {
    /* 已提示 */
  }
}

// ---- 工具 ----
function fmtSize(bytes: number): string {
  if (!bytes) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

onMounted(async () => {
  await loadKBs()
  try {
    health.value = (await embeddingHealth()).data
  } catch {
    /* 未配置 Key 等场景：健康检查失败也提示即可 */
  }
})

onBeforeUnmount(stopPoll)
</script>

<style scoped>
.kb-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px;
  gap: 12px;
  box-sizing: border-box;
}

.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.head-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}
.warn-icon {
  color: var(--warning);
  cursor: help;
}

.body {
  flex: 1;
  display: flex;
  gap: 12px;
  min-height: 0;
}
.kb-panel {
  width: 280px;
  flex-shrink: 0;
  overflow-y: auto;
}
.doc-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}
.panel-head {
  font-weight: 600;
  font-size: 14px;
}
.kb-item {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
  margin-bottom: 4px;
}
.kb-item:hover {
  background: var(--bg-hover);
}
.kb-item.active {
  background: var(--brand-50);
}
.kb-item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.kb-name {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.more-btn {
  cursor: pointer;
  color: var(--text-4);
  flex-shrink: 0;
}
.more-btn:hover {
  color: var(--brand-500);
}
.kb-meta {
  font-size: 12px;
  color: var(--text-4);
  margin-top: 4px;
}
.doc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.doc-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.doc-kb-name {
  font-size: 15px;
  font-weight: 600;
}
.doc-desc {
  color: var(--text-4);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}
.doc-placeholder {
  color: var(--text-4);
  font-size: 13px;
}
.doc-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.disabled-tip {
  font-size: 12px;
  color: var(--warning);
}
.file-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.file-icon {
  color: var(--brand-500);
}
.status-cell {
  display: flex;
  align-items: center;
}
.progress-label {
  font-size: 12px;
  color: var(--text-4);
  margin-top: 2px;
}
.muted {
  color: var(--text-5);
}
.chunk-tip {
  font-size: 12px;
  color: var(--text-4);
  margin-bottom: 10px;
}
.chunk-card {
  border: 1px solid var(--border-2);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
  background: var(--bg-hover);
}
.chunk-head {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}
.chunk-text {
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-1);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
