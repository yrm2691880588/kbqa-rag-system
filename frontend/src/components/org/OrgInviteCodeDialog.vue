<template>
  <!--
    邀请码展示弹窗：创建组织后 / 重置邀请码后各出现一次。

    为什么单独拆成组件：
      这是一个"只出现一次、必须让用户复制走"的强提示场景，
      关掉就再也查不到（只能重置）。拆出来能把这个约束写在一个文件里，
      不容易被别处的改动破坏。
  -->
  <el-dialog
    v-model="visible"
    title="组织邀请码"
    width="520px"
    :close-on-click-modal="false"
  >
    <el-alert type="warning" :closable="false" show-icon class="code-alert">
      <template #title>
        请立刻复制并保存。这个码是让同事加入组织的凭证，页面关掉后就查不到了
        （需要时只能"重置邀请码"，重置后旧码立即失效）。
      </template>
    </el-alert>
    <div class="code-box">
      <span class="code-text">{{ code }}</span>
      <el-button type="primary" plain size="small" @click="copyCode">复制</el-button>
    </div>
    <div class="code-org">所属组织：{{ orgName }}</div>
    <template #footer>
      <el-button type="primary" @click="visible = false">我已保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps<{
  modelValue: boolean
  code: string
  orgName: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
}>()

const visible = ref(props.modelValue)
watch(
  () => props.modelValue,
  (v) => {
    visible.value = v
  },
)
watch(visible, (v) => emit('update:modelValue', v))

async function copyCode() {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(props.code)
      ElMessage.success('已复制到剪贴板')
      return
    }
    throw new Error('no clipboard api')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选中上面的邀请码复制')
  }
}
</script>

<style scoped>
.code-alert {
  margin-bottom: 16px;
}

.code-box {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.code-text {
  font-family: monospace;
  font-size: 15px;
  letter-spacing: 1px;
  flex: 1;
  word-break: break-all;
}

.code-org {
  color: var(--text-4);
  font-size: 13px;
}
</style>
