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

        <!-- 组织邀请码：决定你注册后进入哪家公司（组织）的工作空间 -->
        <el-form-item prop="inviteCode">
          <el-input
            v-model="form.inviteCode"
            :placeholder="invitePlaceholder"
            :prefix-icon="Ticket"
            maxlength="32"
          />
          <div class="field-hint">{{ inviteHint }}</div>
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
import { User, Lock, Postcard, UserFilled, Ticket } from '@element-plus/icons-vue'
import * as authApi from '@/api/auth'
import { checkPassword } from '@/utils/password'

const router = useRouter()
const formRef = ref<FormInstance>()
const loading = ref(false)
const captchaImg = ref('')
// 注册策略：由后端告知"邀请码是否必填"（私有部署选填 / 对外 SaaS 必填）
const inviteRequired = ref(false)
const form = reactive({
  username: '',
  nickname: '',
  inviteCode: '',
  password: '',
  confirm: '',
  captchaId: '',
  captchaCode: '',
})

// 邀请码输入框的提示：随后端策略变化，避免用户"填不填都心慌"
const invitePlaceholder = computed(() =>
  inviteRequired.value ? '组织邀请码（必填）' : '组织邀请码（选填，没有可留空）',
)
const inviteHint = computed(() =>
  inviteRequired.value
    ? '本系统需要邀请码才能注册，请向你的管理员索取'
    : '如果你是被公司/团队邀请使用的，请填写管理员提供的邀请码；个人试用可留空',
)

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
  inviteCode: [
    {
      // 只有后端明确要求时才强制填写（私有部署下留空是合法的）
      validator: (_r, v: string, cb) => {
        if (inviteRequired.value && !String(v || '').trim()) {
          cb(new Error('本系统需要组织邀请码，请向管理员索取'))
        } else {
          cb()
        }
      },
      trigger: 'blur',
    },
  ],
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
    await authApi.register(
      form.username,
      form.password,
      form.nickname,
      form.captchaId,
      form.captchaCode,
      form.inviteCode.trim(),
    )
    ElMessage.success('注册成功，请登录')
    router.push('/login')
  } catch {
    // 图形码错误或已过期等错误由 axios 拦截器统一提示；顺手换张新码
    loadCaptcha()
  } finally {
    loading.value = false
  }
}

/** 拉取注册策略：邀请码选填还是必填（拿不到就按"选填"处理，不影响注册） */
async function loadRegisterPolicy() {
  try {
    const p = await authApi.getRegisterPolicy()
    inviteRequired.value = !!p.invite_required
  } catch {
    inviteRequired.value = false
  }
}

onMounted(() => {
  loadCaptcha()
  loadRegisterPolicy()
})
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

/* 邀请码下方的说明文字 */
.field-hint {
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
  margin-top: 4px;
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
