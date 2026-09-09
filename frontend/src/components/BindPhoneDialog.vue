<template>
  <!-- 绑定手机号弹窗：绑定后该手机号可用短信验证码登录（同一账号） -->
  <el-dialog v-model="visible" title="绑定手机号" width="420px" destroy-on-close @closed="onClosed">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="绑定后，可用「手机号+短信验证码」登录本账号，与用户名密码登录是同一个账号。"
      class="tip"
    />

    <el-form ref="formRef" :model="form" :rules="rules" size="large">
      <el-form-item prop="phone">
        <el-input v-model="form.phone" placeholder="输入要绑定的手机号" maxlength="11" :prefix-icon="Iphone" />
      </el-form-item>

      <!-- 人机图形验证码 -->
      <el-form-item prop="captchaCode">
        <div class="captcha-row">
          <el-input v-model="form.captchaCode" placeholder="图形验证码" maxlength="4" class="captcha-input" />
          <div class="captcha-img" title="看不清？点击换一张" @click="loadCaptcha">
            <img v-if="captchaImg" :src="captchaImg" alt="验证码" />
            <span v-else>加载中…</span>
          </div>
          <el-button text type="primary" :disabled="!captchaImg" @click="loadCaptcha">换一张</el-button>
        </div>
      </el-form-item>

      <el-form-item prop="smsCode">
        <div class="sms-row">
          <el-input v-model="form.smsCode" placeholder="短信验证码" maxlength="6" :prefix-icon="Message" />
          <el-button class="send-btn" :disabled="countdown > 0 || !captchaImg" @click="sendCode">
            {{ countdown > 0 ? `${countdown}s 后重发` : '获取验证码' }}
          </el-button>
        </div>
      </el-form-item>

      <el-alert v-if="devCodeHint" :title="devCodeHint" type="info" :closable="false" show-icon class="tip" />
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="submit">确认绑定</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Iphone, Message } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import * as authApi from '@/api/auth'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean]; bound: [] }>()

const auth = useAuthStore()
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const formRef = ref<FormInstance>()
const loading = ref(false)
const captchaImg = ref('')
const countdown = ref(0)
const devCodeHint = ref('')
let countdownTimer: number | undefined

const form = reactive({ phone: '', captchaId: '', captchaCode: '', smsCode: '' })
const phonePattern = /^1[3-9]\d{9}$/

const rules: FormRules = {
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: phonePattern, message: '手机号格式不正确', trigger: 'blur' },
  ],
  captchaCode: [{ required: true, message: '请先输入图形验证码', trigger: 'blur' }],
  smsCode: [{ required: true, message: '请输入短信验证码', trigger: 'blur' }],
}

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

async function sendCode() {
  const valid = await formRef.value?.validateField(['phone', 'captchaCode']).catch(() => false)
  if (!valid || !form.captchaId) return
  try {
    const res = await authApi.sendSmsCode(form.phone, form.captchaId, form.captchaCode)
    devCodeHint.value = res.dev_code ? `测试模式：短信验证码为 ${res.dev_code}（正式上线后不再显示）` : ''
    ElMessage.success(res.message)
    countdown.value = res.retry_after || 60
    countdownTimer = window.setInterval(() => {
      countdown.value -= 1
      if (countdown.value <= 0) {
        window.clearInterval(countdownTimer)
        countdownTimer = undefined
      }
    }, 1000)
  } finally {
    loadCaptcha()
  }
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await authApi.bindPhone(form.phone, form.smsCode)
    // 绑定成功：把手机号同步到本地用户信息（无需重新登录）
    if (auth.user) auth.user.phone = form.phone
    ElMessage.success('绑定成功，以后可用手机号验证码登录')
    visible.value = false
    emit('bound')
  } catch {
    loadCaptcha()
  } finally {
    loading.value = false
  }
}

function onClosed() {
  // 弹窗关闭时清理倒计时
  if (countdownTimer) window.clearInterval(countdownTimer)
  countdownTimer = undefined
  countdown.value = 0
}

onMounted(loadCaptcha)
</script>

<style scoped>
.tip {
  margin-bottom: 14px;
}

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
  font-size: 12px;
  color: #909399;
}

.captcha-img img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.sms-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.send-btn {
  width: 130px;
  flex: none;
}
</style>
