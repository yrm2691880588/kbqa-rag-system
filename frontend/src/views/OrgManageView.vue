<template>
  <!--
    组织管理页：平台超管专用。这里的"组织" = 一家客户公司。

    【结构说明】本文件只负责"列表 + 新建/编辑"这一件事，
    其余四个弹窗都拆到了 @/components/org/ 下：
      · OrgQuotaDialog       —— 额度设置
      · OrgApiKeyDialog      —— API 密钥管理
      · OrgMembersDialog     —— 成员列表
      · OrgInviteCodeDialog  —— 邀请码展示
    拆分原因：原先这些全塞在同一个 815 行的文件里，改额度要在一大堆
    无关代码中找位置，容易改错。现在每个文件只为一个职责服务。
  -->
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

    <!-- 新建 / 编辑组织（本文件自己的职责，保留在这里） -->
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

    <!-- 以下四个弹窗已拆成独立组件 -->
    <OrgInviteCodeDialog
      v-model="codeDialog.visible"
      :code="codeDialog.code"
      :org-name="codeDialog.orgName"
    />

    <OrgQuotaDialog
      v-model="quotaDialogVisible"
      :org-id="currentOrgId"
      :org-name="currentOrgName"
      @saved="loadOrgs"
    />

    <OrgApiKeyDialog
      v-model="keyDialogVisible"
      :org-id="currentOrgId"
      :org-name="currentOrgName"
    />

    <OrgMembersDialog
      v-model="memberDialogVisible"
      :org-id="currentOrgId"
      :org-name="currentOrgName"
    />
  </div>
</template>

<script setup lang="ts">
// 组织管理页（P1-A）：只有平台超管能进（路由守卫 + 后端接口双重校验）
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import {
  createOrg,
  listOrgs,
  rotateInvite,
  updateOrg,
  type OrgItem,
} from '@/api/org'
import OrgQuotaDialog from '@/components/org/OrgQuotaDialog.vue'
import OrgApiKeyDialog from '@/components/org/OrgApiKeyDialog.vue'
import OrgMembersDialog from '@/components/org/OrgMembersDialog.vue'
import OrgInviteCodeDialog from '@/components/org/OrgInviteCodeDialog.vue'

const orgs = ref<OrgItem[]>([])
const loading = ref(false)

const editDialog = ref({ visible: false, isEdit: false, id: 0, name: '', description: '' })
const saving = ref(false)

// 四个弹窗共用的"当前操作对象"
const currentOrgId = ref(0)
const currentOrgName = ref('')

const quotaDialogVisible = ref(false)
const keyDialogVisible = ref(false)
const memberDialogVisible = ref(false)

// 邀请码弹窗：创建后/重置后各展示一次
const codeDialog = ref({ visible: false, code: '', orgName: '' })

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
      const created = await createOrg(name.trim(), description.trim())
      editDialog.value.visible = false
      // 新建成功后立刻亮出邀请码：这是唯一一次能看到它的机会
      codeDialog.value = {
        visible: true,
        code: created.invite_code,
        orgName: created.name,
      }
    }
    await loadOrgs()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    saving.value = false
  }
}

/** 重置邀请码：旧码立即失效，必须让用户马上把新码发出去 */
async function doRotate(row: OrgItem) {
  try {
    await ElMessageBox.confirm(
      `重置后，旧邀请码立即失效（已经用它发出去的码都作废）。确定重置「${row.name}」的邀请码？`,
      '重置邀请码',
      { type: 'warning', confirmButtonText: '确认重置', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const got = await rotateInvite(row.id)
    codeDialog.value = { visible: true, code: got.invite_code, orgName: row.name }
    await loadOrgs()
  } catch {
    /* 已提示 */
  }
}

/** 停用 / 启用组织 */
async function toggleActive(row: OrgItem) {
  const willDisable = row.is_active !== false
  try {
    await ElMessageBox.confirm(
      willDisable
        ? `停用「${row.name}」后，该组织下所有账号都无法登录使用。确定停用？`
        : `启用「${row.name}」后，该组织的账号可以正常登录。确定启用？`,
      willDisable ? '停用组织' : '启用组织',
      { type: 'warning', confirmButtonText: willDisable ? '确认停用' : '确认启用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await updateOrg(row.id, { is_active: !willDisable })
    ElMessage.success(willDisable ? '已停用' : '已启用')
    await loadOrgs()
  } catch {
    /* 已提示 */
  }
}

/** 本月用量超过 80% 标黄，超过 100% 标红 */
function usageClass(row: OrgItem): string {
  const limit = row.monthly_question_limit
  if (limit === null || limit === undefined || limit <= 0) return ''
  const ratio = (row.monthly_questions || 0) / limit
  if (ratio >= 1) return 'usage-over'
  if (ratio >= 0.8) return 'usage-warn'
  return ''
}

function openQuota(row: OrgItem) {
  currentOrgId.value = row.id
  currentOrgName.value = row.name
  quotaDialogVisible.value = true
}

function openApiKeys(row: OrgItem) {
  currentOrgId.value = row.id
  currentOrgName.value = row.name
  keyDialogVisible.value = true
}

function openMembers(row: OrgItem) {
  currentOrgId.value = row.id
  currentOrgName.value = row.name
  memberDialogVisible.value = true
}

function fmtTime(s?: string | null): string {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

onMounted(loadOrgs)
</script>

<style scoped>
.org-page {
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

.title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.tip {
  margin-bottom: 16px;
}

.org-name {
  font-weight: 500;
}

.muted {
  color: #c0c4cc;
}

.usage-warn {
  color: #e6a23c;
  font-weight: 500;
}

.usage-over {
  color: #f56c6c;
  font-weight: 600;
}
</style>
