<template>
  <!-- 修改密码弹窗：验证旧密码 → 设置新密码 -->
  <el-dialog v-model="visible" title="修改密码" width="420px" destroy-on-close>
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="旧密码" prop="old_password">
        <el-input v-model="form.old_password" type="password" show-password placeholder="输入当前密码" />
      </el-form-item>
      <el-form-item label="新密码" prop="new_password">
        <el-input v-model="form.new_password" type="password" show-password placeholder="至少 6 位" />
      </el-form-item>
      <el-form-item label="确认新密码" prop="confirm">
        <el-input v-model="form.confirm" type="password" show-password placeholder="再次输入新密码" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="submit">确认修改</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const auth = useAuthStore()
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ old_password: '', new_password: '', confirm: '' })

const rules: FormRules = {
  old_password: [{ required: true, message: '请输入旧密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, message: '新密码至少 6 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_r, v, cb) => {
        if (v !== form.new_password) cb(new Error('两次输入的新密码不一致'))
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
    await auth.changePassword(form.old_password, form.new_password)
    ElMessage.success('密码修改成功')
    visible.value = false
    // 清空表单，避免下次打开残留
    form.old_password = ''
    form.new_password = ''
    form.confirm = ''
  } finally {
    loading.value = false
  }
}
</script>
