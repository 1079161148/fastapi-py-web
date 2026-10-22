import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', guestOnly: true },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/RegisterView.vue'),
      meta: { title: '注册', guestOnly: true },
    },
    {
      // 所有页面统一挂在外壳下：侧栏 + 顶栏只渲染一次。
      //
      // requiresAuth 必须写在**每个子路由**上，不能写在父级：外壳同时承载
      // 访客可访问的 /chat，父级一加 requiresAuth 就会把匿名访客弹到登录页，
      // 破坏 M2 的访客直聊。
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        {
          path: '',
          name: 'home',
          component: () => import('@/views/HomeView.vue'),
          meta: { title: '工作台', requiresAuth: true },
        },
        {
          // 访客可直达：登录与匿名两种身份共用同一页面（设计文档 M2）
          path: 'chat',
          name: 'chat',
          component: () => import('@/views/ChatView.vue'),
          meta: { title: '在线客服' },
        },
        {
          path: 'knowledge',
          name: 'knowledge',
          component: () => import('@/views/KnowledgeView.vue'),
          meta: { title: '知识库', requiresAuth: true },
        },
        {
          // M6 坐席工作台：接管、回复、结束人工服务
          path: 'agent',
          name: 'agent',
          component: () => import('@/views/AgentView.vue'),
          meta: { title: '人工坐席', requiresAuth: true },
        },
        {
          // M4 渠道接入：展示各渠道可用状态与回调地址（凭证在环境变量里）
          path: 'channels',
          name: 'channels',
          component: () => import('@/views/ChannelsView.vue'),
          meta: { title: '渠道接入', requiresAuth: true },
        },
        {
          // M7 管理后台：用量成本看板 + 成员管理（页面内部再按角色判断可见性）
          path: 'admin',
          name: 'admin',
          component: () => import('@/views/AdminView.vue'),
          meta: { title: '管理后台', requiresAuth: true },
        },
        {
          // M8 运营支撑：留言箱 / 数据报表 / 快捷回复 / 客户管理 / 系统设置 / 审计日志
          path: 'leave-messages',
          name: 'leave-messages',
          component: () => import('@/views/LeaveMessagesView.vue'),
          meta: { title: '留言箱', requiresAuth: true },
        },
        {
          path: 'stats',
          name: 'stats',
          component: () => import('@/views/StatsView.vue'),
          meta: { title: '数据报表', requiresAuth: true },
        },
        {
          path: 'quick-replies',
          name: 'quick-replies',
          component: () => import('@/views/QuickRepliesView.vue'),
          meta: { title: '快捷回复', requiresAuth: true },
        },
        {
          path: 'customers',
          name: 'customers',
          component: () => import('@/views/CustomersView.vue'),
          meta: { title: '客户管理', requiresAuth: true },
        },
        {
          path: 'tickets',
          name: 'tickets',
          component: () => import('@/views/TicketsView.vue'),
          meta: { title: '工单管理', requiresAuth: true },
        },
        {
          path: 'insights',
          name: 'insights',
          component: () => import('@/views/InsightsView.vue'),
          meta: { title: 'AI 优化', requiresAuth: true },
        },
        {
          path: 'quality',
          name: 'quality',
          component: () => import('@/views/QualityView.vue'),
          meta: { title: '会话质检', requiresAuth: true },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/SettingsView.vue'),
          meta: { title: '系统设置', requiresAuth: true },
        },
        {
          // useTable 封装的用法示例，作为后续列表页的模板
          path: 'demo/table',
          name: 'table-demo',
          component: () => import('@/views/TableDemoView.vue'),
          meta: { title: '组件示例', requiresAuth: true },
        },
      ],
    },
  ],
})

// 守卫依赖 store 的 restored 标记：刷新页面时先恢复登录态再判定，
// 否则会把已登录用户误弹到登录页
router.beforeEach(async (to) => {
  const { useAuthStore } = await import('@/stores/auth')
  const auth = useAuthStore()
  if (!auth.restored) {
    await auth.restore()
  }

  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.meta.guestOnly && auth.isLoggedIn) {
    return { path: '/' }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title
    ? `${to.meta.title as string} - 智能客服 AI 系统`
    : '智能客服 AI 系统'
})

export default router
