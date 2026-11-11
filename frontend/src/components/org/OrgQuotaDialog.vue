<template>
  <!--
    额度设置弹窗：防止某个客户把 AI 成本刷爆，也是收费依据。

    为什么单独拆成组件：
      这段逻辑原本混在 815 行的组织管理页里，和"改组织名""看 API 密钥"
      搅在一起。改额度时要在一大堆无关代码里找，容易改错地方。
      拆出来后：谁负责额度，就看这一个文件。
  -->
  <el-dialog v-model="visible" :title="`额度设置：${orgName}`" width="560px">
    <el-alert type="info" :closable="false" show-icon class="quota-tip">
      <template #title>
        留空 = <b>不限量</b>；填 0 = <b>一次都不允许</b>（两者意思相反，别填错）。
        超出上限后，该组织的问答会被拦下并提示用户。
      </template>
    </el-alert>

    <div class="usage-box" v-if="usage">
      <div class="usage-row">
        <span class="usage-label">今日用量</span>
        <span class="usage-val">
          {{ usage.daily.questions }} 次提问
          <template v-if="usage.daily.question_limit !== null">
            （上限 {{ usage.daily.question_limit }}）
          </template>
        </span>
      </div>
      <div class="usage-row">
        <span class="usage-label">本月用量</span>
        <span class="usage-val">
          {{ usage.monthly.questions }} 次提问 ·
          {{ usage.monthly.tokens_in + usage.monthly.tokens_out }} token
          <template v-if="usage.monthly.question_limit !== null">
            （上限 {{ usage.monthly.question_limit }}）
          </template>
        </span>
      </div>
    </div>

    <el-form label-width="120px" v-loading="loading">
      <el-form-item label="每日提问上限">
        <el-input v-model="daily" placeholder="留空 = 不限" clearable style="width: 200px" />
        <span class="unit">次/天</span>
      </el-form-item>
      <el-form-item label="每月提问上限">
        <el-input v-model="monthly" placeholder="留空 = 不限" clearable style="width: 200px" />
        <span class="unit">次/月</span>
      </el-form-item>
      <el-form-item label="每日 token 上限">
        <el-input v-model="dailyToken" placeholder="留空 = 不限" clearable style="width: 200px" />
        <span class="unit">token</span>
      </el-form-item>
      <el-form-item label="服务状态">
        <el-switch
          v-model="enabled"
          active-text="在用"
          inactive-text="暂停服务"
          inline-prompt
        />
        <span class="unit">暂停后即使额度没用完也无法提问（欠费/到期时用）</span>
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="note" maxlength="255" placeholder="为什么是这几个数字，方便日后回看" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * 额度设置弹窗。
 *
 * 对外接口：
 *   - v-model（visible）：控制显示
 *   - orgId / orgName：要设置哪家组织
 *   - @saved：保存成功后通知父组件刷新列表
 *
 * 设计说明：组件自己负责"打开时拉取最新配置与用量"，
 * 而不是让父组件传进来 —— 因为额度数字必须是最新的，
 * 传进来的可能是列表页缓存的旧值。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getOrgQuota, getOrgUsage, updateOrgQuota, type UsageSummary } from '@/api/quota'

const props = defineProps<{
  modelValue: boolean
  orgId: number
  orgName: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved'): void
}>()

const visible = ref(props.modelValue)
watch(
  () => props.modelValue,
  (v) => {
    visible.value = v
  },
)
watch(visible, (v) => emit('update:modelValue', v))

// 上限用字符串存（空串 = 不限），保存时再转成数字或 null
const daily = ref('')
const monthly = ref('')
const dailyToken = ref('')
const enabled = ref(true)
const note = ref('')
const usage = ref<UsageSummary | null>(null)
const loading = ref(false)
const saving = ref(false)

function close() {
  visible.value = false
}

/** 空串 → null（不限）；非法输入 → 抛错提示 */
function parseLimit(raw: string, label: string): number | null {
  const s = (raw ?? '').trim()
  if (!s) return null
  const n = Number(s)
  if (!Number.isFinite(n) || n < 0 || !Number.isInteger(n)) {
    throw new Error(`${label}请填写非负整数，或留空表示不限`)
  }
  return n
}

/** 打开时拉当前配置与用量，避免"看到的数字是旧的" */
async function load() {
  if (!props.orgId) return
  loading.value = true
  try {
    const [cfg, u] = await Promise.all([getOrgQuota(props.orgId), getOrgUsage(props.orgId)])
    daily.value = cfg.daily_question_limit === null ? '' : String(cfg.daily_question_limit)
    monthly.value = cfg.monthly_question_limit === null ? '' : String(cfg.monthly_question_limit)
    dailyToken.value = cfg.daily_token_limit === null ? '' : String(cfg.daily_token_limit)
    enabled.value = cfg.is_service_enabled
    note.value = cfg.note || ''
    usage.value = u
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.orgId],
  ([v]) => {
    if (v) load()
  },
)

async function save() {
  let d: number | null
  let m: number | null
  let t: number | null
  try {
    d = parseLimit(daily.value, '每日提问上限')
    m = parseLimit(monthly.value, '每月提问上限')
    t = parseLimit(dailyToken.value, '每日 token 上限')
  } catch (e: any) {
    return ElMessage.warning(e.message)
  }
  saving.value = true
  try {
    await updateOrgQuota(props.orgId, {
      daily_question_limit: d,
      monthly_question_limit: m,
      daily_token_limit: t,
      is_service_enabled: enabled.value,
      note: note.value || '',
    })
    ElMessage.success('额度已保存')
    visible.value = false
    emit('saved')
  } catch {
    /* 已提示 */
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.quota-tip {
  margin-bottom: 16px;
}

.usage-box {
  background: var(--bg-hover);
  border-radius: 6px;
  padding: 12px 16px;
  margin-bottom: 16px;
}

.usage-row {
  display: flex;
  justify-content: space-between;
  line-height: 1.9;
}

.usage-label {
  color: var(--text-3);
}

.usage-val {
  color: var(--text-1);
  font-weight: 500;
}

.unit {
  margin-left: 8px;
  color: var(--text-4);
  font-size: 12px;
}
</style>
