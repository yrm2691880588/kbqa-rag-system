<template>
  <!--
    成员列表弹窗：看某家组织下有哪些账号。

    为什么单独拆成组件：
      它只负责"展示"（只读），不涉及任何修改操作。
      拆出来后，组织管理页就不用再为"拉成员列表"维护一份 state
      和 loading 标记，职责更清晰。
  -->
  <el-dialog v-model="visible" :title="`成员：${orgName}`" width="720px">
    <el-table :data="members" v-loading="loading" size="small" max-height="420">
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
          <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
            {{ row.is_active ? '正常' : '停用' }}
          </el-tag>
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
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { listMembers, type OrgMember } from '@/api/org'

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
watch(visible, (v) => emit('update:modelValue', v))

const members = ref<OrgMember[]>([])
const loading = ref(false)

function fmtTime(s?: string | null): string {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function load() {
  if (!props.orgId) return
  loading.value = true
  try {
    members.value = await listMembers(props.orgId)
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
</script>
