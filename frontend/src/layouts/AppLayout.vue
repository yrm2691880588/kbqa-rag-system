<template>
  <!-- 主布局：左侧导航栏 + 右侧内容区 -->
  <el-container class="layout">
    <el-aside width="230px" class="aside">
      <div class="brand">
        <el-icon :size="22"><ChatDotRound /></el-icon>
        <span>知识库问答</span>
      </div>

      <el-menu :default-active="$route.path" router class="menu">
        <el-menu-item index="/chat">
          <el-icon><ChatLineRound /></el-icon>
          <span>知识库问答</span>
        </el-menu-item>
        <!-- 知识库管理入口：只有管理员能看到（后端还有一层权限校验兜底） -->
        <el-menu-item v-if="auth.user?.role === 'admin'" index="/admin">
          <el-icon><FolderOpened /></el-icon>
          <span>知识库管理</span>
        </el-menu-item>
      </el-menu>

      <div class="user-area">
        <el-dropdown trigger="click" @command="onUserCommand">
          <span class="user-trigger">
            <el-avatar :size="30">{{ avatarText }}</el-avatar>
            <span class="user-name">{{ displayName }}</span>
            <el-tag v-if="auth.user?.role === 'admin'" size="small" type="warning">管理员</el-tag>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="password">
                <el-icon><Key /></el-icon>修改密码
              </el-dropdown-item>
              <el-dropdown-item command="logout" divided>
                <el-icon><SwitchButton /></el-icon>退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </el-aside>

    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>

  <!-- 修改密码弹窗 -->
  <ChangePasswordDialog v-model="showPasswordDialog" />
  <!-- 绑定手机号弹窗 -->
  <BindPhoneDialog v-model="showBindPhoneDialog" />
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { Iphone } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import ChangePasswordDialog from '@/components/ChangePasswordDialog.vue'
import BindPhoneDialog from '@/components/BindPhoneDialog.vue'

const auth = useAuthStore()
const router = useRouter()
const showPasswordDialog = ref(false)
const showBindPhoneDialog = ref(false)

// 显示名优先用昵称，没填昵称就用用户名
const displayName = computed(() => auth.user?.nickname || auth.user?.username || '')
const avatarText = computed(() => displayName.value.slice(0, 1).toUpperCase() || '?')

function onUserCommand(cmd: string) {
  if (cmd === 'password') {
    showPasswordDialog.value = true
  } else if (cmd === 'bindphone') {
    showBindPhoneDialog.value = true
  } else if (cmd === 'logout') {
    ElMessageBox.confirm('确定要退出登录吗？', '提示', { type: 'warning' })
      .then(async () => {
        await auth.logout()
        router.push('/login')
      })
      .catch(() => {})
  }
}
</script>

<style scoped>
.layout {
  height: 100vh;
}

.aside {
  display: flex;
  flex-direction: column;
  background: #ffffff;
  border-right: 1px solid #e4e7ed;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 18px 20px;
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

.menu {
  border-right: none;
  flex: 1;
}

.user-area {
  padding: 12px 16px;
  border-top: 1px solid #e4e7ed;
}

.user-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  outline: none;
}

.user-name {
  max-width: 90px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #303133;
}

.phone-tag {
  margin-left: 6px;
}

.main {
  padding: 0;
  background: #f5f7fa;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.main > :deep(*) {
  flex: 1;
  min-height: 0;
}
</style>
