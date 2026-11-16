import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/schema'
const columns = [
  "定义编号",
  "流名称",
  "字段名称",
  "字段类型",
  "默认值",
  "可空标识",
  "格式版本",
  "定义状态"
]
const actions = [
  "发布格式",
  "废弃格式",
  "冻结格式"
]
const statuses = [
  "草稿",
  "已发布",
  "已废弃",
  "已冻结"
]
const stats = [
  {
    "label": "格式定义总数",
    "value": 0
  },
  {
    "label": "已发布格式",
    "value": 0
  },
  {
    "label": "已废弃格式",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type Snapshot = {
  "定义编号"?: string
  "字段编号"?: string
  "字段名称"?: string
  "字段类型"?: string
  "格式版本"?: string
  "写入模式"?: string
  "输出配置编号"?: string
  "修订序号"?: number
  "当前"?: boolean
  "创建时间"?: string
}

type RevisionForm = {
  "字段编号": string
  "字段名称": string
  "字段类型": string
  "格式版本": string
  "输出配置编号": string
  "写入模式": string
}

const emptyRevision: RevisionForm = {
  "字段编号": "",
  "字段名称": "",
  "字段类型": "",
  "格式版本": "",
  "输出配置编号": "",
  "写入模式": "",
}

export default function SchemaView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<(Row & { 历史快照?: Snapshot[]; 当前快照?: Snapshot | null }) | null>(null)
  const [revision, setRevision] = useState<RevisionForm>(emptyRevision)
  const [submitting, setSubmitting] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('格式定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流数据格式定义列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const openDetail = useCallback(async (row: Row) => {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) {
        throw new Error('格式定义详情读取失败')
      }
      const payload = await response.json()
      setDetail(payload)
      // 表单回填最新确认快照，刷新或重新进入后仍保留原值。
      const snapshot: Snapshot = payload["当前快照"] ?? {}
      setRevision({
        "字段编号": String(snapshot["字段编号"] ?? payload["字段编号"] ?? ''),
        "字段名称": String(snapshot["字段名称"] ?? payload["字段名称"] ?? ''),
        "字段类型": String(snapshot["字段类型"] ?? payload["字段类型"] ?? ''),
        "格式版本": String(snapshot["格式版本"] ?? payload["格式版本"] ?? ''),
        "输出配置编号": String(snapshot["输出配置编号"] ?? payload["输出配置编号"] ?? ''),
        "写入模式": String(snapshot["写入模式"] ?? payload["写入模式"] ?? ''),
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '格式定义详情读取失败')
    }
  }, [])

  async function submitRevision() {
    if (!detail) return
    setSubmitting(true)
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${detail.id}/revisions`, {
        method: 'POST',
        body: JSON.stringify(revision),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        const message = String(payload.message ?? '字段修订未保存')
        if (message.includes('已确认') || message.includes('未重复')) {
          // 重复提交同一份修订：只保留一份快照，不再当作错误。
          setNoticeMessage(message)
          await openDetail(detail)
          await reload()
          return
        }
        throw new Error(message)
      }
      setNoticeMessage(payload.message ?? '字段修订已保存')
      // 保存成功后重新读取详情和列表，页面显示与快照同源。
      await openDetail(detail)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段修订保存失败')
      // 保存失败时详情、表单和输出契约保持上一份完整快照。
      await openDetail(detail)
    } finally {
      setSubmitting(false)
    }
  }

  async function refreshReport() {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/report`)
      if (!response.ok) {
        throw new Error('发布报告读取失败')
      }
      const payload = await response.json()
      const target = (payload.items ?? []).find(
        (item: Row) => String(item["定义编号"]) === String(detail?.["定义编号"] ?? ''),
      )
      setNoticeMessage(
        target
          ? `报告核对：${target["定义编号"]} ${target["字段名称"]}:${target["字段类型"]} / ${target["格式版本"]}`
          : `报告共 ${payload.total ?? 0} 条格式定义`,
      )
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '发布报告读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        const message = String(payload.message ?? '流数据格式定义动作未生效，请稍后重试')
        if (message.includes('已确认') || message.includes('未重复')) {
          setNoticeMessage(message)
          if (detail && detail.id === row.id) {
            await openDetail(row)
          }
          await reload()
          return
        }
        throw new Error(message)
      }
      setNoticeMessage(payload.message ?? `已执行${action}`)
      if (detail && detail.id === row.id) {
        await openDetail(row)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流数据格式定义操作失败')
    }
  }

  return (
    <section className="page" data-module="schema">
      <header className="page-head">
        <div>
          <h2>流数据格式定义管理</h2>
          <p className="page-desc">维护流数据格式的字段定义、类型约束与版本管理，为流数据格式定义提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('格式定义登记入口尚未接入审批流')}>
            登记格式定义
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出流数据格式定义清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>详情</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无流数据格式定义数据，可先登记格式定义</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="detail-panel" data-module="schema-detail">
          <header className="page-head">
            <div>
              <h3>格式定义详情：{detail["定义编号"]}</h3>
              <p className="page-desc">
                当前快照：{detail["字段编号"] ?? '—'} / {detail["字段名称"] ?? '—'}:
                {detail["字段类型"] ?? '—'} / {detail["格式版本"] ?? '—'}
              </p>
            </div>
            <div className="page-actions">
              <button className="btn" type="button" onClick={() => void refreshReport()}>核对发布报告</button>
              <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭详情</button>
            </div>
          </header>

          <form
            className="filter-bar"
            onSubmit={(event) => { event.preventDefault(); void submitRevision() }}
          >
            {(["字段编号", "字段名称", "字段类型", "格式版本", "输出配置编号", "写入模式"] as const).map((field) => (
              <label key={field} className="filter-item">
                <span>{field}</span>
                <input
                  value={revision[field]}
                  onChange={(event) => setRevision((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
            <button className="btn primary" type="submit" disabled={submitting}>
              {submitting ? '保存中…' : '保存字段修订'}
            </button>
          </form>

          <table className="data-table">
            <thead>
              <tr>
                <th>字段编号</th><th>字段名称</th><th>字段类型</th><th>格式版本</th>
                <th>输出配置编号</th><th>写入模式</th><th>修订序号</th><th>当前</th><th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {(detail["历史快照"] ?? []).map((snapshot) => (
                <tr key={`${snapshot["字段编号"]}-${snapshot["修订序号"]}`}>
                  <td>{snapshot["字段编号"] ?? '—'}</td>
                  <td>{snapshot["字段名称"] ?? '—'}</td>
                  <td>{snapshot["字段类型"] ?? '—'}</td>
                  <td>{snapshot["格式版本"] ?? '—'}</td>
                  <td>{snapshot["输出配置编号"] || '—'}</td>
                  <td>{snapshot["写入模式"] || '—'}</td>
                  <td>{snapshot["修订序号"] ?? '—'}</td>
                  <td>{snapshot["当前"] ? '是' : '否'}</td>
                  <td>{snapshot["创建时间"] ?? '—'}</td>
                </tr>
              ))}
              {(detail["历史快照"] ?? []).length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={9}>暂无字段版本快照，保存字段修订后会生成</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条流数据格式定义记录{noticeMessage ? `，${noticeMessage}` : ''}</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
