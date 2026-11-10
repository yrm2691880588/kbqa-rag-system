<template>
  <!-- 组织管理页：平台超管专用。这里的"组织"= 一家客户公司。 -->
  <div class="org-page">
    <div class="page-head">
      <div class="head-left">
        <h2 class="title">组织管理</h2>
        <el-tag type="info" effect="plain" size="small">共 {{ orgs.length }} 家</el-tag>
      </div>
      <el-button type="primary" @click="openCreate">
        <el-icon style="margin-right: 4px"><Plus /></el-icon>新建组织
      </el-button>
    </div>

    <el-alert type="info" :closable="false" show-icon class="tip">
      <template #title>
        每家「组织」= 一家客户。各组织的知识库与账号互相隔离，谁都看不到别家的资料。
        邀请码是让新同事加入某个组织的凭证，请只发给自己人。
      </template>
    </el-alert>

    <el-card shadow="never">
      <el-table :data="orgs" v-loading="loading" size="default">
        <el-table-column label="组织名称" min-width="180">
          <template #default="{ row }">
            <span class="org-name">{{ row.name }}</span>
            <el-tag v-if="row.is_active === false" type="info" size="small" style="margin-left: 6px">已停用</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ muted: !row.description }">{{ row.description || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="成员" width="90" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openMembers(row)">{{ row.member_count }} 人</el-button>
          </template>
        </el-table-column>
        <el-table-column label="知识库" width="90" align="center">
          <template #default="{ row }">{{ row.kb_count }} 个</template>
        </el-table-column>
        <el-table-column label="本月用量" width="150" align="center">
          <template #default="{ row }">
            <span :class="usageClass(row)">
              {{ row.monthly_questions }}
              <template v-if="row.monthly_question_limit !== null && row.monthly_question_limit !== undefined">
                / {{ row.monthly_question_limit }}
              </template>
              <template v-else> / 不限</template>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="服务" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="row.is_service_enabled ? 'success' : 'danger'" size="small">
              {{ row.is_service_enabled ? '在用' : '已停' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="360" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="success" size="small" @click="openQuota(row)">额度</el-button>
            <el-button link type="primary" size="small" @click="openApiKeys(row)">密钥</el-button>
            <el-button link type="warning" size="small" @click="doRotate(row)">重置邀请码</el-button>
            <el-button link :type="row.is_active ? 'info' : 'success'" size="small" @click="toggleActive(row)">
              {{ row.is_active ? '停用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有组织，点右上角新建" :image-size="80" />
        </template>
      </el-table>
    </el-card>

    <!-- 新建 / 编辑组织 -->
    <el-dialog v-model="editDialog.visible" :title="editDialog.isEdit ? '编辑组织' : '新建组织'" width="460px">
      <el-form label-width="80px">
        <el-form-item label="组织名称" required>
          <el-input v-model="editDialog.name" maxlength="100" placeholder="如：某某科技有限公司" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="editDialog.description"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="这家客户的情况，方便以后识别"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveOrg">
          {{ editDialog.isEdit ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 邀请码展示：创建成功后 / 重置后各出现一次 -->
    <el-dialog v-model="codeDialog.visible" title="组织邀请码" width="520px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon class="code-alert">
        <template #title>
          请立刻复制并保存。这个码是让同事加入组织的凭证，页面关掉后就查不到了
          （需要时只能"重置邀请码"，重置后旧码立即失效）。
        </template>
      </el-alert>
      <div class="code-box">
        <span class="code-text">{{ codeDialog.code }}</span>
        <el-button type="primary" plain size="small" @click="copyCode">复制</el-button>
      </div>
      <div class="code-org">所属组织：{{ codeDialog.orgName }}</div>
      <template #footer>
        <el-button type="primary" @click="codeDialog.visible = false">我已保存</el-button>
      </template>
    </el-dialog>

    <!-- 额度设置（P1-B）：防止某个客户把 AI 成本刷爆，也是收费依据 -->
    <el-dialog v-model="quotaDialog.visible" :title="`额度设置：${quotaDialog.orgName}`" width="560px">
      <el-alert type="info" :closable="false" show-icon class="quota-tip">
        <template #title>
          留空 = <b>不限量</b>；填 0 = <b>一次都不允许</b>（两者意思相反，别填错）。
          超出上限后，该组织的问答会被拦下并提示用户。
        </template>
      </el-alert>

      <div class="usage-box" v-if="quotaDialog.usage">
        <div class="usage-row">
          <span class="usage-label">今日用量</span>
          <span class="usage-val">
            {{ quotaDialog.usage.daily.questions }} 次提问
            <template v-if="quotaDialog.usage.daily.question_limit !== null">
              （上限 {{ quotaDialog.usage.daily.question_limit }}）
            </template>
          </span>
        </div>
        <div class="usage-row">
          <span class="usage-label">本月用量</span>
          <span class="usage-val">
            {{ quotaDialog.usage.monthly.questions }} 次提问 ·
            {{ quotaDialog.usage.monthly.tokens_in + quotaDialog.usage.monthly.tokens_out }} token
            <template v-if="quotaDialog.usage.monthly.question_limit !== null">
              （上限 {{ quotaDialog.usage.monthly.question_limit }}）
            </template>
          </span>
        </div>
      </div>

      <el-form label-width="120px" v-loading="quotaLoading">
        <el-form-item label="每日提问上限">
          <el-input v-model="quotaDialog.daily" placeholder="留空 = 不限" clearable style="width: 200px" />
          <span class="unit">次/天</span>
        </el-form-item>
        <el-form-item label="每月提问上限">
          <el-input v-model="quotaDialog.monthly" placeholder="留空 = 不限" clearable style="width: 200px" />
          <span class="unit">次/月</span>
        </el-form-item>
        <el-form-item label="每日 token 上限">
          <el-input v-model="quotaDialog.dailyToken" placeholder="留空 = 不限" clearable style="width: 200px" />
          <span class="unit">token</span>
        </el-form-item>
        <el-form-item label="服务状态">
          <el-switch
            v-model="quotaDialog.enabled"
            active-text="在用"
            inactive-text="暂停服务"
            inline-prompt
          />
          <span class="unit">暂停后即使额度没用完也无法提问（欠费/到期时用）</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="quotaDialog.note" maxlength="255" placeholder="为什么是这几个数字，方便日后回看" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="quotaDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="quotaSaving" @click="saveQuota">保存</el-button>
      </template>
    </el-dialog>

    <!-- API 密钥（P1-G）：给企业"程序对程序"对接用的长期钥匙 -->
    <el-dialog v-model="keyDialog.visible" :title="`API 密钥：${keyDialog.orgName}`" width="780px">
      <el-alert type="info" :closable="false" show-icon class="key-tip">
        <template #title>
          这是给企业<b>系统对接</b>用的钥匙（不是给人登录用的）。企业在自家系统里带上它，
          就能调用我们的问答接口。钥匙<b>只属于这一家组织</b>，看不到别家的资料；
          不需要时点「吊销」即可立刻失效。
        </template>
      </el-alert>

      <!-- 刚创建出来的明文钥匙：只显示这一次 -->
      <div v-if="keyDialog.created" class="key-new">
        <div class="key-new-head">新钥匙已生成——请立刻复制保存，关掉这个窗口后就查不到了</div>
        <div class="code-box">
          <span class="code-text">{{ keyDialog.created.key }}</span>
          <el-button type="primary" plain size="small" @click="copyNewKey">复制</el-button>
        </div>
        <div class="key-example">
          对接方式：向 <code>POST /api/v1/open/chat</code> 发请求，请求头带上
          <code>Authorization: Bearer 你的Key</code>
        </div>
      </div>

      <div class="key-toolbar">
        <el-input
          v-model="keyDialog.newName"
          placeholder="给新钥匙起个名，如「客服系统」"
          style="width: 240px"
          maxlength="64"
        />
        <el-input
          v-model="keyDialog.expiresDays"
          placeholder="多少天后过期（留空=不过期）"
          style="width: 210px"
        />
        <el-button type="primary" :loading="keyCreating" @click="doCreateApiKey">新建密钥</el-button>
      </div>

      <el-table :data="apiKeys" v-loading="keyLoading" size="small" max-height="320">
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
          <template #default="{ row }">{{ fmtTime(row.last_used_at) || '从未使用' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80" align="center">
          <template #default="{ row }">
            <el-button
              v-if="!row.revoked"
              link
              type="danger"
              size="small"
              @click="doRevokeApiKey(row)"
            >吊销</el-button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有密钥，用上面的输入框新建一个" :image-size="70" />
        </template>
      </el-table>
    </el-dialog>

    <!-- 成员列表 -->
    <el-dialog v-model="memberDialog.visible" :title="`成员：${memberDialog.orgName}`" width="720px">
      <el-table :data="members" v-loading="memberLoading" size="small" max-height="420">
        <el-table-column prop="username" label="登录名" min-width="120" />
        <el-table-column prop="nickname" label="昵称" min-width="120" />
        <el-table-column label="角色" width="110" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.is_superadmin" type="danger" size="small">平台超管</el-tag>
            <el-tag v-else-if="row.role === 'admin'" type="warning" size="small">组织管理员</el-tag>
            <el-tag v-else type="info" size="small">成员</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="手机号" width="130">
          <template #default="{ row }">{{ row.phone || '未绑定' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '正常' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近登录" width="160">
          <template #default="{ row }">{{ fmtTime(row.last_login_at) || '从未登录' }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="该组织还没有成员" :image-size="70" />
        </template>
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
// 组织管理页（P1-A）：只有平台超管能进（路由守卫 + 后端接口双重校验）
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import {
  createOrg,
  listMembers,
  listOrgs,
  rotateInvite,
  updateOrg,
  type OrgItem,
  type OrgMember,
} from '@/api/org'
import { getOrgQuota, getOrgUsage, updateOrgQuota, type UsageSummary } from '@/api/quota'
import { createApiKey, listApiKeys, revokeApiKey, type ApiKeyCreated, type ApiKeyItem } from '@/api/apiKey'

const orgs = ref<OrgItem[]>([])
const loading = ref(false)

const editDialog = ref({ visible: false, isEdit: false, id: 0, name: '', description: '' })
const saving = ref(false)

// 额度弹窗：上限用字符串存（空串=不限），保存时再转成数字或 null
const quotaDialog = ref({
  visible: false,
  orgId: 0,
  orgName: '',
  daily: '',
  monthly: '',
  dailyToken: '',
  enabled: true,
  note: '',
  usage: null as UsageSummary | null,
})
const quotaLoading = ref(false)
const quotaSaving = ref(false)

// 邀请码弹窗：创建后/重置后各展示一次
const codeDialog = ref({ visible: false, code: '', orgName: '' })

const memberDialog = ref({ visible: false, orgName: '' })
const members = ref<OrgMember[]>([])
const memberLoading = ref(false)

// API 密钥弹窗（P1-G）
// created 只在新钥匙刚生成时有值 —— 因为明文只返回这一次，关掉就再也拿不到了。
const keyDialog = ref({
  visible: false,
  orgId: 0,
  orgName: '',
  newName: '',
  expiresDays: '',
  created: null as ApiKeyCreated | null,
})
const apiKeys = ref<ApiKeyItem[]>([])
const keyLoading = ref(false)
const keyCreating = ref(false)

async function loadOrgs() {
  loading.value = true
  try {
    orgs.value = await listOrgs()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editDialog.value = { visible: true, isEdit: false, id: 0, name: '', description: '' }
}

function openEdit(row: OrgItem) {
  editDialog.value = {
    visible: true,
    isEdit: true,
    id: row.id,
    name: row.name,
    description: row.description ?? '',
  }
}

async function saveOrg() {
  const { isEdit, id, name, description } = editDialog.value
  if (!name.trim()) return ElMessage.warning('请填写组织名称')
  saving.value = true
  try {
    if (isEdit) {
      await updateOrg(id, { name: name.trim(), description: description.trim() })
      ElMessage.success('已保存')
      editDialog.value.visible = false
    } else {
      const created = await createOrg(name.trim(), description.trim() || undefined)
      editDialog.value.visible = false
      // 新建后立刻把邀请码亮出来（这是唯一一次能拿到它的机会）
      codeDialog.value = { visible: true, code: created.invite_code, orgName: created.name }
    }
    await loadOrgs()
  } catch {
    /* 拦截器已提示（如同名 409） */
  } finally {
    saving.value = false
  }
}

async function doRotate(row: OrgItem) {
  try {
    await ElMessageBox.confirm(
      `重置「${row.name}」的邀请码后，旧邀请码会立即失效，已经发出去的码将无法再用于注册。确定重置？`,
      '重置邀请码',
      { type: 'warning', confirmButtonText: '确认重置', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const got = await rotateInvite(row.id)
    codeDialog.value = { visible: true, code: got.invite_code, orgName: row.name }
    ElMessage.success('已重置，请把新邀请码发给要加入的同事')
  } catch {
    /* 已提示 */
  }
}

async function toggleActive(row: OrgItem) {
  const next = !row.is_active
  if (!next) {
    try {
      await ElMessageBox.confirm(
        `停用「${row.name}」后，该组织的邀请码将不能用于注册。已有成员仍可正常登录使用。确定停用？`,
        '停用组织',
        { type: 'warning', confirmButtonText: '确认停用', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  try {
    await updateOrg(row.id, { is_active: next })
    ElMessage.success(next ? '已启用' : '已停用')
    await loadOrgs()
  } catch {
    /* 已提示 */
  }
}

/** 本月用量颜色：超过上限 80% 标橙、超了标红，一眼看出哪家快用完 */
function usageClass(row: OrgItem): string {
  const used = row.monthly_questions ?? 0
  const limit = row.monthly_question_limit
  if (limit === null || limit === undefined) return 'usage-ok'
  if (used >= limit) return 'usage-full'
  if (limit > 0 && used / limit >= 0.8) return 'usage-warn'
  return 'usage-ok'
}

/** 打开额度弹窗：先拉当前配置与用量，避免"看到的数字是旧的" */
async function openQuota(row: OrgItem) {
  quotaDialog.value = {
    visible: true,
    orgId: row.id,
    orgName: row.name,
    daily: row.question_limit_per_day === null || row.question_limit_per_day === undefined
      ? '' : String(row.question_limit_per_day),
    monthly: row.monthly_question_limit === null || row.monthly_question_limit === undefined
      ? '' : String(row.monthly_question_limit),
    dailyToken: '',
    enabled: row.is_service_enabled !== false,
    note: '',
    usage: null,
  }
  quotaLoading.value = true
  try {
    const [cfg, usage] = await Promise.all([getOrgQuota(row.id), getOrgUsage(row.id)])
    quotaDialog.value.daily = cfg.daily_question_limit === null ? '' : String(cfg.daily_question_limit)
    quotaDialog.value.monthly = cfg.monthly_question_limit === null ? '' : String(cfg.monthly_question_limit)
    quotaDialog.value.dailyToken = cfg.daily_token_limit === null ? '' : String(cfg.daily_token_limit)
    quotaDialog.value.enabled = cfg.is_service_enabled
    quotaDialog.value.note = cfg.note || ''
    quotaDialog.value.usage = usage
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    quotaLoading.value = false
  }
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

async function saveQuota() {
  const d = quotaDialog.value
  let daily: number | null
  let monthly: number | null
  let dailyToken: number | null
  try {
    daily = parseLimit(d.daily, '每日提问上限')
    monthly = parseLimit(d.monthly, '每月提问上限')
    dailyToken = parseLimit(d.dailyToken, '每日 token 上限')
  } catch (e: any) {
    return ElMessage.warning(e.message)
  }
  quotaSaving.value = true
  try {
    await updateOrgQuota(d.orgId, {
      daily_question_limit: daily,
      monthly_question_limit: monthly,
      daily_token_limit: dailyToken,
      is_service_enabled: d.enabled,
      note: d.note || '',
    })
    ElMessage.success('额度已保存')
    d.visible = false
    await loadOrgs()
  } catch {
    /* 已提示 */
  } finally {
    quotaSaving.value = false
  }
}

/** 打开密钥弹窗：先拉这家组织已有哪些钥匙 */
async function openApiKeys(row: OrgItem) {
  keyDialog.value = {
    visible: true, orgId: row.id, orgName: row.name,
    newName: '', expiresDays: '', created: null,
  }
  apiKeys.value = []
  keyLoading.value = true
  try {
    apiKeys.value = await listApiKeys(row.id)
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    keyLoading.value = false
  }
}

/** 新建一把钥匙。成功后会亮出明文 —— 这是唯一一次能看到它的机会。 */
async function doCreateApiKey() {
  const d = keyDialog.value
  // 过期天数：留空 = 永不过期；填了必须是正整数
  let days: number | null = null
  const raw = (d.expiresDays ?? '').trim()
  if (raw) {
    const n = Number(raw)
    if (!Number.isFinite(n) || n < 1 || !Number.isInteger(n)) {
      return ElMessage.warning('过期天数请填正整数（例如 365），或留空表示不过期')
    }
    days = n
  }
  keyCreating.value = true
  try {
    const got = await createApiKey({
      name: d.newName.trim(),
      expires_days: days,
      org_id: d.orgId,
    })
    d.created = got
    d.newName = ''
    d.expiresDays = ''
    await refreshApiKeys()
  } catch {
    /* 已提示 */
  } finally {
    keyCreating.value = false
  }
}

async function refreshApiKeys() {
  try {
    apiKeys.value = await listApiKeys(keyDialog.value.orgId)
  } catch {
    /* 已提示 */
  }
}

/** 吊销一把钥匙：先确认（不可撤销，吊销后对方系统会立刻调不通） */
async function doRevokeApiKey(row: ApiKeyItem) {
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
    await refreshApiKeys()
  } catch {
    /* 已提示 */
  }
}

/** 复制刚生成的明文钥匙 */
async function copyNewKey() {
  const text = keyDialog.value.created?.key
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

/** 判断一把钥匙是否已过期（后端也会拦，这里只是给个直观的红色标记） */
function isExpired(expiresAt?: string | null): boolean {
  if (!expiresAt) return false
  const t = new Date(expiresAt).getTime()
  return !Number.isNaN(t) && t < Date.now()
}

async function openMembers(row: OrgItem) {
  memberDialog.value = { visible: true, orgName: row.name }
  members.value = []
  memberLoading.value = true
  try {
    members.value = await listMembers(row.id)
  } catch {
    /* 已提示 */
  } finally {
    memberLoading.value = false
  }
}

/** 复制邀请码到剪贴板（老浏览器不支持剪贴板 API 时降级提示手动复制） */
async function copyCode() {
  const text = codeDialog.value.code
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      ElMessage.success('已复制到剪贴板')
      return
    }
    throw new Error('no clipboard api')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选中上面的邀请码复制')
  }
}

function fmtTime(s?: string | null): string {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

onMounted(loadOrgs)
</script>

<style scoped>
.org-page {
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
.tip {
  margin: 0;
}
.org-name {
  font-weight: 600;
}
.muted {
  color: #c0c4cc;
}
.code-alert {
  margin-bottom: 14px;
}
.code-box {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px dashed #409eff;
  border-radius: 8px;
  background: #ecf5ff;
}
.code-text {
  flex: 1;
  font-family: Consolas, Monaco, monospace;
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  word-break: break-all;
  user-select: all;
}
.code-org {
  margin-top: 10px;
  font-size: 13px;
  color: #909399;
}

/* ---- 额度弹窗 ---- */
.quota-tip {
  margin-bottom: 14px;
}
.usage-box {
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 16px;
}
.usage-row {
  display: flex;
  gap: 12px;
  line-height: 1.9;
  font-size: 13px;
}
.usage-label {
  flex: 0 0 76px;
  color: #909399;
}
.usage-val {
  color: #303133;
}
.unit {
  margin-left: 10px;
  font-size: 12.5px;
  color: #909399;
}

/* ---- API 密钥弹窗（P1-G） ---- */
.key-tip {
  margin-bottom: 14px;
}
.key-new {
  border: 1px solid #a0cfff;
  background: #ecf5ff;
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}
.key-new-head {
  font-size: 13px;
  font-weight: 600;
  color: #1f6fd0;
  margin-bottom: 10px;
}
.key-example {
  margin-top: 10px;
  font-size: 12.5px;
  color: #606266;
  line-height: 1.7;
}
.key-example code {
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  padding: 1px 5px;
  font-family: Consolas, Monaco, monospace;
  color: #303133;
}
.key-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.code-text-sm {
  font-family: Consolas, Monaco, monospace;
  font-size: 12.5px;
  color: #606266;
}

/* ---- 本月用量颜色 ---- */
.usage-ok {
  color: #303133;
}
.usage-warn {
  color: #e6a23c;
  font-weight: 600;
}
.usage-full {
  color: #c0392b;
  font-weight: 600;
}
</style>
