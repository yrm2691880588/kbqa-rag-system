<template>
  <!-- 成员管理：组织管理员管本组织的人；平台超管可切换组织 -->
  <div class="page">
    <div class="page-head">
      <div class="head-left">
        <h2 class="title">成员管理</h2>
        <el-tag type="info" effect="plain" size="small">共 {{ members.length }} 人</el-tag>
      </div>
      <div class="head-right">
        <el-input
          v-model="keyword"
          placeholder="搜用户名 / 昵称 / 手机号"
          clearable
          style="width: 240px"
          @keyup.enter="load"
          @clear="load"
        >
          <template #append>
            <el-button @click="load">搜索</el-button>
          </template>
        </el-input>
      </div>
    </div>

    <el-alert type="info" :closable="false" show-icon class="tip">
      <template #title>
        这里只能管理<b>本组织</b>的成员。组织管理员可以调整成员角色、停用账号、帮成员重置密码；
        平台超管的账号不受组织侧管理。停用或重置密码后，该成员已登录的设备会立即失效。
      </template>
    </el-alert>

    <el-card shadow="never">
      <el-table :data="members" v-loading="loading" size="default">
        <el-table-column prop="username" label="登录名" min-width="130" />
        <el-table-column prop="nickname" label="昵称" min-width="120" />
        <el-table-column label="角色" width="130" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.is_superadmin" type="danger" size="small">平台超管</el-tag>
            <el-tag v-else-if="row.role === 'admin'" type="warning" size="small">组织管理员</el-tag>
            <el-tag v-else type="info" size="small">普通成员</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="手机号" width="135">
          <template #default="{ row }">{{ row.phone || '未绑定' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="85" align="center">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'danger'" size="small">
              {{ row.is_active ? '正常' : '已停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近登录" width="160">
          <template #default="{ row }">{{ fmtTime(row.last_login_at) || '从未登录' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="260" align="center">
          <template #default="{ row }">
            <!-- 自己 / 平台超管：不给任何管理按钮，避免误操作把自己锁在门外 -->
            <span v-if="!canManage(row)" class="no-action">—</span>
            <template v-else>
              <el-button link type="primary" size="small" @click="toggleRole(row)">
                {{ row.role === 'admin' ? '降为成员' : '设为管理员' }}
              </el-button>
              <el-button link :type="row.is_active ? 'warning' : 'success'" size="small" @click="toggleActive(row)">
                {{ row.is_active ? '停用' : '启用' }}
              </el-button>
              <el-button link type="danger" size="small" @click="doReset(row)">重置密码</el-button>
            </template>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="没有找到成员" :image-size="80" />
        </template>
      </el-table>
    </el-card>

    <!-- 临时密码展示：只出现这一次 -->
    <el-dialog v-model="tempDialog.visible" title="临时密码" width="520px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon class="tip">
        <template #title>
          请立刻复制并通过可靠渠道告诉本人。这个密码<b>只显示这一次</b>，关掉后就查不到了。
          并请本人登录后尽快自行修改密码。
        </template>
      </el-alert>
      <div class="pwd-box">
        <span class="pwd-text">{{ tempDialog.password }}</span>
        <el-button type="primary" plain size="small" @click="copyTemp">复制</el-button>
      </div>
      <div class="pwd-user">所属成员：{{ tempDialog.username }}</div>
      <template #footer>
        <el-button type="primary" @click="tempDialog.visible = false">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
// 成员管理页：只有管理员能进（路由守卫 + 后端双重校验）
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import {
  listMembers,
  patchMember,
  resetMemberPassword,
  type MemberPatch,
} from '@/api/admin'
import type { UserInfo } from '@/stores/auth'

const auth = useAuthStore()

const members = ref<UserInfo[]>([])
const loading = ref(false)
const keyword = ref('')

const tempDialog = ref({ visible: false, username: '', password: '' })

/** 能不能管这个人：不能管自己，也不能管平台超管 */
function canManage(row: UserInfo): boolean {
  if (row.is_superadmin) return false
  if (row.id === auth.user?.id) return false
  return true
}

async function load() {
  loading.value = true
  try {
    members.value = await listMembers({ keyword: keyword.value.trim() })
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

/** 改角色：管理员 <-> 普通成员 */
async function toggleRole(row: UserInfo) {
  const next: MemberPatch['role'] = row.role === 'admin' ? 'user' : 'admin'
  const label = next === 'admin' ? '设为组织管理员' : '降为普通成员'
  try {
    await ElMessageBox.confirm(
      `确定把「${row.nickname || row.username}」${label}？` +
        (next === 'admin' ? '管理员可以管理本组织的知识库和成员。' : ''),
      '确认变更',
      { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await patchMember(row.id, { role: next })
    ElMessage.success('已更新')
    await load()
  } catch {
    /* 已提示 */
  }
}

/** 停用 / 启用账号 */
async function toggleActive(row: UserInfo) {
  const next = !row.is_active
  if (!next) {
    try {
      await ElMessageBox.confirm(
        `停用「${row.nickname || row.username}」后，该账号将无法登录，已登录的设备也会立刻被踢下线。确定停用？`,
        '停用账号',
        { type: 'warning', confirmButtonText: '确认停用', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  try {
    await patchMember(row.id, { is_active: next })
    ElMessage.success(next ? '已启用' : '已停用')
    await load()
  } catch {
    /* 已提示 */
  }
}

/** 重置密码：由系统生成随机临时密码，一次性展示 */
async function doReset(row: UserInfo) {
  try {
    await ElMessageBox.confirm(
      `将为「${row.nickname || row.username}」重置密码。系统会生成一个随机临时密码，` +
        `只显示一次；该成员当前已登录的设备会立刻失效。确定继续？`,
      '重置密码',
      { type: 'warning', confirmButtonText: '确认重置', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const got = await resetMemberPassword(row.id)
    tempDialog.value = { visible: true, username: got.username, password: got.temp_password }
  } catch {
    /* 已提示 */
  }
}

async function copyTemp() {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(tempDialog.value.password)
      ElMessage.success('已复制到剪贴板')
      return
    }
    throw new Error('no clipboard')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选中上面的密码复制')
  }
}

function fmtTime(s?: string | null): string {
  if (!s) return ''
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
}
.title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}
.tip {
  margin: 0;
}
.no-action {
  color: #c0c4cc;
}
.pwd-box {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px dashed #409eff;
  border-radius: 8px;
  background: #ecf5ff;
}
.pwd-text {
  flex: 1;
  font-family: Consolas, Monaco, monospace;
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  word-break: break-all;
  user-select: all;
}
.pwd-user {
  margin-top: 10px;
  font-size: 13px;
  color: #909399;
}
</style>
