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
  padding: var(--space-5) var(--space-6);
  max-width: 1500px;
}

.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-5);
  flex-wrap: wrap;
  gap: var(--space-3);
}

.title {
  margin: 0;
  font-size: var(--font-xl);
  font-weight: 600;
  color: var(--text-1);
  letter-spacing: -0.02em;
}

.issues {
  margin-bottom: var(--space-4);
  border-radius: var(--radius-md);
}

.issues-tip {
  color: var(--text-4);
  margin-left: var(--space-1);
}

/* ---------- 指标卡 ---------- */
.cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.card {
  background: var(--bg-card);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  padding: var(--space-5);
  transition: box-shadow var(--transition-base), transform var(--transition-fast);
  position: relative;
  overflow: hidden;
}

.card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

/* 顶部一条彩色标识条：让四个卡片不用读文字就能区分 */
.card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--brand-500);
  opacity: 0.9;
}
.card:nth-child(2)::before { background: var(--success); }
.card:nth-child(3)::before { background: var(--warning); }
.card:nth-child(4)::before { background: var(--brand-300); }

.card-num {
  font-size: var(--font-2xl);
  font-weight: 600;
  color: var(--text-1);
  line-height: 1.15;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}

.card-num.good { color: var(--success); }
.card-num.warn { color: var(--warning); }
.card-num.bad { color: var(--danger); }
.card-num.muted { color: var(--text-5); }

.card-label {
  margin-top: var(--space-2);
  font-size: var(--font-sm);
  color: var(--text-3);
  font-weight: 500;
}

.card-sub {
  margin-top: var(--space-1);
  font-size: var(--font-xs);
  color: var(--text-4);
}

/* ---------- 第二排 ---------- */
.row2 {
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.card-title {
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: var(--space-4);
  font-size: var(--font-lg);
}

/* ---------- 趋势图 ---------- */
.chart {
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
  height: 190px;
  padding: 0 var(--space-1);
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
  font-size: var(--font-xs);
  color: var(--text-4);
  height: 18px;
  font-variant-numeric: tabular-nums;
}

.bar {
  width: 62%;
  /* 有数据的柱子用品牌渐变，没数据的（高度 2px）也能看出是"这天为 0" */
  background: linear-gradient(180deg, var(--brand-300), var(--brand-500));
  border-radius: var(--radius-sm) var(--radius-sm) 2px 2px;
  transition: height var(--transition-base);
  min-height: 2px;
}

.bar-wrap:hover .bar {
  background: linear-gradient(180deg, var(--brand-400), var(--brand-600));
}

.bar-label {
  margin-top: var(--space-2);
  font-size: var(--font-xs);
  color: var(--text-4);
}

.chart-empty {
  color: var(--text-4);
  font-size: var(--font-sm);
  padding: var(--space-5) 0;
  text-align: center;
}

/* ---------- 最近活动 ---------- */
.recent-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 10px 0;
  border-bottom: 1px solid var(--border-2);
  font-size: var(--font-sm);
}

.recent-item:last-child {
  border-bottom: none;
}

.recent-q {
  flex: 1;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-user {
  color: var(--text-4);
  font-size: var(--font-xs);
}

/* ---------- 资源概况 ---------- */
.resources :deep(.el-card__body) {
  padding: var(--space-5);
}

.res-grid {
  display: flex;
  gap: var(--space-8);
}

.res-item {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}

.res-num {
  font-size: var(--font-xl);
  font-weight: 600;
  color: var(--text-1);
  font-variant-numeric: tabular-nums;
}

.res-label {
  font-size: var(--font-sm);
  color: var(--text-4);
}

/* ---------- 窄屏：卡片两列 ---------- */
@media (max-width: 1100px) {
  .cards {
    grid-template-columns: repeat(2, 1fr);
  }
  .row2 {
    grid-template-columns: 1fr;
  }
}
</style>
