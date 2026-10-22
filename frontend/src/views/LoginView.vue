<script setup lang="ts">
/**
 * 登录页（设计文档 M1）。
 * 错误提示由 axios 拦截器统一弹出，这里只处理跳转。
 */
import { ElMessage } from 'element-plus'
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { FormInstance, FormRules } from 'element-plus'

import AuthLayout from '@/layouts/AuthLayout.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({
  identifier: '',
  tenant: '',
  password: '',
})

const rules: FormRules = {
  identifier: [{ required: true, message: '请输入邮箱或用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

/** 用户名登录才需要租户标识；邮箱全局唯一，不需要 */
const needsTenant = computed(() => !form.identifier.includes('@'))

async function submit() {
  // 前端只做提示，不替代后端校验（后端缺 tenant 会返回 400 TENANT_REQUIRED）
  if (needsTenant.value && !form.tenant.trim()) {
    ElMessage.warning('使用用户名登录时，请填写组织标识（租户 slug）')
    return
  }
  loading.value = true
  try {
    await auth.login({
      identifier: form.identifier.trim(),
      tenant: form.tenant.trim() || undefined,
      password: form.password,
    })
    const redirect = (route.query.redirect as string) || '/'
    await router.replace(redirect)
  } finally {
    loading.value = false
  }
}

function onSubmit() {
  // validate 返回 Promise（即使传入回调），需显式 void；
  // 回调保持同步，异步逻辑在 submit() 内 await
  void formRef.value?.validate((valid) => {
    if (valid) void submit()
  })
}
</script>

<template>
  <AuthLayout>
    <h2 class="auth-title">登录</h2>
    <p class="auth-sub">使用邮箱或用户名继续</p>

    <el-form
      ref="formRef"
      :model="form"
      :rules="rules"
      label-position="top"
      size="large"
      @keyup.enter="onSubmit"
    >
      <el-form-item label="邮箱 / 用户名" prop="identifier">
        <el-input
          v-model="form.identifier"
          placeholder="you@example.com 或 用户名"
          autocomplete="username"
        />
      </el-form-item>
      <el-form-item v-if="needsTenant" label="组织标识" prop="tenant">
        <el-input v-model="form.tenant" placeholder="租户 slug，见管理后台「租户信息」" />
      </el-form-item>
      <el-form-item label="密码" prop="password">
        <el-input
          v-model="form.password"
          type="password"
          show-password
          placeholder="请输入密码"
          autocomplete="current-password"
        />
      </el-form-item>
      <el-button class="auth-submit" type="primary" :loading="loading" @click="onSubmit">
        登录
      </el-button>
    </el-form>

    <p class="auth-foot">
      还没有账号？<router-link to="/register">注册（自动创建租户）</router-link>
    </p>
  </AuthLayout>
</template>
