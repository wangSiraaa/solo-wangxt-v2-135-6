import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, POLICY_META } from './api'
import Shaft from './components/Shaft.jsx'
import MetricsTable from './components/MetricsTable.jsx'
import PassengerAudit from './components/PassengerAudit.jsx'
import { buildFrames } from './engine/replay.js'

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
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [activePol, setActivePol] = useState('collective')
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

  const run = async () => {
    setLoading(true); setError(null); setData(null)
    try {
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
      }
      const res = await api.simulate(body)
      setData(res)
      setActivePol(res.results[0].policy)
      setFrameIdx(0)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const activeResult = data?.results.find(r => r.policy === activePol)

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
        {error && <div className="error">{error}</div>}
        {data && (
          <div className="same-flow-note">
            ✓ 三种策略使用<b>完全相同的 {data.demand_n} 名乘客到达序列</b>
            （种子 {data.params.seed}），仅派梯决策不同
            {data.persisted ? '；结果与事件日志已写入 PostgreSQL' : ''}
          </div>
        )}
      </section>

      {data && (
        <>
          <section>
            <h2>指标对比（候梯 / 乘梯 / 总行程分列，未服务不剔除）</h2>
            <MetricsTable comparison={data.comparison} results={data.results} />
          </section>

          <section className="playback">
            <div className="play-head">
              <h2>井道与候梯队列（事件日志重放）</h2>
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
            </div>
          </section>

          {activeResult && frame && (
            <section>
              <h2>乘客级核对 · {POLICY_META[activePol].name}</h2>
              <PassengerAudit result={activeResult} frame={frame}
                              demand={data.demand_n} />
            </section>
          )}
        </>
      )}
    </div>
  )
}
