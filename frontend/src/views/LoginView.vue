<template>
  <!-- 登录页：密码登录 / 手机号验证码登录 双模式 -->
  <div class="auth-page">
    <!-- 左侧品牌区：登录前先告诉用户"这是什么、能做什么" -->
    <aside class="brand-panel">
      <div class="brand-logo">
        <el-icon :size="26"><ChatDotRound /></el-icon>
      </div>
      <h1 class="brand-title">知识库问答系统</h1>
      <p class="brand-desc">
        把商品资料、售后政策、内部文档交给它，<br />
        用自然语言提问，得到带出处的答案。
      </p>
      <ul class="brand-points">
        <li>
          <el-icon><Search /></el-icon>
          <span><b>精准召回</b>：只从你的资料里找答案，不乱编</span>
        </li>
        <li>
          <el-icon><Document /></el-icon>
          <span><b>带出处</b>：每条回答标注来自哪份文档</span>
        </li>
        <li>
          <el-icon><Lock /></el-icon>
          <span><b>数据隔离</b>：各组织资料互不可见</span>
        </li>
      </ul>
    </aside>

    <!-- 右侧登录卡 -->
    <el-card class="auth-card" shadow="never">
      <div class="title">
        <h2>欢迎回来</h2>
      </div>
      <p class="subtitle">登录后即可开始提问</p>

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
            <div class="extra-row">
              <el-button link type="primary" @click="openForgot">忘记密码？</el-button>
            </div>
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

    <!-- ============ 忘记密码：手机号 + 短信验证码 自助重置 ============ -->
    <el-dialog v-model="forgotVisible" title="重置密码" width="420px" @closed="onForgotClosed">
      <p class="forgot-tip">
        请输入<strong>已绑定的手机号</strong>，我们会给该号码发送一条验证码，验证通过后即可设置新密码。
        若忘记当初绑定的号码，请联系管理员重置。
      </p>
      <el-form ref="forgotFormRef" :model="forgotForm" :rules="forgotRules" label-position="top">
        <el-form-item label="手机号" prop="phone">
          <el-input v-model="forgotForm.phone" placeholder="11 位手机号" maxlength="11" :prefix-icon="Iphone" />
        </el-form-item>

        <el-form-item label="图形验证码" prop="captchaCode">
          <div class="captcha-row">
            <el-input v-model="forgotForm.captchaCode" placeholder="图形验证码" maxlength="4" class="captcha-input" />
            <div class="captcha-img" title="看不清？点击换一张" @click="loadForgotCaptcha">
              <img v-if="forgotCaptchaImg" :src="forgotCaptchaImg" alt="验证码" />
              <span v-else class="captcha-loading">加载中…</span>
            </div>
            <el-button text type="primary" :disabled="!forgotCaptchaImg" @click="loadForgotCaptcha">换一张</el-button>
          </div>
        </el-form-item>

        <el-form-item label="短信验证码" prop="code">
          <div class="sms-row">
            <el-input v-model="forgotForm.code" placeholder="短信验证码" maxlength="6" :prefix-icon="Message" />
            <el-button
              class="send-btn"
              :disabled="forgotCountdown > 0 || !forgotCaptchaImg"
              :loading="forgotSending"
              @click="sendForgotCode"
            >
              {{ forgotCountdown > 0 ? `${forgotCountdown}s 后重发` : '获取验证码' }}
            </el-button>
          </div>
        </el-form-item>

        <el-form-item label="新密码" prop="newPassword">
          <el-input
            v-model="forgotForm.newPassword"
            type="password"
            show-password
            placeholder="至少 8 位，需同时含字母和数字"
            :prefix-icon="Lock"
          />
        </el-form-item>

        <el-alert
          v-if="forgotDevCode"
          :title="`测试模式：短信验证码为 ${forgotDevCode}（正式上线后不再显示）`"
          type="info"
          :closable="false"
          show-icon
          class="dev-hint"
        />
      </el-form>

      <template #footer>
        <el-button @click="forgotVisible = false">取消</el-button>
        <el-button type="primary" :loading="forgotLoading" @click="submitForgot">确认重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Iphone, Message, Search, Document, ChatDotRound } from '@element-plus/icons-vue'
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

// ---------- 忘记密码（手机号 + 短信验证码 自助重置）----------
const forgotVisible = ref(false)
const forgotFormRef = ref<FormInstance>()
const forgotLoading = ref(false)
const forgotSending = ref(false)
const forgotForm = reactive({ phone: '', captchaId: '', captchaCode: '', code: '', newPassword: '' })
const forgotCaptchaImg = ref('')
const forgotCountdown = ref(0)
const forgotDevCode = ref('')
let forgotTimer: number | undefined

const forgotRules: FormRules = {
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: phonePattern, message: '手机号格式不正确', trigger: 'blur' },
  ],
  captchaCode: [{ required: true, message: '请先输入图形验证码', trigger: 'blur' }],
  code: [
    { required: true, message: '请输入短信验证码', trigger: 'blur' },
    { min: 4, message: '验证码至少 4 位', trigger: 'blur' },
  ],
  // 前端只做最基本的长度提示；真正的强度规则（含不含用户名等）由后端说了算，
  // 前端不重复实现，避免两边规则不一致——改了后端忘了改前端就会出现"前端说行、后端说不行"
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
}

function openForgot() {
  // 每次打开都清空，避免上次输了一半的手机号/密码留在框里
  forgotForm.phone = ''
  forgotForm.captchaId = ''
  forgotForm.captchaCode = ''
  forgotForm.code = ''
  forgotForm.newPassword = ''
  forgotDevCode.value = ''
  forgotCountdown.value = 0
  if (forgotTimer) { window.clearInterval(forgotTimer); forgotTimer = undefined }
  forgotVisible.value = true
  loadForgotCaptcha()
}

function onForgotClosed() {
  if (forgotTimer) { window.clearInterval(forgotTimer); forgotTimer = undefined }
}

async function loadForgotCaptcha() {
  forgotCaptchaImg.value = ''
  forgotForm.captchaCode = ''
  try {
    const data = await authApi.getCaptcha()
    forgotForm.captchaId = data.captcha_id
    forgotCaptchaImg.value = data.image_base64
  } catch {
    forgotCaptchaImg.value = ''
  }
}

async function sendForgotCode() {
  const valid = await forgotFormRef.value?.validateField(['phone', 'captchaCode']).catch(() => false)
  if (!valid || !forgotForm.captchaId) return
  forgotSending.value = true
  try {
    const res = await authApi.forgotPassword(forgotForm.phone, forgotForm.captchaId, forgotForm.captchaCode)
    forgotDevCode.value = res.dev_code || ''
    // 注意这里用的是后端返回的提示，不是前端自己拼的话术：
    // 后端对"手机号是否注册过"返回的是同一句话，前端照抄即可，不额外加工
    ElMessage.success(res.message)
    startForgotCountdown(res.retry_after || 60)
  } finally {
    forgotSending.value = false
    loadForgotCaptcha() // 图形码已消费，换一张新的
  }
}

function startForgotCountdown(seconds: number) {
  forgotCountdown.value = seconds
  forgotTimer = window.setInterval(() => {
    forgotCountdown.value -= 1
    if (forgotCountdown.value <= 0) {
      window.clearInterval(forgotTimer)
      forgotTimer = undefined
      forgotCountdown.value = 0
    }
  }, 1000)
}

async function submitForgot() {
  const valid = await forgotFormRef.value?.validate().catch(() => false)
  if (!valid) return
  forgotLoading.value = true
  try {
    const res = await authApi.resetPassword(forgotForm.phone, forgotForm.code, forgotForm.newPassword)
    ElMessage.success(res.message)
    forgotVisible.value = false
    // 顺手把手机号带进密码登录框，用户少输一次
    pwdForm.username = ''
    pwdForm.password = ''
  } catch (err: any) {
    if (!err?.response?.data?.detail) ElMessage.error(err?.message || '重置失败')
  } finally {
    forgotLoading.value = false
  }
}

onMounted(() => {
  // 预加载一张图形验证码（切到手机号登录就能直接用）
  loadCaptcha()
})
</script>

<style scoped>
/* 整页：左右分栏 */
.auth-page {
  min-height: 100vh;
  display: flex;
  background: var(--bg-card);
}

/* ---------- 左侧品牌区 ---------- */
.brand-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 0 72px;
  color: #fff;
  /* 品牌渐变：从深蓝到主色，比纯色更有质感 */
  background:
    radial-gradient(circle at 20% 20%, rgba(255, 255, 255, 0.12), transparent 45%),
    linear-gradient(140deg, var(--brand-800) 0%, var(--brand-600) 55%, var(--brand-500) 100%);
  position: relative;
  overflow: hidden;
}

/* 背景装饰圆：让纯色背景不显得空 */
.brand-panel::after {
  content: '';
  position: absolute;
  right: -120px;
  bottom: -120px;
  width: 380px;
  height: 380px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.06);
}

.brand-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 52px;
  height: 52px;
  border-radius: var(--radius-md);
  background: rgba(255, 255, 255, 0.16);
  backdrop-filter: blur(6px);
  margin-bottom: 28px;
  /* 避免遮住 ::after 装饰圆 */
  position: relative;
  z-index: 1;
}

.brand-title {
  margin: 0 0 16px;
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.25;
  position: relative;
  z-index: 1;
}

.brand-desc {
  margin: 0 0 36px;
  font-size: 15px;
  line-height: 1.85;
  color: rgba(255, 255, 255, 0.82);
  position: relative;
  z-index: 1;
}

.brand-points {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
  position: relative;
  z-index: 1;
}

.brand-points li {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 14px;
  color: rgba(255, 255, 255, 0.72);
}

.brand-points li b {
  color: #fff;
  font-weight: 600;
}

.brand-points .el-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, 0.12);
  flex-shrink: 0;
  font-size: 16px;
}

/* ---------- 右侧登录卡 ---------- */
.auth-card {
  width: 480px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  border: none;
  box-shadow: none;
  background: var(--bg-card);
  /* 关键：不让卡片被 flex 拉伸到整高，同时让内部的 body 垂直居中。
     否则表单会顶到屏幕最上面，下面留一大片空白（实际截图看到的问题）。 */
  align-self: center;
  border-radius: 0;
}

.auth-card :deep(.el-card__body) {
  padding: 0 56px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.title {
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 8px;
}

.title h2 {
  margin: 0;
  font-size: 24px;
  font-weight: 700;
  color: var(--text-1);
  letter-spacing: -0.02em;
}

.subtitle {
  text-align: center;
  color: var(--text-4);
  font-size: var(--font-sm);
  margin: 0 0 28px;
}

.submit {
  width: 100%;
  margin-top: 8px;
  height: 44px;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.06em;
}

/* "忘记密码"一行：右对齐，不抢主按钮的视觉重心 */
.extra-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 4px;
}

.forgot-tip {
  margin: 0 0 14px;
  font-size: var(--font-sm);
  line-height: 1.7;
  color: var(--text-3);
}

.footer {
  margin-top: 26px;
  text-align: center;
  font-size: var(--font-sm);
  color: var(--text-4);
}

/* 表单间距更松一点，别挤在一起 */
.auth-card :deep(.el-form-item) {
  margin-bottom: 20px;
}

/* ---------- 窄屏：左侧品牌区收起，只留表单 ----------
   这是移动端"预留空间"的第一步：手机上不显示品牌区，
   避免把表单挤到看不见。 */
@media (max-width: 900px) {
  .brand-panel {
    display: none;
  }
  .auth-card {
    width: 100%;
    min-height: 100vh;
  }
  .auth-card :deep(.el-card__body) {
    padding: 0 32px;
  }
}
</style>
