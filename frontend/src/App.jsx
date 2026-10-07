import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, POLICY_META } from './api'
import Shaft from './components/Shaft.jsx'
import MetricsTable from './components/MetricsTable.jsx'
import PassengerAudit from './components/PassengerAudit.jsx'
import { buildFrames } from './engine/replay.js'

function fmtDelta(v, suffix = '', digits = 1) {
  if (v == null) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${Number(v).toFixed(digits)}${suffix}`
}

function BlockImpact({ data }) {
  const rows = Object.entries(data.deltas.rows || {})
  return (
    <div className="block-impact">
      <h3>同一客流有/无阻挡变化（不重新生成乘客）</h3>
      <table>
        <thead>
          <tr>
            <th>策略</th>
            <th>完成人数变化</th>
            <th>未服务变化</th>
            <th>全客流候梯均值变化</th>
            <th>全客流总行程均值变化</th>
            <th>乘梯均值变化**</th>
            <th>受影响乘客*</th>
            <th>受影响者候梯均值*</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([pol, d]) => {
            const bg = d.blockages
            const am = bg?.affected_metrics
            return (
              <tr key={pol}>
                <td className="pol-name">{POLICY_META[pol]?.name || pol}</td>
                <td className={d.served_delta < 0 ? 'bad' : ''}>
                  {fmtDelta(d.served_delta, ' 人', 0)}
                </td>
                <td className={d.unserved_delta > 0 ? 'bad'
                  : d.unserved_delta < 0 ? 'best' : ''}>
                  {fmtDelta(d.unserved_delta, ' 人', 0)}
                </td>
                <td className={d.wait_mean_all_delta > 0 ? 'bad'
                  : d.wait_mean_all_delta < 0 ? 'best' : ''}>
                  {fmtDelta(d.wait_mean_all_delta, 's')}
                </td>
                <td className={d.total_mean_all_delta > 0 ? 'bad'
                  : d.total_mean_all_delta < 0 ? 'best' : ''}>
                  {fmtDelta(d.total_mean_all_delta, 's')}
                </td>
                <td>{fmtDelta(d.ride_mean_served_delta, 's')}</td>
                <td>{bg?.affected_count ?? '—'} 人
                  <small> （完成 {bg?.affected_served ?? 0}）</small></td>
                <td>{am?.wait_mean != null ? `${am.wait_mean.toFixed(1)}s` : '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="metric-note">
        * <b>受影响乘客</b>：阻挡期间被滞留在阻挡楼层候梯的人，与门保持打开时
        已在该轿厢内、行程被延后的人；其指标为子口径，与全客流口径并列、不替换。<br />
        ** 乘梯均值为已完成乘客条件均值之差。阻挡只延长门开停留，不改变任何乘客的
        起终层；负值来自有/无阻挡下完成人群的构成变化。
      </p>
    </div>
  )
}

function BlockTimeline({ replay, frameIdx, until, onSeek }) {
  const blocks = replay.blockages || []
  if (blocks.length === 0) return null
  // 时间轴上点击区间：跳到该阻挡进入保持的那一帧
  const jumpTo = (enterT) => {
    const idx = replay.frames.findIndex(f => f.t >= enterT)
    if (idx >= 0) onSeek(idx)
  }
  return (
    <div className="block-timeline">
      <span className="block-tl-label">阻挡区间</span>
      <div className="block-tl-track">
        {blocks.map((b, i) => {
          const left = pct(b.enterT ?? b.start)
          const width = pct((b.end - (b.enterT ?? b.start)) / until * 100 * 1)
          return (
            <button key={i}
                    className="block-tl-seg"
                    style={{
                      left,
                      width: `${((b.end - (b.enterT ?? b.start)) / until) * 100}%`,
                    }}
                    title={`${b.blockId}号阻挡：${b.car}#梯 @ ${b.floor}层 `
                      + `${(b.enterT ?? b.start).toFixed(1)}s–${b.end.toFixed(1)}s`}
                    onClick={() => jumpTo(b.enterT ?? b.start)}>
              {b.car}# · {b.floor}F
            </button>
          )
        })}
      </div>
    </div>
  )
}


const CASES = [
  { id: 'morning_up', name: '早高峰上行',
    desc: '大堂/低层 → 办公层，泊松到达，全部上行（满载压力集中在低层）' },
  { id: 'cross_floor', name: '跨层需求',
    desc: '上、下行混流，中间层互访，随机起终层' },
  { id: 'long_door', name: '长开门时间',
    desc: '与跨层同一客流（同种子），仅开关门耗时 4s→12s，对照门时影响' },
]

const DEFAULTS = {
  morning_up: { floors: 12, car_count: 4, capacity: 8, floor_time: 2,
    door_time: 4, seed: 11, rate: 22, duration: 900, until: 1500 },
  cross_floor: { floors: 12, car_count: 4, capacity: 8, floor_time: 2,
    door_time: 4, seed: 11, rate: 16, duration: 1200, until: 1800 },
  long_door: { floors: 12, car_count: 4, capacity: 8, floor_time: 2,
    door_time: 12, seed: 11, rate: 16, duration: 1200, until: 2400 },
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [caseId, setCaseId] = useState('morning_up')
  const [params, setParams] = useState(DEFAULTS.morning_up)
  const [policies, setPolicies] = useState(['collective', 'nearest', 'zoning'])
  const [blocks, setBlocks] = useState([])
  const [compareBaseline, setCompareBaseline] = useState(true)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [activePol, setActivePol] = useState('collective')
  const [showBaseline, setShowBaseline] = useState(false)
  const [frameIdx, setFrameIdx] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(16)
  const timerRef = useRef(null)

  useEffect(() => { api.health().then(setHealth).catch(() => setHealth({ db: false })) }, [])

  const selectCase = (id) => {
    setCaseId(id)
    setParams(DEFAULTS[id])
  }

  const togglePol = (p) => {
    setPolicies(ps => ps.includes(p) ? ps.filter(x => x !== p) : [...ps, p])
  }

  const addBlock = () => {
    setBlocks(bs => [...bs, {
      car: 0, floor: 1, start: 300, end: 360,
    }])
  }
  const updateBlock = (i, patch) => {
    setBlocks(bs => bs.map((b, j) => j === i ? { ...b, ...patch } : b))
  }
  const removeBlock = (i) => {
    setBlocks(bs => bs.filter((_, j) => j !== i))
  }

  const run = async () => {
    setLoading(true); setError(null); setData(null)
    const body = {
      case: caseId,
      policies,
      floors: Number(params.floors),
      car_count: Number(params.car_count),
      capacity: Number(params.capacity),
      floor_time: Number(params.floor_time),
      door_time: Number(params.door_time),
      seed: Number(params.seed),
      rate_per_min: Number(params.rate),
      duration: Number(params.duration),
      until: Number(params.until),
      blocks: blocks.map(b => ({
        car: Number(b.car), floor: Number(b.floor),
        start: Number(b.start), end: Number(b.end),
      })),
      compare_baseline: compareBaseline,
    }
    try {
      const res = await api.simulate(body)
      setData(res)
      setActivePol(res.results[0].policy)
      setShowBaseline(false)
      setFrameIdx(0)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const activeResult = showBaseline
    ? data?.baselines?.find(r => r.policy === activePol)
    : data?.results.find(r => r.policy === activePol)

  const replay = useMemo(() => {
    if (!activeResult) return null
    return buildFrames(activeResult.events, {
      floors: data.params.floors,
      carCount: data.params.car_count,
      capacity: data.params.capacity,
    })
  }, [activeResult, data])

  // 播放循环：事件日志按时间戳节奏推进（不是随机帧）
  useEffect(() => {
    if (!playing || !replay) return
    const next = () => {
      setFrameIdx(i => {
        if (i >= replay.frames.length - 1) { setPlaying(false); return i }
        return i + 1
      })
    }
    timerRef.current = setInterval(next, 1000 / speed)
    return () => clearInterval(timerRef.current)
  }, [playing, replay, speed, frameIdx])

  const frame = replay?.frames[Math.min(frameIdx, replay.frames.length - 1)]
  const simT = frame?.t ?? 0
  const progress = replay ? (frameIdx / (replay.frames.length - 1)) * 100 : 0

  return (
    <div className="app">
      <header>
        <h1>楼宇交通 · 电梯派梯策略对比仿真</h1>
        <div className="subtitle">
          SimPy 离散事件仿真 · React 重放事件日志 · PostgreSQL 持久化 · 不连接真实电梯
        </div>
        <div className={`health ${health?.db ? 'db-ok' : 'db-bad'}`}>
          {health == null ? '连接中…'
            : health.db ? `PostgreSQL 已连接（事件日志可回放）`
            : '数据库未连接（本次运行不持久化）'}
        </div>
      </header>

      <section className="control">
        <div className="case-cards">
          {CASES.map(c => (
            <button key={c.id}
                    className={`case-card ${caseId === c.id ? 'active' : ''}`}
                    onClick={() => selectCase(c.id)}>
              <b>{c.name}</b>
              <small>{c.desc}</small>
            </button>
          ))}
        </div>

        <div className="param-grid">
          <label>随机种子<input type="number" value={params.seed}
            onChange={e => setParams({ ...params, seed: e.target.value })} /></label>
          <label>楼层数<input type="number" value={params.floors}
            onChange={e => setParams({ ...params, floors: e.target.value })} /></label>
          <label>轿厢数<input type="number" min={1} max={8} value={params.car_count}
            onChange={e => setParams({ ...params, car_count: e.target.value })} /></label>
          <label>容量(人)<input type="number" value={params.capacity}
            onChange={e => setParams({ ...params, capacity: e.target.value })} /></label>
          <label>层间耗时(s)<input type="number" step={0.5} value={params.floor_time}
            onChange={e => setParams({ ...params, floor_time: e.target.value })} /></label>
          <label>开关门耗时(s)<input type="number" step={0.5}
            className={caseId === 'long_door' ? 'longdoor' : ''}
            value={params.door_time}
            onChange={e => setParams({ ...params, door_time: e.target.value })} /></label>
          <label>到达率(人/分)<input type="number" step={0.5} value={params.rate}
            onChange={e => setParams({ ...params, rate: e.target.value })} /></label>
          <label>客流时长(s)<input type="number" value={params.duration}
            onChange={e => setParams({ ...params, duration: e.target.value })} /></label>
          <label>仿真截止(s)<input type="number" value={params.until}
            onChange={e => setParams({ ...params, until: e.target.value })} /></label>
        </div>

        <div className="policy-row">
          {Object.entries(POLICY_META).map(([id, meta]) => (
            <label key={id} className={`policy-check ${policies.includes(id) ? 'on' : ''}`}>
              <input type="checkbox" checked={policies.includes(id)}
                     onChange={() => togglePol(id)} />
              <span><b>{meta.name}</b><small>{meta.desc}</small></span>
            </label>
          ))}
          <button className="run-btn" disabled={loading || policies.length === 0}
                  onClick={run}>
            {loading ? '仿真中…' : '同一客流运行对比 ▶'}
          </button>
        </div>

        <div className="block-editor">
          <div className="block-editor-head">
            <b>门保持打开（阻挡）事件</b>
            <label className="baseline-toggle">
              <input type="checkbox" checked={compareBaseline}
                     onChange={e => setCompareBaseline(e.target.checked)} />
              同时跑无阻挡对照（同一客流/种子，不重新生成乘客）
            </label>
            <button className="add-block-btn" onClick={addBlock}>＋ 添加阻挡</button>
          </div>
          {blocks.length === 0 && (
            <small className="muted-note">
              不配置则为普通仿真。配置后每个策略各跑一次阻挡运行；
              指定轿厢在 [开始, 结束] 秒内门保持打开、不能移动或重复登乘，其他轿厢照常运行。
            </small>
          )}
          {blocks.map((b, i) => (
            <div key={i} className="block-row">
              <span className="block-id">#{i}</span>
              <label>轿厢
                <input type="number" min={0} max={Math.max(0, Number(params.car_count) - 1)}
                       value={b.car}
                       onChange={e => updateBlock(i, { car: e.target.value })} /></label>
              <label>楼层
                <input type="number" min={1} max={params.floors} value={b.floor}
                       onChange={e => updateBlock(i, { floor: e.target.value })} /></label>
              <label>开始(s)
                <input type="number" min={0} value={b.start}
                       onChange={e => updateBlock(i, { start: e.target.value })} /></label>
              <label>解除(s)
                <input type="number" min={0} value={b.end}
                       onChange={e => updateBlock(i, { end: e.target.value })} /></label>
              <span className="block-duration">
                持续 {Math.max(0, Number(b.end) - Number(b.start))}s
              </span>
              <button className="del-block" onClick={() => removeBlock(i)}>删除</button>
            </div>
          ))}
        </div>

        {error && <div className="error">{error}</div>}
        {data && (
          <div className="same-flow-note">
            ✓ {blocks.length > 0 ? '有/无阻挡及' : ''}三种策略使用
            <b>完全相同的 {data.demand_n} 名乘客到达序列</b>
            （种子 {data.params.seed}），仅派梯决策{blocks.length > 0 ? '与阻挡事件' : ''}不同
            {data.persisted ? '；结果与事件日志已写入 PostgreSQL' : ''}
          </div>
        )}
      </section>

      {data && (
        <>
          <section>
            <h2>指标对比（候梯 / 乘梯 / 总行程分列，未服务不剔除）</h2>
            <MetricsTable comparison={showBaseline && data.baseline_comparison
                ? data.baseline_comparison : data.comparison}
              results={showBaseline ? data.baselines : data.results}
              blocked={!showBaseline && (data.params.blocks?.length > 0)} />
            {data.params.blocks?.length > 0 && data.deltas && (
              <BlockImpact data={data} />
            )}
          </section>

          <section className="playback">
            <div className="play-head">
              <h2>井道与候梯队列（事件日志重放）</h2>
              <div className="play-head-right">
                {data.baselines?.length > 0 && (
                  <div className="view-toggle">
                    <button className={!showBaseline ? 'active' : ''}
                            onClick={() => { setShowBaseline(false); setFrameIdx(0) }}>
                      阻挡运行
                    </button>
                    <button className={showBaseline ? 'active' : ''}
                            onClick={() => { setShowBaseline(true); setFrameIdx(0) }}>
                      无阻挡对照
                    </button>
                  </div>
                )}
                <div className="policy-tabs">
                  {data.results.map(r => (
                    <button key={r.policy}
                            className={activePol === r.policy ? 'active' : ''}
                            onClick={() => { setActivePol(r.policy); setFrameIdx(0) }}>
                      {POLICY_META[r.policy].name}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="play-controls">
              <button onClick={() => { setFrameIdx(0); setPlaying(false) }}>⏮</button>
              <button onClick={() => setPlaying(p => !p)}>
                {playing ? '⏸ 暂停' : '▶ 播放'}
              </button>
              <input type="range" min={0}
                     max={replay.frames.length - 1} value={frameIdx}
                     onChange={e => { setPlaying(false); setFrameIdx(Number(e.target.value)) }} />
              <span className="clock">仿真时刻 {simT.toFixed(1)}s / {data.params.until}s</span>
              <label className="speed">速度
                <select value={speed} onChange={e => setSpeed(Number(e.target.value))}>
                  <option value={4}>0.25×</option>
                  <option value={8}>0.5×</option>
                  <option value={16}>1×</option>
                  <option value={40}>2.5×</option>
                  <option value={120}>8×</option>
                </select>
              </label>
              <span className="frame-no">帧 {frameIdx}/{replay.frames.length - 1}</span>
            </div>
            <BlockTimeline replay={replay} frameIdx={frameIdx}
                           until={data.params.until}
                           onSeek={(i) => { setPlaying(false); setFrameIdx(i) }} />
            <div className="progress"><i style={{ width: `${progress}%` }} /></div>

            {frame && (
              <Shaft frame={frame} floors={data.params.floors}
                     carCount={data.params.car_count}
                     capacity={data.params.capacity} />
            )}
            <div className="legend">
              <span><i className="dot pax" /> 乘客编号（悬停看目的层）</span>
              <span><i className="dot assign" /> →n# 呼叫被指派给 n 号梯</span>
              <span><i className="dot support" /> +n# 长队列补位车</span>
              <span>▲▼ 轿厢扫描方向；开门时轿厢描边变绿</span>
              <span className="legend-block"><i className="dot blockdot" /> ✋ 门保持打开（禁动/禁重复登乘）</span>
            </div>
          </section>

          {activeResult && frame && (
            <section>
              <h2>乘客级核对 · {POLICY_META[activePol].name}
                {showBaseline ? '（无阻挡对照）' : ''}</h2>
              <PassengerAudit result={activeResult} frame={frame}
                              demand={data.demand_n} />
            </section>
          )}
        </>
      )}
    </div>
  )
}
