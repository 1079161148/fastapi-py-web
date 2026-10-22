<script setup lang="ts">
/**
 * 登录/注册外壳：左侧品牌区 + 右侧表单。
 *
 * 为什么做成布局组件而不是两页各写一遍：登录与注册必须视觉一致，
 * 复制一份样式意味着以后改主色要改两处，迟早会漏。
 */
const highlights = [
  { icon: 'Reading', title: '知识库驱动', desc: '回答带出处，不凭空编' },
  { icon: 'Connection', title: '多渠道接入', desc: '网页 / 邮件 / 微信 / 钉钉' },
  { icon: 'Service', title: '平滑转人工', desc: '对话上下文完整交给坐席' },
]
</script>

<template>
  <div class="auth">
    <aside class="auth__brand">
      <div class="auth__brand-inner">
        <div class="auth__logo">
          <span>AI</span>
        </div>
        <h1 class="auth__headline">把知识库变成<br />会回答问题的客服</h1>
        <p class="auth__lede">
          企业级智能客服系统：检索增强回答、多渠道接入、人工接管与用量成本观测。
        </p>

        <ul class="auth__points">
          <li v-for="item in highlights" :key="item.title">
            <span class="auth__point-icon">
              <el-icon><component :is="item.icon" /></el-icon>
            </span>
            <span class="auth__point-text">
              <strong>{{ item.title }}</strong>
              <small>{{ item.desc }}</small>
            </span>
          </li>
        </ul>
      </div>
      <p class="auth__copyright">© 2026 智能客服 AI 系统</p>
    </aside>

    <main class="auth__main">
      <div class="auth__card">
        <slot />
      </div>
    </main>
  </div>
</template>

<style scoped>
.auth {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  min-height: 100vh;
  background: var(--app-bg);
}

/* ---------- 品牌区 ---------- */
.auth__brand {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 48px 56px;
  background:
    radial-gradient(90% 70% at 15% 10%, rgba(37, 99, 235, 0.32), transparent 60%),
    radial-gradient(70% 60% at 90% 90%, rgba(124, 58, 237, 0.24), transparent 60%),
    linear-gradient(160deg, #0b1220, #131c30 55%, #0b1220);
  color: #e2e8f0;
}

.auth__brand-inner {
  max-width: 460px;
}

.auth__logo {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 12px;
  background: linear-gradient(135deg, #2563eb, #7c3aed);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.auth__headline {
  margin: 32px 0 0;
  font-size: 34px;
  font-weight: 650;
  letter-spacing: -0.03em;
  line-height: 1.28;
}

.auth__lede {
  margin: 16px 0 0;
  color: #94a3b8;
  font-size: 14px;
  line-height: 1.7;
}

.auth__points {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 36px 0 0;
  padding: 0;
  list-style: none;
}

.auth__points li {
  display: flex;
  align-items: center;
  gap: 12px;
}

.auth__point-icon {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  place-items: center;
  border: 1px solid rgba(148, 163, 184, 0.22);
  border-radius: 9px;
  background: rgba(148, 163, 184, 0.08);
  color: #93c5fd;
  font-size: 15px;
}

.auth__point-text {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}
.auth__point-text strong {
  font-size: 13.5px;
  font-weight: 550;
}
.auth__point-text small {
  color: #64748b;
  font-size: 12px;
}

.auth__copyright {
  margin: 24px 0 0;
  color: #475569;
  font-size: 11.5px;
}

/* ---------- 表单区 ---------- */
.auth__main {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 24px;
  background: var(--app-surface);
}

.auth__card {
  width: 100%;
  max-width: 380px;
}

@media (max-width: 900px) {
  .auth {
    grid-template-columns: minmax(0, 1fr);
  }
  /* 窄屏隐藏品牌区：表单是唯一任务，装饰性内容不该占首屏 */
  .auth__brand {
    display: none;
  }
}
</style>
