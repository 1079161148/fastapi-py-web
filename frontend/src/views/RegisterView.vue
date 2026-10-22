<script setup lang="ts">
/**
 * 注册页（设计文档 M1）：注册即开户，自动创建租户并成为属主。
 */
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { FormInstance, FormRules } from 'element-plus'

import AuthLayout from '@/layouts/AuthLayout.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({
  email: '',
  username: '',
  password: '',
  confirmPassword: '',
  tenant_name: '',
})

const rules: FormRules = {
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  // 用户名可选；留空则该账号只能用邮箱登录
  username: [
    {
      validator: (_rule, value, callback) => {
        const raw = ((value as string) || '').trim()
        if (raw && !/^[A-Za-z0-9_]{3,32}$/.test(raw)) {
          callback(new Error('3-32 位字母、数字或下划线'))
          return
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, max: 72, message: '密码长度 8-72 位', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== form.password) {
          callback(new Error('两次输入的密码不一致'))
          return
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
  tenant_name: [{ required: true, message: '请输入租户名称', trigger: 'blur' }],
}

async function submit() {
  loading.value = true
  try {
    await auth.register({
      email: form.email,
      username: form.username.trim() || undefined,
      password: form.password,
      tenant_name: form.tenant_name,
    })
    await router.replace('/')
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
    <h2 class="auth-title">创建账号</h2>
    <p class="auth-sub">注册即开户，你将成为该租户的所有者</p>

    <el-form
      ref="formRef"
      :model="form"
      :rules="rules"
      label-position="top"
      size="large"
      @keyup.enter="onSubmit"
    >
      <el-form-item label="邮箱" prop="email">
        <el-input v-model="form.email" placeholder="you@example.com" autocomplete="username" />
      </el-form-item>
      <el-form-item label="用户名（可选）" prop="username">
        <el-input
          v-model="form.username"
          placeholder="zhangsan；留空则只能用邮箱登录"
          autocomplete="off"
        />
      </el-form-item>
      <el-form-item label="密码" prop="password">
        <el-input
          v-model="form.password"
          type="password"
          show-password
          autocomplete="new-password"
        />
      </el-form-item>
      <el-form-item label="确认密码" prop="confirmPassword">
        <el-input
          v-model="form.confirmPassword"
          type="password"
          show-password
          autocomplete="new-password"
        />
      </el-form-item>
      <el-form-item label="租户名称" prop="tenant_name">
        <el-input v-model="form.tenant_name" placeholder="你的团队或公司名称" />
      </el-form-item>
      <el-button class="auth-submit" type="primary" :loading="loading" @click="onSubmit">
        注册并登录
      </el-button>
    </el-form>

    <p class="auth-foot">
      已有账号？<router-link to="/login">直接登录</router-link>
    </p>
  </AuthLayout>
</template>
