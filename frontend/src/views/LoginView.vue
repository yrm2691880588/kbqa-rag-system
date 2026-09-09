<template>
  <!-- 登录页：密码登录 / 手机号验证码登录 双模式 -->
  <div class="auth-page">
    <el-card class="auth-card">
      <div class="title">
        <el-icon :size="26" color="#409eff"><ChatDotRound /></el-icon>
        <h2>企业知识库问答系统</h2>
      </div>
      <p class="subtitle">电商商品资料智能问答 · 请登录后使用</p>

      <el-tabs v-model="tab" stretch>
        <!-- ============ 方式一：账号密码登录 ============ -->
        <el-tab-pane label="密码登录" name="password">
          <el-form ref="pwdFormRef" :model="pwdForm" :rules="pwdRules" size="large" @keyup.enter="submitPwd">
            <el-form-item prop="username">
              <el-input v-model="pwdForm.username" placeholder="用户名" :prefix-icon="User" />
            </el-form-item>
            <el-form-item prop="password">
              <el-input v-model="pwdForm.password" type="password" show-password placeholder="密码" :prefix-icon="Lock" />
            </el-form-item>
            <el-button type="primary" class="submit" :loading="pwdLoading" @click="submitPwd">登 录</el-button>
          </el-form>
        </el-tab-pane>

        <!-- ============ 方式二：手机号验证码登录 ============ -->
        <el-tab-pane label="手机号登录" name="sms">
          <el-form ref="smsFormRef" :model="smsForm" :rules="smsRules" size="large">
            <el-form-item prop="phone">
              <el-input v-model="smsForm.phone" placeholder="手机号（未注册将自动创建账号）" :prefix-icon="Iphone" maxlength="11" />
            </el-form-item>

            <!-- 人机图形验证码：答对才能发短信（防短信轰炸） -->
            <el-form-item prop="captchaCode">
              <div class="captcha-row">
                <el-input v-model="smsForm.captchaCode" placeholder="图形验证码" maxlength="4" class="captcha-input" />
                <div class="captcha-img" title="看不清？点击换一张" @click="loadCaptcha">
                  <img v-if="captchaImg" :src="captchaImg" alt="验证码" />
                  <span v-else class="captcha-loading">加载中…</span>
                </div>
                <el-button text type="primary" :disabled="!captchaImg" @click="loadCaptcha">换一张</el-button>
              </div>
            </el-form-item>

            <el-form-item prop="smsCode">
              <div class="sms-row">
                <el-input v-model="smsForm.smsCode" placeholder="短信验证码" maxlength="6" :prefix-icon="Message" />
                <el-button
                  class="send-btn"
                  :disabled="countdown > 0 || !captchaImg"
                  @click="sendCode"
                >
                  {{ countdown > 0 ? `${countdown}s 后重发` : '获取验证码' }}
                </el-button>
              </div>
            </el-form-item>

            <!-- 测试模式提示：后端 SMS_PROVIDER=mock 时返回的万能验证码 -->
            <el-alert
              v-if="devCodeHint"
              :title="devCodeHint"
              type="info"
              :closable="false"
              show-icon
              class="dev-hint"
            />

            <el-button type="primary" class="submit" :loading="smsLoading" @click="submitSms">登录 / 注册</el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>

      <div class="footer">
        <span>还没有账号？</span>
        <router-link to="/register">立即注册</router-link>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Iphone, Message } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import * as authApi from '@/api/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const tab = ref<'password' | 'sms'>('password')

// ---------- 密码登录 ----------
const pwdFormRef = ref<FormInstance>()
const pwdLoading = ref(false)
const pwdForm = reactive({ username: '', password: '' })

const pwdRules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function submitPwd() {
  const valid = await pwdFormRef.value?.validate().catch(() => false)
  if (!valid) return
  pwdLoading.value = true
  try {
    await auth.login(pwdForm.username, pwdForm.password)
    ElMessage.success('登录成功，正在跳转...')
    redirectTo()
  } catch (err: any) {
    // 兜底：错误提示由 axios 拦截器统一弹出
    if (!err?.response?.data?.detail) ElMessage.error(err?.message || '登录失败')
  } finally {
    pwdLoading.value = false
  }
}

// ---------- 手机号验证码登录 ----------
const smsFormRef = ref<FormInstance>()
const smsLoading = ref(false)
const smsForm = reactive({ phone: '', captchaId: '', captchaCode: '', smsCode: '' })
const captchaImg = ref('')
const countdown = ref(0)
const devCodeHint = ref('')
let countdownTimer: number | undefined
let lastDevCode = '' // 记住测试验证码，防用户没看清提示

const phonePattern = /^1[3-9]\d{9}$/

const smsRules: FormRules = {
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: phonePattern, message: '手机号格式不正确', trigger: 'blur' },
  ],
  captchaCode: [{ required: true, message: '请先输入图形验证码', trigger: 'blur' }],
  smsCode: [{ required: true, message: '请输入短信验证码', trigger: 'blur' }],
}

/** 拉取一张新的图形验证码（注册/发短信前须答对） */
async function loadCaptcha() {
  captchaImg.value = ''
  smsForm.captchaCode = ''
  try {
    const data = await authApi.getCaptcha()
    smsForm.captchaId = data.captcha_id
    captchaImg.value = data.image_base64
  } catch {
    captchaImg.value = ''
  }
}

/** 发送短信验证码：先提交手机号+图形码，通过后开始倒计时 */
async function sendCode() {
  const valid = await smsFormRef.value?.validateField(['phone', 'captchaCode']).catch(() => false)
  if (!valid || !smsForm.captchaId) return
  try {
    const res = await authApi.sendSmsCode(smsForm.phone, smsForm.captchaId, smsForm.captchaCode)
    lastDevCode = res.dev_code || ''
    devCodeHint.value = lastDevCode ? `测试模式：短信验证码为 ${lastDevCode}（正式上线后不再显示）` : ''
    ElMessage.success(res.message)
    startCountdown(res.retry_after || 60)
  } finally {
    loadCaptcha() // 图形码已消费，换一张新的
  }
}

function startCountdown(seconds: number) {
  countdown.value = seconds
  countdownTimer = window.setInterval(() => {
    countdown.value -= 1
    if (countdown.value <= 0) {
      window.clearInterval(countdownTimer)
      countdownTimer = undefined
      countdown.value = 0
    }
  }, 1000)
}

async function submitSms() {
  const valid = await smsFormRef.value?.validate().catch(() => false)
  if (!valid) return
  smsLoading.value = true
  try {
    await auth.loginBySms(smsForm.phone, smsForm.smsCode)
    ElMessage.success('登录成功，正在跳转...')
    redirectTo()
  } catch (err: any) {
    if (!err?.response?.data?.detail) ElMessage.error(err?.message || '登录失败')
  } finally {
    smsLoading.value = false
  }
}

/** 登录成功后的统一跳转（去登录前想去的页面，否则回问答主页） */
function redirectTo() {
  const redirect = (route.query.redirect as string) || '/chat'
  setTimeout(() => {
    window.location.href = redirect
  }, 300)
}

onMounted(() => {
  // 预加载一张图形验证码（切到手机号登录就能直接用）
  loadCaptcha()
})
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

/* 短信验证码一行 */
.sms-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.send-btn {
  width: 130px;
  flex: none;
}

.dev-hint {
  margin-bottom: 12px;
}
</style>
