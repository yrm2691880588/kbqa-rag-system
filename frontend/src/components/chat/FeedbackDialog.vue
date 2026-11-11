<template>
  <!--
    反馈弹窗：用户点了"踩"之后，问清楚"哪里不准确"。

    为什么单独做成弹窗而不是内联展开：
      内联展开会把消息列表撑高、打断阅读节奏；而且选项较多（7 个原因 + 输入框），
      内联会让每条回答下面都挂一大块表单。
      用弹窗的好处：只在用户主动表达"不满意"时才打扰，且能容纳更多选项。

    为什么原因要分类而不是只给一个输入框：
      自由文本没法统计"哪类问题最常出现"。有了分类，
      后台才能回答"最近大家主要抱怨什么"，进而知道该补资料还是该调检索。
      输入框作为补充，用于记录具体细节。
  -->
  <el-dialog
    v-model="visible"
    title="这条回答哪里不准确？"
    width="520px"
    :close-on-click-modal="false"
    @closed="onClosed"
  >
    <el-alert type="info" :closable="false" show-icon class="tip">
      <template #title>
        你的反馈会直接进入改进清单，帮助我们定位是"资料缺失"还是"检索没找到"。匿名收集，只记录原因。
      </template>
    </el-alert>

    <el-form label-width="0">
      <el-form-item>
        <div class="reason-grid">
          <div
            v-for="r in reasonOptions"
            :key="r.value"
            class="reason-item"
            :class="{ active: selected === r.value }"
            @click="selected = r.value"
          >
            {{ r.label }}
          </div>
        </div>
      </el-form-item>

      <el-form-item>
        <el-input
          v-model="note"
          type="textarea"
          :rows="3"
          maxlength="500"
          show-word-limit
          placeholder="补充说明（可选）：具体哪里不对？正确答案应该是什么？"
        />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!selected" @click="submit">
        提交反馈
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * 反馈弹窗组件。
 *
 * 对外接口：
 *   - v-model（visible）：控制显示
 *   - @submit({ reason, note })：提交时回调，由父组件负责调接口
 *
 * 注意：原因列表必须与后端 schemas/chat.py 的 FEEDBACK_REASONS 保持一致。
 * 后端会校验 key，传错了会 422 —— 这是刻意的（防止脏值进数据库）。
 */
import { ref, watch } from 'vue'

/** 与后端 FEEDBACK_REASONS 一一对应。加分类时两边都要改。 */
const reasonOptions = [
  { value: 'inaccurate', label: '回答不准确' },
  { value: 'no_reference', label: '没有引用到相关资料' },
  { value: 'irrelevant', label: '答非所问' },
  { value: 'incomplete', label: '回答不完整' },
  { value: 'wrong_citation', label: '引用错了文档' },
  { value: 'outdated', label: '资料过时' },
  { value: 'other', label: '其他' },
]

const props = defineProps<{
  modelValue: boolean
  submitting?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'submit', payload: { reason: string; note: string }): void
}>()

const visible = ref(props.modelValue)
const selected = ref('')
const note = ref('')

watch(
  () => props.modelValue,
  (v) => {
    visible.value = v
    if (v) {
      // 每次打开都重置：避免上一次选的原因残留在下一次
      selected.value = ''
      note.value = ''
    }
  },
)
watch(visible, (v) => emit('update:modelValue', v))

function submit() {
  if (!selected.value) return
  emit('submit', { reason: selected.value, note: note.value.trim() })
}

/** 弹窗关闭后清空，防止"取消"后内容还留着 */
function onClosed() {
  selected.value = ''
  note.value = ''
}
</script>

<style scoped>
.tip {
  margin-bottom: 16px;
}

.reason-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  width: 100%;
}

.reason-item {
  padding: 9px 12px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: #606266;
  transition: all 0.15s;
  user-select: none;
}

.reason-item:hover {
  border-color: #409eff;
  color: #409eff;
}

.reason-item.active {
  border-color: #409eff;
  background: #ecf5ff;
  color: #409eff;
  font-weight: 500;
}
</style>
