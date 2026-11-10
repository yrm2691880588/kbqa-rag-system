// 路由表与权限守卫
import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/RegisterView.vue'),
      meta: { title: '注册', public: true },
    },
    {
      // 主布局：带左侧栏（会话/导航）与顶部用户菜单
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      redirect: '/chat',
      children: [
        {
          path: 'chat',
          name: 'chat',
          component: () => import('@/views/ChatView.vue'),
          meta: { title: '知识库问答', requiresAuth: true },
        },
        {
          // 管理页占位：M2 起填充知识库管理/检索测试/看板
          path: 'admin',
          name: 'admin',
          component: () => import('@/views/KnowledgeBaseView.vue'),
          meta: { title: '知识库管理', requiresAuth: true, requiresAdmin: true },
        },
        {
          // 成员管理（P1-C）：组织管理员管本组织的人
          path: 'members',
          name: 'members',
          component: () => import('@/views/MembersView.vue'),
          meta: { title: '成员管理', requiresAuth: true, requiresAdmin: true },
        },
        {
          // 问答日志（P1-C）：看哪些问题没答上，是补资料的依据
          path: 'query-logs',
          name: 'query-logs',
          component: () => import('@/views/QueryLogView.vue'),
          meta: { title: '问答日志', requiresAuth: true, requiresAdmin: true },
        },
        {
          // 组织管理：只有平台超管能进（建客户组织、发邀请码）
          path: 'orgs',
          name: 'orgs',
          component: () => import('@/views/OrgManageView.vue'),
          meta: { title: '组织管理', requiresAuth: true, requiresSuperadmin: true },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})

// 全局前置守卫：控制"未登录/非管理员"是否能进入页面
router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 1) 页面明确允许游客访问（登录/注册页）且已登录时，直接送去主页
  if (to.meta.public) {
    if (auth.accessToken) return '/chat'
    return true
  }

  // 2) 未登录 → 一律跳登录页（并记录想去的地址，登录后跳回）
  if (!auth.accessToken) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // 3) 已登录但还没拉取用户信息时，先补拉一次（含角色）
  if (!auth.user) {
    try {
      await auth.fetchMe()
    } catch {
      auth.clear()
      return { path: '/login' }
    }
  }

  // 4) 页面要求管理员，但当前是普通用户 → 回到问答页
  //    注意：平台超管也是 admin，所以这里用 isAdmin（含超管）判断，避免超管被挡在外面
  if (to.meta.requiresAdmin && !auth.isAdmin) {
    return '/chat'
  }

  // 5) 页面要求平台超管，但当前不是 → 回到问答页
  //    超管是"跨组织"的角色（建客户组织），普通组织管理员不能进，否则隔离形同虚设
  if (to.meta.requiresSuperadmin && !auth.isSuperadmin) {
    return '/chat'
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · 知识库问答系统` : '知识库问答系统'
})

export default router
