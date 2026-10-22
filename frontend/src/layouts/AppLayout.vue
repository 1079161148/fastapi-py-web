<script setup lang="ts">
/**
 * 应用外壳（M7 视觉重构）：深色侧栏 + 顶栏 + 内容区。
 *
 * 为什么自己写而不用 el-menu：客服后台的导航是"固定图标 + 文字"的窄栏，
 * el-menu 的展开/折叠动画与层级样式在这里是负担；自绘 60 行 CSS 更可控。
 *
 * 导航按角色过滤：坐席看不到管理后台，普通客户看不到知识库。
 * 这只是体验层过滤，真正的门禁在后端（require_agent / require_tenant_admin）。
 */
import { ElMessageBox } from 'element-plus'
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

interface NavItem {
  to: string
  label: string
  icon: string
  visible: boolean
}

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? '工作台')

const isAuthed = computed(() => auth.isLoggedIn)

const displayName = computed(
  () => auth.user?.display_name || auth.user?.email.split('@')[0] || '未登录',
)
const initial = computed(() => displayName.value.slice(0, 1).toUpperCase())
const roleLabel = computed(() => {
  // 展示用租户内角色：平台 admin 在别人租户里也只是成员，署"管理员"会误导
  const map: Record<string, string> = {
    owner: '所有者',
    admin: '管理员',
    agent: '坐席',
    viewer: '只读',
  }
  const role = auth.user?.tenant_role ?? ''
  return map[role] ?? role
})

const navItems = computed<NavItem[]>(() => {
  // 访客（未登录）只能用在线客服：其余页面都要登录才有意义
  if (!isAuthed.value) {
    return [{ to: '/chat', label: '在线客服', icon: 'ChatDotRound', visible: true }]
  }
  // 导航按租户内角色收口，与后端 require_tenant_admin / require_agent 对齐；
  // 用平台 role 会出现"看得见菜单、点进去 403"
  const role = auth.user?.tenant_role
  const isAdmin = role === 'owner' || role === 'admin'
  const items: NavItem[] = [
    { to: '/', label: '工作台', icon: 'HomeFilled', visible: true },
    { to: '/chat', label: '在线客服', icon: 'ChatDotRound', visible: true },
    {
      to: '/knowledge',
      label: '知识库',
      icon: 'Collection',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/agent',
      label: '人工坐席',
      icon: 'Service',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/leave-messages',
      label: '留言箱',
      icon: 'Message',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/stats',
      label: '数据报表',
      icon: 'TrendCharts',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/quick-replies',
      label: '快捷回复',
      icon: 'ChatLineSquare',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/customers',
      label: '客户管理',
      icon: 'User',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/tickets',
      label: '工单管理',
      icon: 'Tickets',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/insights',
      label: 'AI 优化',
      icon: 'DataAnalysis',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/quality',
      label: '会话质检',
      icon: 'Stamp',
      visible: isAdmin || role === 'agent',
    },
    { to: '/channels', label: '渠道接入', icon: 'Connection', visible: isAdmin },
    {
      to: '/settings',
      label: '系统设置',
      icon: 'Setting',
      visible: isAdmin,
    },
    { to: '/admin', label: '管理后台', icon: 'DataLine', visible: isAdmin },
    // 组件示例是开发脚手架，不进主导航（路由仍保留，便于对照 useTable 用法）
  ]
  return items.filter((item) => item.visible)
})

function isActive(path: string): boolean {
  // 首页必须精确匹配：否则任何路径都会点亮"工作台"
  return path === '/' ? route.path === '/' : route.path.startsWith(path)
}

/**
 * 导航目标地址。
 *
 * `/chat` 需要显式租户参数：ChatView 在拿不到 query 时会要求手填租户 UUID
 * （生产靠子域名解析，本地没有）。已登录用户有 tenantId，直接带上，
 * 否则从侧栏点进去会卡在"先确定租户"。
 */
function navTarget(path: string): string | { path: string; query: Record<string, string> } {
  if (path === '/chat' && auth.tenantId) {
    return { path: '/chat', query: { tenant: auth.tenantId } }
  }
  return path
}

onMounted(async () => {
  // 只对已登录用户拉租户：访客访问 /chat 时外壳同样渲染，
  // 无 token 调 /tenants/current 会 401 并弹一次无意义的错误提示
  if (isAuthed.value && !auth.tenant) {
    await auth.loadTenant()
  }
})

async function onLogout() {
  // 二次确认：退出会清空本地令牌，误触意味着要重新走一遍登录（用用户名登录还要再填租户标识）
  try {
    await ElMessageBox.confirm(
      '退出后需要重新登录才能继续使用，确定退出吗？',
      '退出登录',
      {
        type: 'warning',
        confirmButtonText: '退出登录',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    // 取消不是错误：ElMessageBox 以 reject 表达取消，这里静默返回
    return
  }
  await auth.logout()
  await router.replace('/login')
}
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="sidebar__brand">
        <div class="sidebar__logo">AI</div>
        <div class="sidebar__brand-text">
          <strong>智能客服</strong>
          <span>Customer Service</span>
        </div>
      </div>

      <nav class="sidebar__nav">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="navTarget(item.to)"
          class="nav"
          :class="{ 'nav--active': isActive(item.to) }"
          :title="item.label"
        >
          <el-icon class="nav__icon"><component :is="item.icon" /></el-icon>
          <span class="nav__label">{{ item.label }}</span>
        </router-link>
      </nav>

      <div class="sidebar__footer">
        <template v-if="isAuthed">
          <div class="user">
            <div class="user__avatar">{{ initial }}</div>
            <div class="user__meta">
              <span class="user__name">{{ displayName }}</span>
              <span class="user__role">{{ roleLabel }}</span>
            </div>
          </div>
          <button class="logout" type="button" title="退出登录" @click="onLogout">
            <el-icon><SwitchButton /></el-icon>
          </button>
        </template>
        <router-link v-else to="/login" class="login-cta">登录 / 注册</router-link>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="topbar__left">
          <h1 class="topbar__title">{{ pageTitle }}</h1>
          <span v-if="auth.tenant" class="topbar__tenant">
            {{ auth.tenant.name }}
            <code>{{ auth.tenant.slug }}</code>
          </span>
        </div>
        <div class="topbar__right">
          <template v-if="isAuthed">
            <el-tag
              v-if="auth.tenant"
              size="small"
              :type="auth.tenant.status === 'active' ? 'success' : 'info'"
            >
              {{ auth.tenant.status === 'active' ? '正常' : auth.tenant.status }}
            </el-tag>
            <span class="topbar__account">{{ auth.user?.email }}</span>
          </template>
          <template v-else>
            <span class="topbar__account">访客模式</span>
            <router-link to="/login" class="topbar__login">登录</router-link>
          </template>
        </div>
      </header>

      <main class="content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100vh;
  /* dvh 处理移动端地址栏收放；不支持的浏览器回退到上面的 100vh */
  height: 100dvh;
  overflow: hidden;
  background: var(--app-bg);
}

/* ---------- 侧栏 ---------- */
.sidebar {
  display: flex;
  width: 232px;
  flex: 0 0 232px;
  flex-direction: column;
  background: var(--app-sidebar);
  color: var(--app-sidebar-text);
  height: 100%;
}

.sidebar__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 18px;
}

.sidebar__logo {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  place-items: center;
  border-radius: 10px;
  background: linear-gradient(135deg, #2563eb, #7c3aed);
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.sidebar__brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}
.sidebar__brand-text strong {
  color: #fff;
  font-size: 14px;
  font-weight: 600;
}
.sidebar__brand-text span {
  font-size: 11px;
  color: #64748b;
  letter-spacing: 0.02em;
}

.sidebar__nav {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  overflow-y: auto;
}

.nav {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border-radius: 9px;
  color: var(--app-sidebar-text);
  font-size: 13.5px;
  text-decoration: none;
  transition:
    background 0.15s ease,
    color 0.15s ease;
}
.nav:hover {
  background: var(--app-sidebar-hover);
  color: #e2e8f0;
}
.nav--active {
  background: var(--app-primary);
  color: var(--app-sidebar-active);
}
.nav--active:hover {
  background: var(--app-primary);
  color: var(--app-sidebar-active);
}
.nav__icon {
  font-size: 16px;
}

.sidebar__footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 12px 16px;
  border-top: 1px solid rgba(148, 163, 184, 0.14);
}

.user {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: 9px;
}
.user__avatar {
  display: grid;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  place-items: center;
  border-radius: 50%;
  background: var(--app-sidebar-hover);
  color: #cbd5e1;
  font-size: 13px;
  font-weight: 600;
}
.user__meta {
  display: flex;
  min-width: 0;
  flex-direction: column;
  line-height: 1.35;
}
.user__name {
  overflow: hidden;
  color: #e2e8f0;
  font-size: 12.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user__role {
  color: #64748b;
  font-size: 11px;
}

.logout {
  display: grid;
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  place-items: center;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #64748b;
  cursor: pointer;
}
.logout:hover {
  background: rgba(220, 38, 38, 0.14);
  color: #fca5a5;
}

/* ---------- 主区 ---------- */
.main {
  display: flex;
  min-width: 0;
  /* min-height 必须显式归零：否则 flex 子项不会收缩，内容区滚不起来 */
  min-height: 0;
  flex: 1;
  flex-direction: column;
}

.topbar {
  display: flex;
  height: 60px;
  flex: 0 0 60px;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 24px;
  border-bottom: 1px solid var(--app-border);
  background: rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 10;
}

.topbar__left {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 12px;
}
.topbar__title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.topbar__tenant {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding-left: 12px;
  border-left: 1px solid var(--app-border);
  color: var(--app-text-3);
  font-size: 12.5px;
}
.topbar__tenant code {
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--app-surface-muted);
  color: var(--app-text-2);
  font-size: 11.5px;
}

.topbar__right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.topbar__account {
  color: var(--app-text-3);
  font-size: 12.5px;
}
.topbar__login {
  padding: 5px 12px;
  border-radius: 7px;
  background: var(--app-primary);
  color: #fff;
  font-size: 12.5px;
  font-weight: 550;
  text-decoration: none;
}
.topbar__login:hover {
  background: var(--app-primary-hover);
}

/* 访客态侧栏底部：把"登录/注册"当主行动按钮，而不是显示一个灰色的未登录用户 */
.login-cta {
  display: block;
  width: 100%;
  padding: 9px 12px;
  border-radius: 9px;
  background: var(--app-primary);
  color: #fff;
  font-size: 13px;
  font-weight: 550;
  text-align: center;
  text-decoration: none;
}
.login-cta:hover {
  background: var(--app-primary-hover);
}

.content {
  flex: 1;
  min-height: 0;
  /* 滚动交给内容区：顶栏与侧栏常驻，聊天页才能在内部自己滚 */
  overflow-y: auto;
  padding: 24px;
  max-width: 1240px;
  width: 100%;
  margin: 0 auto;
}

/* 窄屏：侧栏收成图标栏，保证内容区仍有可用宽度 */
@media (max-width: 960px) {
  .sidebar {
    width: 68px;
    flex-basis: 68px;
  }
  .sidebar__brand-text,
  .nav__label,
  .user__meta {
    display: none;
  }
  .sidebar__brand {
    justify-content: center;
    padding: 18px 0;
  }
  .sidebar__footer {
    flex-direction: column;
    gap: 10px;
  }
  .nav {
    justify-content: center;
    padding: 10px 0;
  }
  .content {
    padding: 16px;
  }
  .topbar {
    padding: 0 16px;
  }
  .topbar__account {
    display: none;
  }
}
</style>
