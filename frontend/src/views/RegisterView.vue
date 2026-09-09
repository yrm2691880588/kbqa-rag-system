<template>
  <!-- 注册页：用户名 + 密码(带强度校验) + 图形验证码（防机器人批量注册） -->
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
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="密码（至少 8 位，含字母和数字）"
            :prefix-icon="Lock"
            @input="onPasswordInput"
          />
          <!-- 实时密码强度提示条 -->
          <div v-if="form.password" class="strength-row">
            <el-progress
              :percentage="strengthPercent"
              :color="strengthColor"
              :show-text="false"
              :stroke-width="6"
              class="strength-bar"
            />
            <span class="strength-text" :style="{ color: strengthColor }">{{ strengthText }}</span>
          </div>
        </el-form-item>
        <el-form-item prop="confirm">
          <el-input v-model="form.confirm" type="password" show-password placeholder="确认密码" :prefix-icon="Lock" />
        </el-form-item>

        <!-- 人机图形验证码：答对才能提交注册（防脚本批量刷号） -->
        <el-form-item prop="captchaCode">
          <div class="captcha-row">
            <el-input v-model="form.captchaCode" placeholder="图形验证码" maxlength="4" class="captcha-input" />
            <div class="captcha-img" title="看不清？点击换一张" @click="loadCaptcha">
              <img v-if="captchaImg" :src="captchaImg" alt="验证码" />
              <span v-else class="captcha-loading">加载中…</span>
            </div>
            <el-button text type="primary" :disabled="!captchaImg" @click="loadCaptcha">换一张</el-button>
          </div>
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
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Postcard, UserFilled } from '@element-plus/icons-vue'
import * as authApi from '@/api/auth'
import { checkPassword } from '@/utils/password'

const router = useRouter()
const formRef = ref<FormInstance>()
const loading = ref(false)
const captchaImg = ref('')
const form = reactive({ username: '', nickname: '', password: '', confirm: '', captchaId: '', captchaCode: '' })

// 密码强度即时反馈
const pwCheck = computed(() => checkPassword(form.password, form.username))
const strengthPercent = computed(() => {
  if (!pwCheck.value.ok) return 30
  return pwCheck.value.level === 'strong' ? 100 : 66
})
const strengthColor = computed(() => {
  if (!pwCheck.value.ok) return '#f56c6c'
  return pwCheck.value.level === 'strong' ? '#67c23a' : '#e6a23c'
})
const strengthText = computed(() => {
  if (!form.password) return ''
  return pwCheck.value.ok ? pwCheck.value.reason : `还需改进：${pwCheck.value.reason}`
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[a-zA-Z0-9_]{3,32}$/, message: '3-32位字母/数字/下划线', trigger: 'blur' },
  ],
  nickname: [{ max: 32, message: '昵称最长 32 字', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    {
      validator: (_r, v: string, cb) => {
        const c = checkPassword(v || '', form.username)
        if (!c.ok) cb(new Error(c.reason))
        else cb()
      },
      trigger: 'blur',
    },
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
  captchaCode: [{ required: true, message: '请先输入图形验证码', trigger: 'blur' }],
}

/** 密码输入时实时刷新强度条 */
function onPasswordInput() {
  pwCheck.value
}

/** 拉取一张新的图形验证码 */
async function loadCaptcha() {
  captchaImg.value = ''
  form.captchaCode = ''
  try {
    const data = await authApi.getCaptcha()
    form.captchaId = data.captcha_id
    captchaImg.value = data.image_base64
  } catch {
    captchaImg.value = ''
  }
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await authApi.register(form.username, form.password, form.nickname, form.captchaId, form.captchaCode)
    ElMessage.success('注册成功，请登录')
    router.push('/login')
  } catch {
    // 图形码错误或已过期等错误由 axios 拦截器统一提示；顺手换张新码
    loadCaptcha()
  } finally {
    loading.value = false
  }
}

onMounted(loadCaptcha)
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
  width: 440px;
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
  margin: 8px 0 18px;
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

/* 密码强度条 */
.strength-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  margin-top: 6px;
}

.strength-bar {
  flex: 1;
}

.strength-text {
  font-size: 12px;
  white-space: nowrap;
}

/* 图形验证码一行 */
.captcha-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.captcha-input {
  width: 110px;
  flex: none;
}

.captcha-img {
  width: 108px;
  height: 38px;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  overflow: hidden;
  cursor: pointer;
  flex: none;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
}

.captcha-img img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.captcha-loading {
  font-size: 12px;
  color: #909399;
}
</style>
