<template>
  <!--
    API 密钥弹窗：给企业"程序对程序"对接用的长期钥匙。

    为什么单独拆成组件：
      这段代码里有"明文只显示一次"这个关键且易错的状态（created），
      还带着复制、吊销、过期判断等逻辑。和"改组织名"混在一起时，
      很容易在别处误清掉 created，导致用户再也看不到刚生成的钥匙。
      ���出来之后，这个状态的生命周期被限制在一个文件内，好维护也好审查。
  -->
  <el-dialog v-model="visible" :title="`API 密钥：${orgName}`" width="780px">
    <el-alert type="info" :closable="false" show-icon class="key-tip">
      <template #title>
        这是给企业<b>系统对接</b>用的钥匙（不是给人登录用的）。企业在自家系统里带上它，
        就能调用我们的问答接口。钥匙<b>只属于这一家组织</b>，看不到别家的资料；
        不需要时点「吊销」即可立刻失效。
      </template>
    </el-alert>

    <!-- 刚创建出来的明文钥匙：只显示这一次 -->
    <div v-if="created" class="key-new">
      <div class="key-new-head">新钥匙已生成——请立刻复制保存，关掉这个窗口后就查不到了</div>
      <div class="code-box">
        <span class="code-text">{{ created.key }}</span>
        <el-button type="primary" plain size="small" @click="copyNewKey">复制</el-button>
      </div>
      <div class="key-example">
        对接方式：向 <code>POST /api/v1/open/chat</code> 发请求，请求头带上
        <code>Authorization: Bearer 你的Key</code>
      </div>
    </div>

    <div class="key-toolbar">
      <el-input
        v-model="newName"
        placeholder="给新钥匙起个名，如「客服系统」"
        style="width: 240px"
        maxlength="64"
      />
      <el-input
        v-model="expiresDays"
        placeholder="多少天后过期（留空=不过期）"
        style="width: 210px"
      />
      <el-button type="primary" :loading="creating" @click="doCreate">新建密钥</el-button>
    </div>

    <el-table :data="apiKeys" v-loading="loading" size="small" max-height="320">
      <el-table-column prop="name" label="名称" min-width="130">
        <template #default="{ row }">
          <span>{{ row.name || '未命名' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="钥匙前缀" width="130">
        <template #default="{ row }">
          <span class="code-text-sm">{{ row.key_prefix }}…</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.revoked" type="info" size="small">已吊销</el-tag>
          <el-tag v-else-if="isExpired(row.expires_at)" type="danger" size="small">已过期</el-tag>
          <el-tag v-else type="success" size="small">可用</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="调用次数" width="90" align="center">
        <template #default="{ row }">{{ row.use_count }}</template>
      </el-table-column>
      <el-table-column label="最后使用" width="150">
        <template #default="{ row }">{{ formatDateTime(row.last_used_at) || '从未使用' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="80" align="center">
        <template #default="{ row }">
          <el-button
            v-if="!row.revoked"
            link
            type="danger"
            size="small"
            @click="doRevoke(row)"
          >吊销</el-button>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="还没有密钥，用上面的输入框新建一个" :image-size="70" />
      </template>
    </el-table>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * API 密钥管理弹窗。
 *
 * 对外接口：
 *   - v-model（visible）：控制显示
 *   - orgId / orgName：操作哪家组织
 *
 * 需要注意的状态：created
 *   明文钥匙只由后端返回这一次，之后接口只给前缀。
 *   所以只要这个窗口没关，就必须一直把 created 留着让用户复制；
 *   一旦关闭就清空（避免下次打开看到上一次的旧钥匙，产生误导）。
 */
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createApiKey,
  listApiKeys,
  revokeApiKey,
  type ApiKeyCreated,
  type ApiKeyItem,
} from '@/api/apiKey'
import { formatDateTime } from '@/utils/datetime'

const props = defineProps<{
  modelValue: boolean
  orgId: number
  orgName: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
}>()

const visible = ref(props.modelValue)
watch(
  () => props.modelValue,
  (v) => {
    visible.value = v
  },
)
watch(visible, (v) => {
  emit('update:modelValue', v)
  // 关闭时清空明文钥匙：下次打开不应再看到上一次的
  if (!v) created.value = null
})

const newName = ref('')
const expiresDays = ref('')
const created = ref<ApiKeyCreated | null>(null)
const apiKeys = ref<ApiKeyItem[]>([])
const loading = ref(false)
const creating = ref(false)

/** 判断一把钥匙是否已过期（后端也会拦，这里只是给个直观的红色标记） */
function isExpired(expiresAt?: string | null): boolean {
  if (!expiresAt) return false
  const t = new Date(expiresAt).getTime()
  return !Number.isNaN(t) && t < Date.now()
}

async function load() {
  if (!props.orgId) return
  loading.value = true
  try {
    apiKeys.value = await listApiKeys(props.orgId)
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.orgId],
  ([v]) => {
    if (v) {
      created.value = null
      load()
    }
  },
)

/** 新建一把钥匙。成功后会亮出明文 —— 这是唯一一次能看到它的机会。 */
async function doCreate() {
  // 过期天数：留空 = 永不过期；填了必须是正整数
  let days: number | null = null
  const raw = (expiresDays.value ?? '').trim()
  if (raw) {
    const n = Number(raw)
    if (!Number.isFinite(n) || n < 1 || !Number.isInteger(n)) {
      return ElMessage.warning('过期天数请填正整数（例如 365），或留空表示不过期')
    }
    days = n
  }
  creating.value = true
  try {
    const got = await createApiKey({
      name: newName.value.trim(),
      expires_days: days,
      org_id: props.orgId,
    })
    created.value = got
    newName.value = ''
    expiresDays.value = ''
    await load()
  } catch {
    /* 已提示 */
  } finally {
    creating.value = false
  }
}

/** 吊销一把钥匙：先确认（不可撤销，吊销后对方系统会立刻调不通） */
async function doRevoke(row: ApiKeyItem) {
  const label = row.name || row.key_prefix
  try {
    await ElMessageBox.confirm(
      `吊销「${label}」后，对方系统用这把钥匙的调用会立刻失败（需要重新建一把并换到他们系统里）。确定吊销？`,
      '吊销 API 密钥',
      { type: 'warning', confirmButtonText: '确认吊销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await revokeApiKey(row.id)
    ElMessage.success('已吊销')
    await load()
  } catch {
    /* 已提示 */
  }
}

/** 复制刚生成的明文钥匙 */
async function copyNewKey() {
  const text = created.value?.key
  if (!text) return
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      ElMessage.success('已复制到剪贴板')
      return
    }
    throw new Error('no clipboard api')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选中上面的钥匙复制')
  }
}
</script>

<style scoped>
.key-tip {
  margin-bottom: 16px;
}

.key-new {
  background: #f0f9eb;
  border: 1px solid #e1f3d8;
  border-radius: 6px;
  padding: 12px 16px;
  margin-bottom: 16px;
}

.key-new-head {
  color: #67c23a;
  font-weight: 600;
  margin-bottom: 8px;
}

.code-box {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.code-text {
  font-family: monospace;
  font-size: 13px;
  word-break: break-all;
  flex: 1;
}

.code-text-sm {
  font-family: monospace;
  font-size: 12px;
}

.key-example {
  color: #909399;
  font-size: 12px;
}

.key-toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}

.muted {
  color: #c0c4cc;
}
</style>
