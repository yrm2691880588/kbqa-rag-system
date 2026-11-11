<template>
  <!-- 主布局：左侧导航栏 + 右侧内容区 -->
  <el-container class="layout">
    <el-aside width="230px" class="aside">
      <div class="brand">
        <el-icon :size="22"><ChatDotRound /></el-icon>
        <span>知识库问答</span>
      </div>

      <!-- 当前所在的组织：让用户随时知道"我现在在哪家公司的空间里工作" -->
      <div v-if="orgName" class="org-banner" :title="`当前组织：${orgName}`">
        <el-icon><OfficeBuilding /></el-icon>
        <span class="org-name">{{ orgName }}</span>
      </div>
      <div v-else-if="auth.user && !auth.user.org_id" class="org-banner warn" title="你还没有归属任何组织，将看不到任何知识库">
        <el-icon><WarningFilled /></el-icon>
        <span class="org-name">未归属组织</span>
      </div>

      <el-menu :default-active="$route.path" router class="menu">
        <!-- 概览：管理员一进来先看整体状况，而不是直接进聊天 -->
        <el-menu-item v-if="auth.isAdmin && auth.user?.org_id" index="/dashboard">
          <el-icon><DataBoard /></el-icon>
          <span>概览</span>
        </el-menu-item>
        <el-menu-item index="/chat">
          <el-icon><ChatLineRound /></el-icon>
          <span>知识库问答</span>
        </el-menu-item>
        <!-- 知识库管理入口：只有管理员能看到（后端还有一层权限校验兜底） -->
        <el-menu-item v-if="auth.isAdmin" index="/admin">
          <el-icon><FolderOpened /></el-icon>
          <span>知识库管理</span>
        </el-menu-item>
        <!-- 成员管理：组织管理员管本组织的人 -->
        <el-menu-item v-if="auth.isAdmin && auth.user?.org_id" index="/members">
          <el-icon><UserFilled /></el-icon>
          <span>成员管理</span>
        </el-menu-item>
        <!-- 问答日志：看"哪些问题没答上"，是优化知识库的依据 -->
        <el-menu-item v-if="auth.isAdmin && auth.user?.org_id" index="/query-logs">
          <el-icon><DataLine /></el-icon>
          <span>问答日志</span>
        </el-menu-item>
        <!-- 用户反馈：点了踩之后具体哪里不对，是改进的最直接依据 -->
        <el-menu-item v-if="auth.isAdmin && auth.user?.org_id" index="/feedbacks">
          <el-icon><ChatLineSquare /></el-icon>
          <span>用户反馈</span>
        </el-menu-item>
        <!-- 组织管理入口：只有平台超管能看到 -->
        <el-menu-item v-if="auth.isSuperadmin" index="/orgs">
          <el-icon><OfficeBuilding /></el-icon>
          <span>组织管理</span>
        </el-menu-item>
      </el-menu>

      <div class="user-area">
        <el-dropdown trigger="click" @command="onUserCommand">
          <span class="user-trigger">
            <el-avatar :size="30">{{ avatarText }}</el-avatar>
            <span class="user-name">{{ displayName }}</span>
            <el-tag v-if="auth.isSuperadmin" size="small" type="danger">平台超管</el-tag>
            <el-tag v-else-if="auth.user?.role === 'admin'" size="small" type="warning">管理员</el-tag>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <!-- 组织管理员可以随时查看本组织的邀请码，用来拉同事进来 -->
              <el-dropdown-item v-if="canSeeInvite" command="invite">
                <el-icon><Ticket /></el-icon>我的邀请码
              </el-dropdown-item>
              <el-dropdown-item v-if="!auth.isPhoneBound()" command="bindphone">
                <el-icon><Iphone /></el-icon>绑定手机号
              </el-dropdown-item>
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

  <!-- 我的邀请码弹窗：方便组织管理员把邀请码发给新同事 -->
  <el-dialog v-model="showInviteDialog" title="我的组织邀请码" width="500px">
    <el-alert type="info" :closable="false" show-icon class="invite-alert">
      <template #title>
        把这个邀请码发给要加入本组织的同事，他们注册时填上就能进入「{{ orgName || '本组织' }}」。
        请不要发到公开的群里。
      </template>
    </el-alert>
    <div class="invite-box">
      <span class="invite-code">{{ inviteCode }}</span>
      <el-button type="primary" plain size="small" @click="copyInvite">复制</el-button>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ChatLineSquare,
  Iphone,
  Key,
  OfficeBuilding,
  SwitchButton,
  Ticket,
  WarningFilled,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { getMyInviteCode, getMyOrg } from '@/api/org'
import ChangePasswordDialog from '@/components/ChangePasswordDialog.vue'
import BindPhoneDialog from '@/components/BindPhoneDialog.vue'

const auth = useAuthStore()
const router = useRouter()
const showPasswordDialog = ref(false)
const showBindPhoneDialog = ref(false)

// 当前组织名（拿不到就不显示那一行，不影响使用）
const orgName = ref('')

// 显示名优先用昵称，没填昵称就用用户名
const displayName = computed(() => auth.user?.nickname || auth.user?.username || '')
const avatarText = computed(() => displayName.value.slice(0, 1).toUpperCase() || '?')

// 谁能看到"我的邀请码"：平台超管、或本组织的管理员
const canSeeInvite = computed(
  () => auth.isSuperadmin || (auth.user?.role === 'admin' && !!auth.user?.org_id),
)

const showInviteDialog = ref(false)
const inviteCode = ref('')

/** 加载"我在哪个组织"（用于侧边栏显示） */
async function loadMyOrg() {
  if (!auth.user?.org_id) {
    orgName.value = ''
    return
  }
  try {
    orgName.value = (await getMyOrg()).name
  } catch {
    orgName.value = ''
  }
}

// 用户信息就绪后再去查组织名（登录后 user 是异步补上的）
watch(
  () => auth.user?.org_id,
  () => loadMyOrg(),
  { immediate: true },
)

async function onUserCommand(cmd: string) {
  if (cmd === 'password') {
    showPasswordDialog.value = true
  } else if (cmd === 'bindphone') {
    showBindPhoneDialog.value = true
  } else if (cmd === 'invite') {
    try {
      inviteCode.value = (await getMyInviteCode()).invite_code
      showInviteDialog.value = true
    } catch {
      /* 无权限/无组织：拦截器已提示 */
    }
  } else if (cmd === 'logout') {
    ElMessageBox.confirm('确定要退出登录吗？', '提示', { type: 'warning' })
      .then(async () => {
        await auth.logout()
        router.push('/login')
      })
      .catch(() => {})
  }
}

/** 复制邀请码（浏览器不允许时提示手动复制） */
async function copyInvite() {
  try {
    await navigator.clipboard.writeText(inviteCode.value)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选中邀请码复制')
  }
}
</script>

<style scoped>
.layout {
  height: 100vh;
}

/* 侧边栏：浅色 + 细边框分区。
   ─────────────────────────────────────────────
   这是一次方案修正。第一版用了深色侧边栏，
   结果「纯黑导航 + 浅灰列表 + 纯白主区」三块硬切，非常割裂；
   而且这正是十年前后台模板的典型做法，观感老套。

   现在改成：与内容区同为浅色，只用一条极淡边框分区。
   靠留白和边框建立层次，而不是靠大色块。 */
.aside {
  display: flex;
  flex-direction: column;
  background: var(--aside-bg);
  border-right: 1px solid var(--aside-border);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 20px 18px;
  font-size: var(--font-lg);
  font-weight: 600;
  color: var(--text-1);
  letter-spacing: -0.01em;
}

/* 品牌图标加一点主色底，让 logo 有存在感 */
.brand .el-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  background: linear-gradient(135deg, var(--brand-400), var(--brand-600));
  color: #fff;
  box-shadow: 0 2px 6px rgba(79, 110, 247, 0.35);
}

.menu {
  border-right: none;
  flex: 1;
  background: transparent;
  padding: 0 10px;
}

/* ---- 菜单项（深色主题）---- */
.aside :deep(.el-menu-item) {
  height: 40px;
  line-height: 40px;
  margin-bottom: 1px;
  border-radius: var(--radius-sm);
  color: var(--aside-text);
  font-size: var(--font-base);
  transition: background var(--transition-fast), color var(--transition-fast);
}

.aside :deep(.el-menu-item:hover) {
  background: var(--aside-bg-hover);
  color: var(--text-1);
}

/* 选中态：主色浅底 + 主色文字。
   原来用实心主色块（深色背景下才需要那么强的对比），
   浅色背景下实心块太重，会抢走内容区的注意力。 */
.aside :deep(.el-menu-item.is-active) {
  background: var(--aside-active-bg);
  color: var(--aside-active-text);
  font-weight: 600;
}

/* 选中项左侧加一条主色指示条，比整块底色更轻、更精致 */
.aside :deep(.el-menu-item.is-active)::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 18px;
  border-radius: 0 2px 2px 0;
  background: var(--brand-500);
}

/* ---- 底部用户区（浅色底）---- */
.user-area {
  padding: 12px 14px;
  border-top: 1px solid var(--aside-border);
  margin-top: auto;
}

.user-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.user-trigger:hover {
  background: var(--aside-bg-hover);
}

.user-area :deep(.el-avatar) {
  background: linear-gradient(135deg, var(--brand-400), var(--brand-600));
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
}

.user-area .user-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--font-sm);
  color: var(--text-1);
  font-weight: 500;
}

/* 当前组织条：夹在品牌名和菜单之间，让人一眼知道自己"在哪家公司的空间里" */
.org-banner {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 14px 12px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  color: var(--text-3);
  font-size: var(--font-xs);
  line-height: 1;
  border: 1px solid var(--border-2);
}

/* 没有组织归属：换成醒目的警告色（这种账号什么都看不到，必须让人知道） */
.org-banner.warn {
  background: var(--warning-bg);
  border-color: rgba(247, 144, 9, 0.3);
  color: var(--warning);
}

.org-banner .org-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}

/* 邀请码弹窗 */
.invite-alert {
  margin-bottom: 14px;
}

.invite-box {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px dashed var(--brand-500);
  border-radius: 8px;
  background: var(--brand-50);
}

.invite-code {
  flex: 1;
  font-family: Consolas, Monaco, monospace;
  font-size: 18px;
  font-weight: 600;
  color: var(--text-1);
  word-break: break-all;
  user-select: all;
}

.user-area {
  padding: 12px 16px;
  border-top: 1px solid var(--border-1);
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
  color: var(--text-1);
}

.phone-tag {
  margin-left: 6px;
}

/* 主内容容器。
   ─────────────────────────────────────────────
   原来这里是 --bg-hover（#f2f3f5 灰底），
   外面套着白色卡片 —— 于是在"导航(极浅) / 列表(极浅) / 容器(灰) / 内容(白)"
   之间又多了第五种色阶，正是割裂感的来源之一。

   现在改成白色，与内部内容区同色。
   最终整个界面只有两类底色：
     · 侧边两块（导航 + 会话列表）：极浅 #fbfcfd，作为"背景层"
     · 主内容区：纯白，作为"内容层"
   两层结构，靠一条边框分隔，不再有多层色阶叠加。 */
.main {
  padding: 0;
  background: var(--bg-card);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.main > :deep(*) {
  flex: 1;
  min-height: 0;
}
</style>
