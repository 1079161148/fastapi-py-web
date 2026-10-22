import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus, { ElMessage } from 'element-plus'
import 'element-plus/dist/index.css'
// 主题必须在 Element 样式之后引入：靠覆盖 CSS 变量生效
import './styles/theme.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import App from './App.vue'
import { setUnauthorizedHandler } from './api/client'
import router from './router'
import { useAuthStore } from './stores/auth'

const app = createApp(App)
const pinia = createPinia()

// Element Plus 图标全局注册：模板里直接用 <el-icon><Edit /></el-icon>
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

app.use(pinia)
app.use(router)
app.use(ElementPlus)

/**
 * 401 不能只清 localStorage 里的 token：内存中的 user 还在，isLoggedIn 仍为 true，
 * 守卫继续放行，用户会停在失效的页面上，直到手动刷新走 restore() 才被送去登录页。
 * 这里把状态重置与跳转接上，client 层只负责判定与去重。
 */
setUnauthorizedHandler(() => {
  const auth = useAuthStore(pinia)
  auth.reset()

  const current = router.currentRoute.value
  // 已在登录页（如刷新后 restore 失败）不重复跳转，避免提示/路由反复触发
  if (current.name === 'login') return
  ElMessage.warning('登录已过期，请重新登录')
  void router.push({ path: '/login', query: { redirect: current.fullPath } })
})

app.mount('#app')
