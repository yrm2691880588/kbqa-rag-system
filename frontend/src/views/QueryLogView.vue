<template>
  <!-- 问答日志：把一直"只写不读"的记录变成优化知识库的依据 -->
  <div class="page">
    <div class="page-head">
      <div class="head-left">
        <h2 class="title">问答日志</h2>
      </div>
      <div class="head-right">
        <el-select v-model="filter" style="width: 150px" @change="reload">
          <el-option label="全部问题" value="all" />
          <el-option label="只看没答上" value="unanswered" />
          <el-option label="只看答上的" value="answered" />
        </el-select>
        <el-input
          v-model="keyword"
          placeholder="搜问题内容"
          clearable
          style="width: 220px"
          @keyup.enter="reload"
          @clear="reload"
        >
          <template #append>
            <el-button @click="reload">搜索</el-button>
          </template>
        </el-input>
        <el-button @click="reload">刷新</el-button>
      </div>
    </div>

    <!-- 概览：管理员打开页面第一个想知道的是"整体答得怎么样" -->
    <div class="stats">
      <div class="stat-card">
        <div class="stat-num">{{ stats.total }}</div>
        <div class="stat-label">总提问</div>
      </div>
      <div class="stat-card ok">
        <div class="stat-num">{{ stats.answered }}</div>
        <div class="stat-label">答上了</div>
      </div>
      <div class="stat-card bad">
        <div class="stat-num">{{ stats.unanswered }}</div>
        <div class="stat-label">没答上</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{{ (stats.avg_latency_ms / 1000).toFixed(1) }}s</div>
        <div class="stat-label">平均耗时</div>
      </div>
    </div>

    <el-alert v-if="stats.unanswered > 0" type="warning" :closable="false" show-icon class="tip">
      <template #title>
        有 <b>{{ stats.unanswered }}</b> 个问题系统没能从知识库里答上来。
        这通常意味着资料缺失或表述不一致——点开看看用户到底怎么问的，再补资料，效果最直接。
      </template>
    </el-alert>

    <el-card shadow="never">
      <el-table :data="logs" v-loading="loading" size="default" @row-click="openDetail">
        <el-table-column label="问题" min-width="300" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="q">{{ row.question }}</span>
          </template>
        </el-table-column>
        <el-table-column label="提问人" width="120">
          <template #default="{ row }">{{ row.username || '—' }}</template>
        </el-table-column>
        <el-table-column label="结果" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="row.answered ? 'success' : 'danger'" size="small">
              {{ row.answered ? '已答' : '未答上' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="命中片段" width="100" align="center">
          <template #default="{ row }">{{ row.hit_count }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="100" align="right">
          <template #default="{ row }">{{ row.latency_ms ? (row.latency_ms / 1000).toFixed(1) + 's' : '—' }}</template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有问答记录" :image-size="80" />
        </template>
      </el-table>

      <div class="pager">
        <el-button :disabled="offset === 0 || loading" @click="prev">上一页</el-button>
        <span class="pager-info">第 {{ offset / pageSize + 1 }} 页</span>
        <el-button :disabled="logs.length < pageSize || loading" @click="next">下一页</el-button>
      </div>
    </el-card>

    <!-- 详情：检索命中明细，用来诊断"为什么这题没答好" -->
    <el-drawer v-model="drawer.visible" title="问答详情" size="520px">
      <div v-if="drawer.detail" class="detail">
        <div class="d-row">
          <span class="d-label">问题</span>
          <span class="d-val">{{ drawer.detail.question }}</span>
        </div>
        <div class="d-row">
          <span class="d-label">提问人</span>
          <span class="d-val">{{ drawer.detail.username || '—' }}</span>
        </div>
        <div class="d-row">
          <span class="d-label">是否答上</span>
          <span class="d-val">{{ drawer.detail.answered ? '答上了' : '没答上（知识库里没有对应内容）' }}</span>
        </div>
        <div class="d-row">
          <span class="d-label">耗时</span>
          <span class="d-val">{{ drawer.detail.latency_ms ? (drawer.detail.latency_ms / 1000).toFixed(1) + ' 秒' : '—' }}</span>
        </div>

        <div class="d-title">检索命中的片段（{{ (drawer.detail.retrieval || []).length }} 条）</div>
        <el-empty
          v-if="!drawer.detail.retrieval || !drawer.detail.retrieval.length"
          description="一条都没命中 —— 说明知识库里确实没有相关内容"
          :image-size="70"
        />
        <div v-for="(r, i) in drawer.detail.retrieval || []" :key="i" class="hit">
          <div class="hit-head">
            <span class="hit-doc">{{ r.doc_name || '未知文档' }}</span>
            <span class="hit-score" v-if="typeof r.score === 'number'">
              相关度 {{ (r.score * 100).toFixed(0) }}%
            </span>
          </div>
          <div class="hit-text">{{ r.text || '（无片段原文）' }}</div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
// 问答日志页：管理员专用。日志按组织隔离，看不到别家公司的提问。
import { onMounted, ref } from 'vue'
import {
  getQueryLogDetail,
  getQueryLogStats,
  listQueryLogs,
  type QueryLogDetail,
  type QueryLogItem,
  type QueryLogStats,
} from '@/api/admin'

const logs = ref<QueryLogItem[]>([])
const stats = ref<QueryLogStats>({ total: 0, answered: 0, unanswered: 0, avg_latency_ms: 0 })
const loading = ref(false)
const keyword = ref('')
const filter = ref<'all' | 'answered' | 'unanswered'>('all')

const pageSize = 50
const offset = ref(0)

const drawer = ref({ visible: false, detail: null as QueryLogDetail | null })

async function reload() {
  offset.value = 0
  await load()
}

async function load() {
  loading.value = true
  try {
    const params: { answered?: boolean; keyword?: string; limit: number; offset: number } = {
      limit: pageSize,
      offset: offset.value,
    }
    if (filter.value !== 'all') params.answered = filter.value === 'answered'
    if (keyword.value.trim()) params.keyword = keyword.value.trim()

    const [rows, s] = await Promise.all([listQueryLogs(params), getQueryLogStats()])
    logs.value = rows
    stats.value = s
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

function next() {
  offset.value += pageSize
  load()
}

function prev() {
  offset.value = Math.max(0, offset.value - pageSize)
  load()
}

async function openDetail(row: QueryLogItem) {
  try {
    drawer.value.detail = await getQueryLogDetail(row.id)
    drawer.value.visible = true
  } catch {
    /* 已提示 */
  }
}

function fmtTime(s: string): string {
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

onMounted(load)
</script>

<style scoped>
.page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px;
  gap: 12px;
  box-sizing: border-box;
  overflow-y: auto;
}
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.head-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.head-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}
.tip {
  margin: 0;
}

/* 概览四张卡片 */
.stats {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.stat-card {
  flex: 1 1 130px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 10px;
  padding: 12px 16px;
}
.stat-card.ok {
  border-color: #b7dfc4;
  background: #f2faf4;
}
.stat-card.bad {
  border-color: #f3c8c1;
  background: #fdf3f1;
}
.stat-num {
  font-size: 22px;
  font-weight: 700;
  color: #303133;
}
.stat-label {
  font-size: 12.5px;
  color: #909399;
  margin-top: 2px;
}

.q {
  font-weight: 500;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 12px;
}
.pager-info {
  font-size: 13px;
  color: #909399;
}

/* 详情抽屉 */
.d-row {
  display: flex;
  gap: 10px;
  margin-bottom: 10px;
  font-size: 13.5px;
}
.d-label {
  flex: 0 0 68px;
  color: #909399;
}
.d-val {
  flex: 1;
  color: #303133;
  word-break: break-word;
}
.d-title {
  margin: 18px 0 10px;
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}
.hit {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
  background: #fafbfc;
}
.hit-head {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
  font-size: 12.5px;
}
.hit-doc {
  font-weight: 600;
  color: #409eff;
}
.hit-score {
  color: #909399;
}
.hit-text {
  font-size: 12.5px;
  line-height: 1.7;
  color: #606266;
  max-height: 160px;
  overflow-y: auto;
  white-space: pre-wrap;
}
</style>
