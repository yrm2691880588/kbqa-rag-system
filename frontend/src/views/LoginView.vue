<template>
  <!-- 登录页 -->
  <div class="auth-page">
    <el-card class="auth-card">
      <div class="title">
        <el-icon :size="26" color="#409eff"><ChatDotRound /></el-icon>
        <h2>企业知识库问答系统</h2>
      </div>
      <p class="subtitle">电商商品资料智能问答 · 请登录后使用</p>

      <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="submit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名" :prefix-icon="User" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="密码" :prefix-icon="Lock" />
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" @click="submit">登 录</el-button>
      </el-form>

      <div class="footer">
        <span>还没有账号？</span>
        <router-link to="/register">立即注册</router-link>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功，正在跳转...')
    // 登录前想去的页面（如管理页）优先跳回；没有就回问答主页
    const redirect = (route.query.redirect as string) || '/chat'
    // 用整页跳转代替 SPA 路由跳转：100% 可靠，绕开懒加载/HMR 等任何诡异时序问题
    // 整页刷新会让路由守卫自然地重新跑一遍（有 token，没 user → fetchMe → 拿到用户信息 → 渲染主界面）
    setTimeout(() => {
      window.location.href = redirect
    }, 300) // 留 300ms 让 "登录成功" 提示能看见
  } catch (err: any) {
    // 兜底：万一 axios 拦截器没拦住
    ElMessage.error(err?.response?.data?.detail || err?.message || '登录失败')
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
  background: linear-gradient(135deg, #eaf3ff 0%, #f5f7fa 100%);
}

.auth-card {
  width: 400px;
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
