<template>
  <!--
    仪表盘：管理员登录后第一眼看到的"系统整体怎么样"。

    设计原则：
      · 不堆图表。只保留最有信息量的：今天多少提问、答上率、满意度、待处理项。
      · 数据为 None 时显示 "—" 而不是 0% —— 把"还没数据"显示成 0% 会误导人。
      · 趋势用纯 CSS 柱状图，不引入 ECharts（那会给项目加约 1MB 体积，
        只为画 7 根柱子不值得）。
  -->
  <div class="page" v-loading="loading">
    <div class="page-head">
      <h2 class="title">概览</h2>
      <el-button @click="reload" :loading="loading">刷新</el-button>
    </div>

    <!-- 待处理提醒：有问题时置顶，没问题时不占地方 -->
    <el-alert
      v-if="hasIssues"
      type="warning"
      :closable="false"
      show-icon
      class="issues"
    >
      <template #title>
        <span v-if="data.failed_docs > 0">
          有 <b>{{ data.failed_docs }}</b> 个文档解析失败；
        </span>
        <span v-if="data.processing_docs > 0">
          <b>{{ data.processing_docs }}</b> 个正在入库；
        </span>
        <span v-if="data.feedback_dislikes > 0">
          <b>{{ data.feedback_dislikes }}</b> 条用户反馈表示"答得不准"。
        </span>
        <span class="issues-tip">这些都可以在"问答日志 / 用户反馈 / 知识库管理"里逐项处理。</span>
      </template>
    </el-alert>

    <!-- 第一排：今天的关键数字 -->
    <div class="cards">
      <div class="card">
        <div class="card-num">{{ data.today_questions }}</div>
        <div class="card-label">今日提问</div>
        <div class="card-sub">近 {{ periodDays }} 天 {{ data.period_questions }} 次</div>
      </div>
      <div class="card">
        <div class="card-num" :class="rateClass(data.answer_rate)">
          {{ fmtRate(data.answer_rate) }}
        </div>
        <div class="card-label">答上率</div>
        <div class="card-sub">问题能从资料里找到答案的比例</div>
      </div>
      <div class="card">
        <div class="card-num" :class="rateClass(data.satisfaction_rate)">
          {{ fmtRate(data.satisfaction_rate) }}
        </div>
        <div class="card-label">满意率</div>
        <div class="card-sub">
          共 {{ data.feedback_total }} 条评价（赞 {{ data.feedback_likes }} / 踩
          {{ data.feedback_dislikes }}）
        </div>
      </div>
      <div class="card">
        <div class="card-num">{{ data.doc_count }}</div>
        <div class="card-label">已入库文档</div>
        <div class="card-sub">
          处理中 {{ data.processing_docs }} · 失败 {{ data.failed_docs }}
        </div>
      </div>
    </div>

    <!-- 第二排：趋势 + 最近活动 -->
    <div class="row2">
      <el-card shadow="never" class="trend-card">
        <div class="card-title">最近 {{ periodDays }} 天提问量</div>
        <div class="chart">
          <div
            v-for="p in data.trend"
            :key="p.date"
            class="bar-wrap"
            :title="`${p.date}：${p.count} 次`"
          >
            <div class="bar-value">{{ p.count || '' }}</div>
            <div class="bar" :style="{ height: barHeight(p.count) }" />
            <div class="bar-label">{{ p.date.slice(5) }}</div>
          </div>
        </div>
        <div v-if="!hasTrendData" class="chart-empty">
          最近 {{ periodDays }} 天还没有提问记录
        </div>
      </el-card>

      <el-card shadow="never" class="recent-card">
        <div class="card-title">最近提问</div>
        <div v-if="!data.recent.length" class="chart-empty">还没有提问记录</div>
        <div v-for="(r, i) in data.recent" :key="i" class="recent-item">
          <el-tag :type="r.answered ? 'success' : 'danger'" size="small" effect="plain">
            {{ r.answered ? '答上' : '没答上' }}
          </el-tag>
          <span class="recent-q">{{ r.question }}</span>
          <span class="recent-user">{{ r.username }}</span>
        </div>
      </el-card>
    </div>

    <!-- 资源概况 -->
    <el-card shadow="never" class="resources">
      <div class="card-title">资源概况</div>
      <div class="res-grid">
        <div class="res-item">
          <div class="res-num">{{ data.user_count }}</div>
          <div class="res-label">活跃成员</div>
        </div>
        <div class="res-item">
          <div class="res-num">{{ data.kb_count }}</div>
          <div class="res-label">知识库</div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
/**
 * 仪表盘页。
 *
 * 为什么放在侧边栏最上面：
 *   管理员登录后应该先看到"系统整体状况"，而不是直接跳进聊天。
 *   看到"答上率低 / 有失败文档 / 有用户抱怨"才有明确的下一步动作。
 */
import { computed, onMounted, ref } from 'vue'
import { getDashboard, type DashboardData } from '@/api/dashboard'

const periodDays = ref(7)
const loading = ref(false)
const data = ref<DashboardData>({
  user_count: 0,
  kb_count: 0,
  doc_count: 0,
  processing_docs: 0,
  failed_docs: 0,
  today_questions: 0,
  period_questions: 0,
  answer_rate: null,
  feedback_total: 0,
  feedback_likes: 0,
  feedback_dislikes: 0,
  satisfaction_rate: null,
  trend: [],
  recent: [],
})

const hasIssues = computed(
  () =>
    data.value.failed_docs > 0 ||
    data.value.processing_docs > 0 ||
    data.value.feedback_dislikes > 0,
)

const hasTrendData = computed(() => data.value.trend.some((p) => p.count > 0))

const maxCount = computed(() =>
  Math.max(1, ...data.value.trend.map((p) => p.count)),
)

/** 柱高按最大值等比缩放，最小留 4% 让"有数据但很小"也能看得见 */
function barHeight(count: number): string {
  if (!count) return '2px'
  return `${Math.max(8, Math.round((count / maxCount.value) * 100))}%`
}

/**
 * 比率显示。
 * None 表示"还没有数据" —— 显示 "—" 而不是 0%，
 * 否则会让人以为"系统表现极差"，其实是"还没人用过"。
 */
function fmtRate(v: number | null): string {
  return v === null ? '—' : `${v}%`
}

/** 比率着色：低于 60% 标红，低于 80% 标黄 */
function rateClass(v: number | null): string {
  if (v === null) return 'muted'
  if (v < 60) return 'bad'
  if (v < 80) return 'warn'
  return 'good'
}

async function reload() {
  loading.value = true
  try {
    data.value = await getDashboard(periodDays.value)
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
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

.title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.issues {
  margin-bottom: 16px;
}

.issues-tip {
  color: #909399;
  margin-left: 4px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 16px;
}

.card {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 10px;
  padding: 18px 20px;
}

.card-num {
  font-size: 30px;
  font-weight: 600;
  color: #303133;
  line-height: 1.1;
}

.card-num.good {
  color: #67c23a;
}
.card-num.warn {
  color: #e6a23c;
}
.card-num.bad {
  color: #f56c6c;
}
.card-num.muted {
  color: #c0c4cc;
}

.card-label {
  margin-top: 6px;
  font-size: 13px;
  color: #606266;
  font-weight: 500;
}

.card-sub {
  margin-top: 4px;
  font-size: 12px;
  color: #a8abb2;
}

.row2 {
  display: grid;
  grid-template-columns: 1.3fr 1fr;
  gap: 14px;
  margin-bottom: 16px;
}

.card-title {
  font-weight: 600;
  color: #303133;
  margin-bottom: 16px;
}

.chart {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  height: 180px;
  padding: 0 4px;
}

.bar-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
}

.bar-value {
  font-size: 11px;
  color: #909399;
  height: 16px;
}

.bar {
  width: 60%;
  background: linear-gradient(180deg, #79bbff, #409eff);
  border-radius: 4px 4px 0 0;
  transition: height 0.3s;
  min-height: 2px;
}

.bar-label {
  margin-top: 6px;
  font-size: 11px;
  color: #a8abb2;
}

.chart-empty {
  color: #a8abb2;
  font-size: 13px;
  padding: 20px 0;
  text-align: center;
}

.recent-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px dashed #f0f2f5;
  font-size: 13px;
}

.recent-item:last-child {
  border-bottom: none;
}

.recent-q {
  flex: 1;
  color: #606266;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-user {
  color: #a8abb2;
  font-size: 12px;
}

.res-grid {
  display: flex;
  gap: 48px;
}

.res-num {
  font-size: 24px;
  font-weight: 600;
  color: #303133;
}

.res-label {
  margin-top: 4px;
  font-size: 13px;
  color: #909399;
}
</style>
