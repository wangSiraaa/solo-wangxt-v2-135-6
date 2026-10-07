const BASE = '/api'

async function jsonFetch(url, opts) {
  const res = await fetch(url, opts)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${text}`)
  }
  return res.json()
}

export const api = {
  health: () => jsonFetch(`${BASE}/health`),
  scenarios: () => jsonFetch(`${BASE}/scenarios`),
  simulate: (body) => jsonFetch(`${BASE}/simulate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  runs: () => jsonFetch(`${BASE}/runs`),
  run: (id) => jsonFetch(`${BASE}/runs/${id}`),
}

export const POLICY_META = {
  collective: { name: '集选 SCAN', desc: '无全局指派，各梯沿方向扫描自行认领，可能重复响应' },
  nearest: { name: '最近梯 ETA', desc: '全局按预估到达时间指派，长队列多梯补位' },
  zoning: { name: '高低区分区', desc: '各梯负责固定楼层区段，本区满载才越区支援' },
}
