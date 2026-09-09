<template>
  <!-- 注册页 -->
  <div class="auth-page">
    <el-card class="auth-card">
      <div class="title">
        <el-icon :size="26" color="#67c23a"><UserFilled /></el-icon>
        <h2>注册新账号</h2>
      </div>
      <p class="subtitle">注册后即可使用知识库问答功能</p>

      <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="submit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名（3-32位字母/数字/下划线）" :prefix-icon="User" />
        </el-form-item>
        <el-form-item prop="nickname">
          <el-input v-model="form.nickname" placeholder="昵称（可选，默认同用户名）" :prefix-icon="Postcard" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="密码（至少 6 位）" :prefix-icon="Lock" />
        </el-form-item>
        <el-form-item prop="confirm">
          <el-input v-model="form.confirm" type="password" show-password placeholder="确认密码" :prefix-icon="Lock" />
        </el-form-item>
        <el-button type="success" class="submit" :loading="loading" @click="submit">注 册</el-button>
      </el-form>

      <div class="footer">
        <span>已有账号？</span>
        <router-link to="/login">去登录</router-link>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Postcard, UserFilled } from '@element-plus/icons-vue'
import * as authApi from '@/api/auth'

const router = useRouter()
const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', nickname: '', password: '', confirm: '' })

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[a-zA-Z0-9_]{3,32}$/, message: '3-32位字母/数字/下划线', trigger: 'blur' },
  ],
  nickname: [{ max: 32, message: '昵称最长 32 字', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 64, message: '密码长度 6-64 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    {
      validator: (_r, v, cb) => {
        if (v !== form.password) cb(new Error('两次输入的密码不一致'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await authApi.register(form.username, form.password, form.nickname)
    ElMessage.success('注册成功，请登录')
    router.push('/login')
  } catch {
    /* 错误提示由 axios 拦截器统一弹出 */
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.auth-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #f0f9eb 0%, #f5f7fa 100%);
}

.auth-card {
  width: 420px;
  padding: 12px 8px;
}

.title {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
}

.title h2 {
  margin: 0;
  font-size: 20px;
  color: #303133;
}

.subtitle {
  text-align: center;
  color: #909399;
  font-size: 13px;
  margin: 8px 0 22px;
}

.submit {
  width: 100%;
  margin-top: 6px;
}

.footer {
  margin-top: 18px;
  text-align: center;
  font-size: 14px;
  color: #909399;
}

.footer a {
  color: #409eff;
  text-decoration: none;
}
</style>
