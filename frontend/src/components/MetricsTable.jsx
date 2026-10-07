import React from 'react'
import { POLICY_META } from '../api'

function fmt(v, digits = 1) {
  if (v == null) return '—'
  return Number(v).toFixed(digits)
}

function Cell({ value, best, bad, suffix }) {
  return (
    <td className={[
      best ? 'best' : '',
      bad ? 'bad' : '',
    ].join(' ')}>
      {fmt(value)}{suffix || ''}
    </td>
  )
}

export default function MetricsTable({ comparison, results }) {
  const best = comparison?.best || {}
  const byPol = Object.fromEntries(results.map(r => [r.policy, r.metrics]))
  return (
    <div className="metrics">
      <table>
        <thead>
          <tr>
            <th>策略</th>
            <th>完成 / 需求</th>
            <th>未服务</th>
            <th>候梯均值*</th>
            <th>候梯P90*</th>
            <th>乘梯均值**</th>
            <th>总行程均值*</th>
            <th>总行程P90*</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(byPol).map(([pol, m]) => {
            const a = m.all_pax, s = m.served_only
            return (
              <tr key={pol}>
                <td className="pol-name">
                  <b>{POLICY_META[pol]?.name || pol}</b>
                  <small>{POLICY_META[pol]?.desc}</small>
                </td>
                <td>{m.served} / {m.demand}</td>
                <Cell value={m.unserved} bad={m.unserved > 0} />
                <Cell value={a.wait_mean} best={best.wait_mean_all === pol} suffix="s" />
                <Cell value={a.wait_p90} best={best.wait_p90_all === pol} suffix="s" />
                <Cell value={s.ride_mean} suffix="s" />
                <Cell value={a.total_mean} best={best.total_mean_all === pol} suffix="s" />
                <Cell value={a.total_p90} suffix="s" />
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="metric-note">
        * 全客流口径：仿真结束仍未完成行程的乘客，等待/总时间按截止时刻计入，<b>不剔除</b>，
        因此"未服务"越多数值越差，不能靠丢弃乘客美化结果。<br />
        ** 乘梯均值为已完成乘客的条件均值（未上车者没有乘梯时间，单列说明）。
        候梯 = 到达→关门登乘；乘梯 = 登乘→目的层离开；总行程 = 到达→目的层 = 候梯 + 乘梯。
      </p>
    </div>
  )
}
