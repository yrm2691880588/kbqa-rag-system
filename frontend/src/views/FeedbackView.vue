<template>
  <!--
    反馈管理：把用户点的"踩"变成可行动的改进清单。

    为什么需要这个页面：
      用户点踩后数据虽然落库了，但没人看得见 —— 等于白收集。
      管理员需要能回答三个问题：
        1) 整体满意度如何？
        2) 大家主要在抱怨什么？（按原因分类）
        3) 具体哪几条答得不好？（能点开看原问题和回答）
  -->
  <div class="page">
    <div class="page-head">
      <div class="head-left">
        <h2 class="title">用户反馈</h2>
      </div>
      <div class="head-right">
        <el-select v-model="filter" style="width: 150px" @change="reload">
          <el-option label="全部反馈" value="all" />
          <el-option label="只看不满意" value="bad" />
          <el-option label="只看满意" value="good" />
        </el-select>
        <el-select
          v-model="reasonFilter"
          placeholder="按原因筛选"
          clearable
          style="width: 180px"
          @change="reload"
        >
          <el-option v-for="r in reasonOptions" :key="r.value" :label="r.label" :value="r.value" />
        </el-select>
        <el-button @click="reload">刷新</el-button>
      </div>
    </div>

    <!-- 概览：管理员第一个想知道的是"用户总体满意吗" -->
    <div class="stats">
      <div class="stat-card">
        <div class="stat-num">{{ stats.total }}</div>
        <div class="stat-label">总评价数</div>
      </div>
      <div class="stat-card ok">
        <div class="stat-num">{{ stats.likes }}</div>
        <div class="stat-label">满意</div>
      </div>
      <div class="stat-card bad">
        <div class="stat-num">{{ stats.dislikes }}</div>
        <div class="stat-label">不满意</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">
          {{ stats.satisfaction_rate === null ? '—' : stats.satisfaction_rate + '%' }}
        </div>
        <div class="stat-label">满意率</div>
      </div>
    </div>

    <!-- 原因分布：直接告诉管理员"该往哪改" -->
    <el-card v-if="stats.reason_counts.length" shadow="never" class="reasons-card">
      <div class="reasons-title">不满意原因分布</div>
      <div class="reasons">
        <div v-for="r in stats.reason_counts" :key="r.reason" class="reason-row">
          <span class="reason-label">{{ r.label }}</span>
          <div class="reason-bar-wrap">
            <div class="reason-bar" :style="{ width: barWidth(r.count) }" />
          </div>
          <span class="reason-count">{{ r.count }}</span>
        </div>
      </div>
    </el-card>

    <el-alert
      v-if="stats.total === 0"
      type="info"
      :closable="false"
      show-icon
      class="tip"
    >
      <template #title>
        还没有用户反馈。用户在对回答点「踩」时会选择原因，数据会出现在这里。
      </template>
    </el-alert>

    <el-card shadow="never">
      <el-table :data="items" v-loading="loading" size="default" @row-click="openDetail">
        <el-table-column label="用户问题" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="q">{{ row.question || '（会话未命名）' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="评价" width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.feedback === 1" type="success" size="small">满意</el-tag>
            <el-tag v-else-if="row.feedback === -1" type="danger" size="small">不满意</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="原因" width="170">
          <template #default="{ row }">
            <el-tag v-if="row.reason_label" type="warning" size="small" effect="plain">
              {{ row.reason_label }}
            </el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="补充说明" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ muted: !row.note }">{{ row.note || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="反馈人" width="110">
          <template #default="{ row }">{{ row.username }}</template>
        </el-table-column>
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ formatDateTime(row.feedback_at) }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有反馈" :image-size="70" />
        </template>
      </el-table>
    </el-card>

    <!-- 详情抽屉：看完整问答，判断到底哪里答错了 -->
    <el-drawer v-model="drawer.visible" title="反馈详情" size="520px">
      <div v-if="drawer.item" class="detail">
        <div class="detail-label">用户问题</div>
        <div class="detail-box">{{ drawer.item.question || '（会话未命名）' }}</div>

        <div class="detail-label">AI 回答</div>
        <div class="detail-box answer">{{ drawer.item.answer }}</div>

        <div class="detail-label">用户反馈</div>
        <div class="detail-box">
          <el-tag v-if="drawer.item.feedback === -1" type="danger" size="small">不满意</el-tag>
          <el-tag v-else type="success" size="small">满意</el-tag>
          <span v-if="drawer.item.reason_label" class="ml">原因：{{ drawer.item.reason_label }}</span>
          <div v-if="drawer.item.note" class="note">补充说明：{{ drawer.item.note }}</div>
        </div>

        <div v-if="drawer.item.citations?.length" class="detail-label">引用的资料</div>
        <div v-for="(c, i) in drawer.item.citations" :key="i" class="cite-box">
          <div class="cite-head">{{ c.index }}. {{ c.doc_name }}</div>
          <div class="cite-text">{{ c.text }}</div>
        </div>

        <div class="detail-label">改进建议</div>
        <div class="detail-box tip-box">
          {{ improveTip(drawer.item.reason) }}
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
/**
 * 反馈管理页（管理员）。
 *
 * 数据来源：GET /api/v1/feedbacks（列表）、/feedbacks/stats（统计）、
 *          /feedbacks/{id}（详情）。
 * 后端已按组织收敛，跨组织数据看不到。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { formatDateTime } from '@/utils/datetime'
import {
  getFeedbackDetail,
  getFeedbackStats,
  listFeedbacks,
  type FeedbackItem,
  type FeedbackStats,
} from '@/api/feedback'

const reasonOptions = [
  { value: 'inaccurate', label: '回答不准确' },
  { value: 'no_reference', label: '没有引用到相关资料' },
  { value: 'irrelevant', label: '答非所问' },
  { value: 'incomplete', label: '回答不完整' },
  { value: 'wrong_citation', label: '引用错了文档' },
  { value: 'outdated', label: '资料过时' },
  { value: 'other', label: '其他' },
]

const filter = ref<'all' | 'good' | 'bad'>('all')
const reasonFilter = ref('')
const items = ref<FeedbackItem[]>([])
const loading = ref(false)
const stats = ref<FeedbackStats>({
  total: 0,
  likes: 0,
  dislikes: 0,
  satisfaction_rate: null,
  reason_counts: [],
})

const drawer = reactive({
  visible: false,
  item: null as FeedbackItem | null,
})

/** 原因条的相对宽度（相对最多的那项，保证视觉上能比较） */
const maxReasonCount = computed(() =>
  Math.max(1, ...stats.value.reason_counts.map((r) => r.count)),
)
function barWidth(count: number): string {
  return `${Math.round((count / maxReasonCount.value) * 100)}%`
}

/** 针对每种原因给出具体的改进方向，而不是笼统的"优化一下" */
function improveTip(reason?: string | null): string {
  switch (reason) {
    case 'inaccurate':
      return '回答内容与资料不符。建议核对知识库里的原始文档是否写错，或该问题需要更新的版本。'
    case 'no_reference':
      return '没有检索到相关资料。通常意味着知识库缺少这份资料，或资料里的表述与用户问法差异太大 —— 可以补充文档，或把常见问法写进资料。'
    case 'irrelevant':
      return '检索命中了不相关的片段。可检查该文档的切块是否过碎/过整，或调整检索阈值让噪声更少。'
    case 'incomplete':
      return '回答只覆盖了一部分。可能资料本身信息不全，或检索只取到了片段中的一条 —— 可以增加每个文档的取片段数。'
    case 'wrong_citation':
      return '引用的文档不对。检查该问题在向量库里的命中情况，确认是否被相似但不相关的内容干扰。'
    case 'outdated':
      return '资料已过时。更新知识库中的该文档即可。'
    default:
      return '结合上面显示的原始问答，判断是资料问题还是检索问题。'
  }
}

async function reload() {
  loading.value = true
  try {
    const feedback = filter.value === 'bad' ? -1 : filter.value === 'good' ? 1 : undefined
    const [list, s] = await Promise.all([
      listFeedbacks({
        feedback,
        reason: reasonFilter.value || undefined,
        limit: 100,
      }),
      getFeedbackStats(),
    ])
    items.value = list
    stats.value = s
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
  }
}

async function openDetail(row: FeedbackItem) {
  drawer.item = row
  drawer.visible = true
  // 再拉一次详情，确保数据是最新的（列表可能是缓存页）
  try {
    drawer.item = await getFeedbackDetail(row.message_id)
  } catch {
    /* 用列表里的数据兜底 */
  }
}

onMounted(reload)
</script>

<style scoped>
.page {
  padding: 4px;
}

.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.head-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}

.stat-card {
  background: #fff;
  border-radius: 8px;
  padding: 16px 20px;
  border: 1px solid #ebeef5;
}

.stat-num {
  font-size: 26px;
  font-weight: 600;
  color: #303133;
  line-height: 1.2;
}

.stat-card.ok .stat-num {
  color: #67c23a;
}

.stat-card.bad .stat-num {
  color: #f56c6c;
}

.stat-label {
  margin-top: 4px;
  color: #909399;
  font-size: 13px;
}

.reasons-card {
  margin-bottom: 16px;
}

.reasons-title {
  font-weight: 600;
  margin-bottom: 12px;
  color: #303133;
}

.reasons {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.reason-row {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
}

.reason-label {
  width: 160px;
  color: #606266;
  flex-shrink: 0;
}

.reason-bar-wrap {
  flex: 1;
  background: #f5f7fa;
  border-radius: 4px;
  height: 16px;
  overflow: hidden;
}

.reason-bar {
  height: 100%;
  background: #f56c6c;
  border-radius: 4px;
  transition: width 0.3s;
}

.reason-count {
  width: 40px;
  text-align: right;
  color: #909399;
}

.tip {
  margin-bottom: 16px;
}

.q {
  font-weight: 500;
}

.muted {
  color: #c0c4cc;
}

.detail {
  font-size: 14px;
}

.detail-label {
  font-weight: 600;
  color: #303133;
  margin: 16px 0 8px;
}

.detail-label:first-child {
  margin-top: 0;
}

.detail-box {
  background: #f5f7fa;
  border-radius: 6px;
  padding: 12px 14px;
  color: #606266;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.detail-box.answer {
  max-height: 240px;
  overflow-y: auto;
}

.ml {
  margin-left: 10px;
  color: #606266;
}

.note {
  margin-top: 8px;
  color: #e6a23c;
}

.cite-box {
  background: #fafafa;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 8px;
}

.cite-head {
  font-weight: 500;
  color: #409eff;
  font-size: 13px;
  margin-bottom: 6px;
}

.cite-text {
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
  max-height: 100px;
  overflow-y: auto;
}

.tip-box {
  background: #f0f9eb;
  color: #529b2e;
}
</style>
