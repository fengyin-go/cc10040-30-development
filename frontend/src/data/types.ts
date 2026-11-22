/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
  entry?: EntryRow
}

/** 修订号：每次重新打开工作台都会变化，便于页面按修订号重新取明细。 */
export type DiagnosticRevision = {
  revision: number
  openedAt: string
  regenerated: boolean
  token?: string
}

export type DiagnosticFileSummary = {
  文件标识: string
  创建时间: string
  创建人: string
  慢查询阈值毫秒: number
  状态: string
  已完整交付: boolean
  最近下载开始时间?: string | null
  最近交付时间?: string | null
  最近中断时间?: string | null
  覆盖完成序上界?: number | null
  覆盖执行标识上界?: number | null
  已完成执行数: number
  样本数: number
  平均耗时毫秒?: number | null
}

export type DiagnosticDetail = {
  执行ID?: number
  执行标识: string
  完成序?: number
  查询时间: string
  操作人: string
  指标标识: string
  指标ID?: number
  指标名称: string
  模板标识: string
  模板ID?: number
  模板名称: string
  取数口径: string
  取数口径版本: string
  取数口径描述: string
  完成方式: string
  耗时毫秒?: number
  返回行数?: string
  status: string
  缓存来源执行标识?: number | null
  复跑关联执行标识?: number | null
  '耗时(毫秒)'?: number
  '慢查询阈值(毫秒)'?: number
  template_snapshot?: Record<string, unknown>
}

export type DiagnosticPayload = {
  文件标识: string
  生成时间: string
  慢查询阈值毫秒: number
  覆盖完成序上界: number
  覆盖执行标识上界: number
  已完成执行数: number
  覆盖执行标识: number[]
  说明: string
  摘要: {
    慢查询阈值毫秒: number
    样本数: number
    平均耗时毫秒: number
    最大耗时毫秒: number
    最小耗时毫秒: number
    耗时合计毫秒: number
    完成方式分布: Record<string, number>
    取数口径分布: Record<string, number>
  }
  明细: DiagnosticDetail[]
  文件状态?: string
}

export type ExecutionLog = {
  id: number
  执行标识: string
  查询时间: string
  操作人: string
  指标标识: string
  模板标识: string
  取数口径: string
  完成方式: string
  '耗时(毫秒)': number
  status: string
  状态: string
  completed: boolean
  复跑关联执行标识?: number | null
  关联诊断文件?: string | null
}

export type CompareResult = {
  旧执行: Record<string, unknown>
  新执行: Record<string, unknown>
  口径变化: boolean
  取数方式变化: boolean
  '耗时差(毫秒)': number
  复跑关联执行标识?: number | null
  关联诊断文件?: string | null
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
