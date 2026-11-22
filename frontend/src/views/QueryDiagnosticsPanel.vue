<template>
  <section class="diagnostics-panel" data-testid="query-diagnostics">
    <header class="diag-head">
      <div>
        <h3>慢查询诊断与复跑</h3>
        <p class="page-desc">
          从慢查询日志打包执行模板、取数口径和耗时数据；下载后在交接窗口选中一笔慢查，
          绕过缓存复跑并核对新旧口径。
        </p>
      </div>
      <div class="page-actions">
        <label class="diag-inline">
          <span>操作人</span>
          <input v-model="operator" data-testid="diag-operator" placeholder="接班人" />
        </label>
        <button class="btn" data-testid="diag-create" type="button" :disabled="loading" @click="createFile">
          登记诊断文件
        </button>
        <button class="btn" data-testid="diag-reload" type="button" :disabled="loading" @click="reloadAll">
          刷新运行日志
        </button>
      </div>
    </header>

    <p v-if="message" class="diag-message" data-testid="diag-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text diag-message" data-testid="diag-error">{{ errorMessage }}</p>

    <article class="diag-card diag-exec">
      <div class="diag-subhead">
        <h4>查询构建器执行入口</h4>
        <span class="page-desc">同一指标的两个模板可分别触发冷执行与缓存命中，再直接核对下方运行日志。</span>
      </div>
      <form class="diag-form" @submit.prevent>
        <label>
          <span>查询模板</span>
          <select v-model.number="selectedTemplateId" data-testid="diag-template">
            <option :value="0" disabled>选择已发布模板</option>
            <option v-for="template in templates" :key="template.id" :value="template.id">
              {{ template.模板名称 }}（{{ template.关联指标 }} / {{ template.缓存策略 || '默认缓存' }}）
            </option>
          </select>
        </label>
        <button
          class="btn primary"
          type="button"
          data-testid="diag-exec-cold"
          :disabled="!selectedTemplateId"
          @click="runTemplate(false)"
        >
          冷执行
        </button>
        <button
          class="btn"
          type="button"
          data-testid="diag-exec-cache"
          :disabled="!selectedTemplateId"
          @click="runTemplate(true)"
        >
          带缓存执行
        </button>
      </form>
    </article>

    <div class="diag-grid">
      <article class="diag-card">
        <h4>诊断文件</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>文件标识</th>
              <th>状态</th>
              <th>覆盖到完成序</th>
              <th>样本 / 平均耗时</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="file in files"
              :key="file.文件标识"
              :data-testid="`diag-file-${file.文件标识}`"
              :data-status="file.状态"
            >
              <td>{{ file.文件标识 }}</td>
              <td>
                <span class="diag-badge" :class="file.已完整交付 ? 'ok' : 'warn'">
                  {{ file.已完整交付 ? '完整可复跑' : file.状态 }}
                </span>
              </td>
              <td>{{ file.覆盖完成序上界 ?? '—' }}</td>
              <td>{{ file.样本数 }} / {{ file.平均耗时毫秒 ?? '—' }}</td>
              <td class="row-actions">
                <button
                  class="link"
                  type="button"
                  :data-testid="`diag-download-${file.文件标识}`"
                  @click="download(file.文件标识, 'json')"
                >
                  下载 JSON
                </button>
                <button
                  class="link"
                  type="button"
                  :data-testid="`diag-download-csv-${file.文件标识}`"
                  @click="download(file.文件标识, 'csv')"
                >
                  下载 CSV
                </button>
                <button
                  class="link"
                  type="button"
                  :data-testid="`diag-open-${file.文件标识}`"
                  :disabled="!file.已完整交付"
                  @click="openFile(file.文件标识)"
                >
                  打开明细
                </button>
                <button
                  class="link danger"
                  type="button"
                  :data-testid="`diag-interrupt-${file.文件标识}`"
                  @click="interrupt(file.文件标识)"
                >
                  模拟中断
                </button>
              </td>
            </tr>
            <tr v-if="!files.length">
              <td colspan="5" class="empty-state">还没有诊断文件，先执行查询再登记。</td>
            </tr>
          </tbody>
        </table>
      </article>

      <article v-if="activeFile" class="diag-card" data-testid="diag-detail">
        <div class="diag-subhead">
          <h4>文件明细 · {{ activeFile.文件标识 }}</h4>
          <span class="page-desc">修订号 r{{ revision.revision }} · 快照 {{ activeFile.生成时间 }}</span>
        </div>
        <p class="diag-note">
          覆盖完成序 ≤ {{ activeFile.覆盖完成序上界 }}，已完成执行 {{ activeFile.已完成执行数 }} 笔；
          摘要样本 {{ activeFile.摘要.样本数 }} 笔，平均 {{ activeFile.摘要.平均耗时毫秒 }} ms，
          阈值 {{ activeFile.摘要.慢查询阈值毫秒 }} ms。
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>执行标识</th>
              <th>模板标识</th>
              <th>取数口径</th>
              <th>完成方式</th>
              <th>耗时(ms)</th>
              <th>状态</th>
              <th>动作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in activeFile.明细"
              :key="item.执行标识"
              :data-testid="`diag-detail-${item.执行标识}`"
            >
              <td>{{ item.执行标识 }}</td>
              <td>{{ item.模板标识 }}</td>
              <td>{{ item.取数口径 }}</td>
              <td>{{ item.完成方式 }}</td>
              <td>{{ item['耗时(毫秒)'] ?? item.耗时毫秒 }}</td>
              <td>{{ item.status }}</td>
              <td class="row-actions">
                <button
                  class="link"
                  type="button"
                  :data-testid="`diag-replay-${item.执行标识}`"
                  @click="replay(item.执行标识)"
                >
                  复跑该笔
                </button>
              </td>
            </tr>
            <tr v-if="!activeFile.明细.length">
              <td colspan="7" class="empty-state">该文件没有超过慢查阈值的已完成执行。</td>
            </tr>
          </tbody>
        </table>
        <p v-if="compare" class="diag-compare" data-testid="diag-compare">
          旧 {{ compare.旧执行['执行标识'] }}（{{ compare.旧执行['取数口径'] }} /
          {{ compare.旧执行['完成方式'] }} / {{ compare.旧执行['耗时(毫秒)'] }}ms）
          → 新 {{ compare.新执行['执行标识'] }}（{{ compare.新执行['取数口径'] }} /
          {{ compare.新执行['完成方式'] }} / {{ compare.新执行['耗时(毫秒)'] }}ms），
          口径变化：{{ compare.口径变化 ? '是' : '否' }}，耗时差
          {{ compare['耗时差(毫秒)'] }}ms。
        </p>
      </article>
    </div>

    <article class="diag-card">
      <div class="diag-subhead">
        <h4>运行日志（执行维度）</h4>
        <span class="page-desc">冷执行与缓存命中分别记账，包含真实指标、模板标识和实际取数口径。</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>执行标识</th>
            <th>模板标识</th>
            <th>指标标识</th>
            <th>完成方式</th>
            <th>取数口径</th>
            <th>耗时(ms)</th>
            <th>状态</th>
            <th>复跑关联</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="log in executions"
            :key="log['执行标识']"
            :data-testid="`exec-${log['执行标识']}`"
          >
            <td>{{ log['执行标识'] }}</td>
            <td>{{ log['模板标识'] }}</td>
            <td>{{ log['指标标识'] }}</td>
            <td>{{ log['完成方式'] }}</td>
            <td>{{ log['取数口径'] }}</td>
            <td>{{ log['耗时(毫秒)'] }}</td>
            <td>{{ log.status }}</td>
            <td>{{ log['复跑关联执行标识'] ?? '—' }}</td>
          </tr>
          <tr v-if="!executions.length">
            <td colspan="8" class="empty-state">暂无运行日志。</td>
          </tr>
        </tbody>
      </table>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  compareExecutions,
  createDiagnosticFile,
  downloadDiagnosticFile,
  executeTemplate,
  interruptDiagnosticFile,
  listDiagnosticFiles,
  listExecutions,
  listTemplates,
  loadDiagnosticFile,
  replayFromDiagnostic,
  type TemplateOption,
} from '@/api/query-diagnostics'
import type {
  CompareResult,
  DiagnosticFileSummary,
  DiagnosticPayload,
  ExecutionLog,
} from '@/data/types'

const props = withDefaults(defineProps<{ revision?: number }>(), { revision: 1 })

const operator = ref('接班人')
const files = ref<DiagnosticFileSummary[]>([])
const templates = ref<TemplateOption[]>([])
const selectedTemplateId = ref(0)
const activeFile = ref<DiagnosticPayload | null>(null)
const executions = ref<ExecutionLog[]>([])
const compare = ref<CompareResult | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const message = ref('')
const revision = ref({ revision: props.revision, openedAt: '', regenerated: false })

function setMessage(text: string) {
  errorMessage.value = ''
  message.value = text
}

function setError(error: unknown, fallback: string) {
  message.value = ''
  errorMessage.value = error instanceof Error ? error.message : fallback
}

async function reloadAll() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [fileList, executionList] = await Promise.all([
      listDiagnosticFiles(),
      listExecutions(),
    ])
    files.value = fileList
    executions.value = [...executionList].reverse()
  } catch (error) {
    setError(error, '诊断文件/运行日志读取失败')
  } finally {
    loading.value = false
  }
}

async function reloadTemplates() {
  try {
    const list = await listTemplates()
    templates.value = list.filter((item) => item.status === '已发布')
    if (!selectedTemplateId.value && templates.value.length) {
      selectedTemplateId.value = templates.value[0].id
    }
  } catch (error) {
    setError(error, '查询模板读取失败')
  }
}

async function runTemplate(useCache: boolean) {
  if (!selectedTemplateId.value) return
  try {
    const result = await executeTemplate(
      selectedTemplateId.value,
      operator.value || '接班人',
      useCache,
    )
    setMessage(
      `${useCache ? '带缓存' : '冷'}执行完成：${String(result['execution_key'] ?? '')}，` +
        `来源 ${String(result['source'] ?? '')}，口径 ${String(result['caliber'] ?? '')}，` +
        `耗时 ${result['duration_ms'] ?? '—'}ms。`,
    )
    await reloadAll()
  } catch (error) {
    setError(error, '查询执行失败')
  }
}

async function createFile() {
  loading.value = true
  try {
    const created = await createDiagnosticFile(operator.value || '接班人')
    setMessage(`已登记诊断文件 ${created.文件标识}，下载开始时冻结快照。`)
    await reloadAll()
  } catch (error) {
    setError(error, '诊断文件登记失败')
  } finally {
    loading.value = false
  }
}

async function download(fileId: string, format: 'json' | 'csv') {
  try {
    await downloadDiagnosticFile(fileId, format)
    setMessage(`已下载 ${fileId}.${format}，文件已完整交付，可作为复跑依据。`)
    await reloadAll()
  } catch (error) {
    setError(error, '诊断文件下载失败')
  }
}

async function interrupt(fileId: string) {
  try {
    await interruptDiagnosticFile(fileId)
    setMessage(`模拟中断 ${fileId}：文件为半成品，不能发起复跑。`)
    await reloadAll()
  } catch (error) {
    setError(error, '中断诊断文件失败')
  }
}

async function openFile(fileId: string) {
  try {
    const payload = await loadDiagnosticFile(fileId)
    activeFile.value = payload
    revision.value = {
      revision: (revision.value.revision ?? 0) + 1,
      openedAt: new Date().toISOString(),
      regenerated: Boolean(payload.生成时间),
    }
    setMessage(`已打开完整文件 ${fileId}（修订号 r${revision.value.revision}）。`)
  } catch (error) {
    activeFile.value = null
    setError(error, '诊断文件明细读取失败')
  }
}

async function replay(executionRef: string) {
  if (!activeFile.value) return
  const source = activeFile.value.明细.find((item) => item.执行标识 === executionRef)
  if (!source) return
  try {
    const result = await replayFromDiagnostic(
      activeFile.value.文件标识,
      executionRef,
      operator.value || '接班人',
    )
    const newExecutionId = Number(result['execution_id'] ?? 0)
    const sourceExecutionId = Number(source['执行ID'] ?? 0)
    // 旧文件保持历史快照：重新打开仍是原口径和原耗时。
    await openFile(activeFile.value.文件标识)
    if (newExecutionId && sourceExecutionId) {
      try {
        compare.value = await compareExecutions(sourceExecutionId, newExecutionId)
      } catch {
        compare.value = null
      }
    }
    setMessage(
      `复跑完成：新执行 ${result['execution_key'] ?? newExecutionId}，` +
        `口径 ${String(result['caliber'] ?? '')}，耗时 ${result['duration_ms'] ?? '—'}ms。`,
    )
    await reloadAll()
  } catch (error) {
    setError(error, '复跑失败')
  }
}

onMounted(() => {
  void reloadAll()
  void reloadTemplates()
})
</script>

<style scoped>
.diagnostics-panel {
  margin-top: 24px;
  border-top: 1px solid var(--border);
  padding-top: 16px;
}
.diag-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}
.diag-head h3 { margin: 0 0 4px; font-size: 16px; }
.diag-inline { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.diag-inline input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; width: 140px; }
.diag-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 12px; }
.diag-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.diag-card h4 { margin: 0 0 8px; font-size: 14px; }
.diag-subhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.diag-note { font-size: 12px; color: var(--muted); background: #f8fafc; border-radius: 6px; padding: 8px; }
.diag-compare { font-size: 12px; color: #1f2937; background: #eef6ff; border-radius: 6px; padding: 8px; }
.diag-message { font-size: 13px; color: #1f6feb; margin: 0 0 10px; }
.diag-exec { padding-bottom: 14px; }
.diag-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-top: 10px; }
.diag-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.diag-form select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; min-width: 280px; }
.diag-badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.diag-badge.ok { background: #dcfce7; color: #166534; }
.diag-badge.warn { background: #fef3c7; color: #92400e; }
.link.danger { color: #b42318; }
.link:disabled { color: #94a3b8; cursor: not-allowed; }
</style>
