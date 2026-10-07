import React, { useMemo, useState } from 'react'
import { passengerTimeline, auditFrame } from '../engine/replay'

const STATUS_TEXT = {
  1: '候梯中', 2: '轿厢内', 3: '已到达', 4: '未服务（计入指标）',
}
const STATUS_CLASS = { 1: 'st-wait', 2: 'st-car', 3: 'st-done', 4: 'st-unserved' }

export default function PassengerAudit({ result, frame, demand }) {
  const [pidInput, setPidInput] = useState(0)
  const [filter, setFilter] = useState('all')
  const passengers = result.passengers
  const events = result.events

  const chain = useMemo(
    () => passengerTimeline(events, Number(pidInput) || 0),
    [events, pidInput])

  const issues = useMemo(() => auditFrame(frame), [frame])

  const counts = useMemo(() => {
    const c = { 1: 0, 2: 0, 3: 0, 4: 0 }
    for (const p of passengers) c[p.status] = (c[p.status] || 0) + 1
    return c
  }, [passengers])

  const shown = useMemo(() => {
    const list = filter === 'all' ? passengers
      : passengers.filter(p => String(p.status) === filter)
    return list.slice(0, 120)
  }, [passengers, filter])

  const selected = passengers[Number(pidInput)]

  return (
    <div className="audit">
      <div className="audit-head">
        <h3>乘客轨迹核对（同一时刻只允许一个物理位置）</h3>
        <div className={`audit-status ${issues.length ? 'bad' : 'ok'}`}>
          {issues.length === 0
            ? `✓ 当前帧 ${frame.pax.size} 名乘客位置全部唯一且合法`
            : `✗ ${issues.length} 个位置异常`}
        </div>
      </div>

      <div className="audit-stats">
        {Object.entries(counts).map(([st, n]) => (
          <button key={st}
                  className={`stat-pill ${STATUS_CLASS[st]} ${filter === st ? 'active' : ''}`}
                  onClick={() => setFilter(filter === st ? 'all' : st)}>
            {STATUS_TEXT[st]} <b>{n}</b>
          </button>
        ))}
      </div>

      <div className="audit-body">
        <div className="pax-table-wrap">
          <table className="pax-table">
            <thead>
              <tr>
                <th>#</th><th>起</th><th>终</th><th>到达</th>
                <th>登乘</th><th>离开</th><th>候梯</th><th>乘梯</th>
                <th>总行程</th><th>梯</th><th>状态</th>
              </tr>
            </thead>
            <tbody>
              {shown.map(p => (
                <tr key={p.pid}
                    className={String(p.pid) === String(pidInput) ? 'sel' : ''}
                    onClick={() => setPidInput(p.pid)}>
                  <td>{p.pid}</td>
                  <td>{p.origin}</td>
                  <td>{p.dest}</td>
                  <td>{p.arrival_time.toFixed(1)}</td>
                  <td>{p.board_time != null ? p.board_time.toFixed(1) : '—'}</td>
                  <td>{p.alight_time != null ? p.alight_time.toFixed(1) : '—'}</td>
                  <td>{p.wait_time != null ? p.wait_time.toFixed(1) : '—'}</td>
                  <td>{p.ride_time != null ? p.ride_time.toFixed(1) : '—'}</td>
                  <td>{p.total_time != null ? p.total_time.toFixed(1) : '—'}</td>
                  <td>{p.car_id != null ? `${p.car_id}#` : '—'}</td>
                  <td className={STATUS_CLASS[p.status]}>{STATUS_TEXT[p.status]}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {passengers.length > 120 && <small>仅显示前 120 行，共 {passengers.length} 人</small>}
        </div>

        <div className="timeline">
          <div className="pid-pick">
            乘客 #
            <input type="number" min={0} max={demand - 1}
                   value={pidInput}
                   onChange={e => setPidInput(e.target.value)} />
          </div>
          {selected && (
            <div className="timeline-summary">
              <div>{selected.origin}F → {selected.dest}F</div>
              <div className={STATUS_CLASS[selected.status]}>
                {STATUS_TEXT[selected.status]}
              </div>
            </div>
          )}
          <ol>
            {chain.map((c, i) => (
              <li key={i} className={c.type}>
                <span className="tl-t">t={c.t.toFixed(2)}</span>
                <span className="tl-loc">{c.location}</span>
              </li>
            ))}
            {chain.length === 0 && <li className="empty">无记录</li>}
          </ol>
        </div>
      </div>
    </div>
  )
}
