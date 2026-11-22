/** 查询诊断与运行日志相关接口：查询构建器执行、诊断文件下载/复跑、新旧执行核对。 */
import { request } from './client'
import type {
  CompareResult,
  DiagnosticFileSummary,
  DiagnosticPayload,
  ExecutionLog,
} from '@/data/types'

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    let detail = fallback
    try {
      const body = await response.json()
      detail = body?.detail ?? body?.error ?? fallback
    } catch {
      // 保留兜底文案
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export async function executeTemplate(
  templateId: number,
  operator: string,
  useCache: boolean,
): Promise<Record<string, unknown>> {
  const query = new URLSearchParams({
    operator,
    use_cache: String(useCache),
  }).toString()
  const response = await request(
    `/api/cross/query/${templateId}/execute?${query}`,
    { method: 'POST' },
  )
  return readJson<Record<string, unknown>>(response, '查询执行失败')
}

export type TemplateOption = {
  id: number
  模板名称: string
  关联指标: string
  维度组合: string
  缓存策略: string
  status: string
}

export async function listTemplates(): Promise<TemplateOption[]> {
  const response = await request('/api/query_builder?size=200')
  const payload = await readJson<{ items: TemplateOption[] }>(response, '查询模板读取失败')
  return payload.items
}

export async function listExecutions(limit = 200): Promise<ExecutionLog[]> {
  const response = await request(`/api/cross/query-journal/executions?limit=${limit}`)
  const payload = await readJson<{ items: ExecutionLog[] }>(response, '运行日志读取失败')
  return payload.items
}

export async function listDiagnosticFiles(): Promise<DiagnosticFileSummary[]> {
  const response = await request('/api/cross/query-journal/diagnostics')
  const payload = await readJson<{ items: DiagnosticFileSummary[] }>(
    response,
    '诊断文件列表读取失败',
  )
  return payload.items
}

export async function createDiagnosticFile(operator: string): Promise<DiagnosticFileSummary> {
  const query = new URLSearchParams({ operator }).toString()
  const response = await request(`/api/cross/query-journal/diagnostics?${query}`, {
    method: 'POST',
  })
  return readJson<DiagnosticFileSummary>(response, '诊断文件登记失败')
}

export async function interruptDiagnosticFile(fileId: string): Promise<void> {
  const response = await request(`/api/cross/query-journal/diagnostics/${fileId}/interrupt`, {
    method: 'POST',
  })
  await readJson(response, '中断诊断文件失败')
}

/** 下载诊断文件；下载动作在服务端冻结快照，完整写出后才标记为可复跑。 */
export async function downloadDiagnosticFile(
  fileId: string,
  format: 'json' | 'csv' = 'json',
): Promise<void> {
  const response = await request(
    `/api/cross/query-journal/diagnostics/${fileId}/download?format=${format}`,
  )
  if (!response.ok) {
    let detail = '诊断文件下载失败'
    try {
      const body = await response.json()
      detail = body?.detail ?? detail
    } catch {
      // 保留兜底文案
    }
    throw new Error(detail)
  }
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${fileId}.${format}`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadDiagnosticFile(fileId: string): Promise<DiagnosticPayload> {
  const response = await request(`/api/cross/query-journal/diagnostics/${fileId}`)
  return readJson<DiagnosticPayload>(response, '诊断文件读取失败')
}

export async function replayFromDiagnostic(
  fileId: string,
  executionRef: string,
  operator: string,
): Promise<Record<string, unknown>> {
  const query = new URLSearchParams({ operator }).toString()
  const response = await request(
    `/api/cross/query/replay/${fileId}/${encodeURIComponent(executionRef)}?${query}`,
    { method: 'POST' },
  )
  return readJson<Record<string, unknown>>(response, '复跑失败')
}

export async function compareExecutions(
  oldExecutionId: number,
  newExecutionId: number,
): Promise<CompareResult> {
  const query = new URLSearchParams({
    old_execution_id: String(oldExecutionId),
    new_execution_id: String(newExecutionId),
  }).toString()
  const response = await request(`/api/cross/query-journal/compare?${query}`)
  return readJson<CompareResult>(response, '新旧执行核对失败')
}
