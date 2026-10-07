// 事件日志确定性重放：把后端 SimPy 事件流转成逐帧世界状态。
// 不生成任何随机运动 —— 轿厢位置、乘客位置全部由日志事件推导。

export const EVENT_TYPES = {
  arrive: 'pax_arrive',
  board: 'pax_board',
  alight: 'pax_alight',
  move: 'car_move',
  stop: 'car_stop',
  doorsOpen: 'doors_open',
  doorsClose: 'doors_close',
  idle: 'car_idle',
  dir: 'car_dir',
  full: 'car_full',
  reverse: 'car_reverse_at_floor',
  assign: 'call_assign',
  release: 'call_release',
  support: 'call_support',
  skip: 'car_skip',
  unserved: 'pax_unserved',
}

// 生成离散关键帧（每条事件一帧）。返回帧序列，每帧是世界快照。
export function buildFrames(events, { floors, carCount, capacity }) {
  const cars = []
  for (let i = 0; i < carCount; i++) {
    cars.push({ id: i, floor: 1, dir: 0, doors: 'closed', load: 0, dests: [] })
  }
  // 乘客状态：pid -> {pid, origin, dest, wanted, location, car, floor, status}
  const pax = new Map()
  // 候梯队列：floor -> {up:[], down:[]}
  const halls = {}
  for (let f = 1; f <= floors; f++) halls[f] = { up: [], down: [] }
  const assignments = {} // `${floor}:${dir}` -> carId
  const supports = {}    // `${floor}:${dir}` -> Set(carId)

  const snapshot = (t) => ({
    t,
    cars: cars.map(c => ({ ...c, dests: [...c.dests] })),
    halls: Object.fromEntries(
      Object.entries(halls).map(([f, v]) => [f, { up: [...v.up], down: [...v.down] }])),
    pax: new Map(pax),
    assignments: { ...assignments },
    supports: Object.fromEntries(Object.entries(supports).map(([k, v]) => [k, new Set(v)])),
  })

  const frames = [snapshot(0)]
  const counts = { board: 0, alight: 0, unserved: 0, full: 0 }

  for (const e of events) {
    switch (e.type) {
      case EVENT_TYPES.idle: {
        const c = cars[e.car]
        c.floor = e.floor; c.dir = 0; c.load = e.load
        break
      }
      case EVENT_TYPES.dir: {
        const c = cars[e.car]
        c.floor = e.floor; c.dir = e.dir; c.load = e.load ?? c.load
        break
      }
      case EVENT_TYPES.move: {
        const c = cars[e.car]
        c.floor = e.floor; c.dir = e.dir
        c.load = e.load ?? c.load
        c.doors = 'closed'
        // 车内乘客随轿厢更新楼层快照（位置仍唯一：car 内）
        for (const p of pax.values()) {
          if (p.location === 'car' && p.car === c.id) p.floor = e.floor
        }
        break
      }
      case EVENT_TYPES.stop: {
        cars[e.car].floor = e.floor
        cars[e.car].load = e.load ?? cars[e.car].load
        break
      }
      case EVENT_TYPES.reverse: {
        cars[e.car].dir = e.dir
        break
      }
      case EVENT_TYPES.doorsOpen: {
        cars[e.car].doors = 'open'
        break
      }
      case EVENT_TYPES.doorsClose: {
        cars[e.car].doors = 'closed'
        cars[e.car].load = e.load ?? cars[e.car].load
        break
      }
      case EVENT_TYPES.arrive: {
        const p = {
          pid: e.pid, origin: e.origin, dest: e.dest, wanted: e.wanted,
          location: 'hall', car: null, floor: e.origin,
          arrivalTime: e.t, status: 'waiting',
        }
        pax.set(e.pid, p)
        const q = e.wanted === 1 ? halls[e.origin].up : halls[e.origin].down
        q.push(e.pid)
        break
      }
      case EVENT_TYPES.board: {
        const p = pax.get(e.pid)
        const qKey = e.wanted === 1 ? 'up' : 'down'
        const q = halls[e.floor][qKey]
        const idx = q.indexOf(e.pid)
        if (idx >= 0) q.splice(idx, 1)
        p.location = 'car'; p.car = e.car; p.floor = e.floor
        p.boardTime = e.t; p.status = 'aboard'
        const c = cars[e.car]
        c.load = e.load
        if (!c.dests.includes(e.dest)) c.dests.push(e.dest)
        counts.board++
        break
      }
      case EVENT_TYPES.alight: {
        const p = pax.get(e.pid)
        p.location = 'outside'; p.car = null; p.floor = e.floor
        p.alightTime = e.t; p.status = 'done'
        const c = cars[e.car]
        c.load = e.load
        // 到达该层后清除内呼
        if (c.load === 0 || !pax.values().some(
          x => x.location === 'car' && x.car === c.id && x.dest === e.floor)) {
          c.dests = c.dests.filter(d => d !== e.floor)
        }
        counts.alight++
        break
      }
      case EVENT_TYPES.full: {
        counts.full++
        break
      }
      case EVENT_TYPES.assign: {
        assignments[`${e.floor}:${e.dir}`] = e.car
        break
      }
      case EVENT_TYPES.release: {
        delete assignments[`${e.floor}:${e.dir}`]
        break
      }
      case EVENT_TYPES.support: {
        const k = `${e.floor}:${e.dir}`
        if (!supports[k]) supports[k] = new Set()
        supports[k].add(e.car)
        break
      }
      case EVENT_TYPES.unserved: {
        const p = pax.get(e.pid)
        if (p) {
          if (e.where === 'hall') {
            p.location = 'hall'; p.floor = e.floor; p.status = 'unserved'
          } else {
            p.location = 'car'; p.car = e.car; p.status = 'unserved'
          }
        }
        counts.unserved++
        break
      }
      default:
        break
    }
    frames.push(snapshot(e.t))
  }

  return { frames, counts }
}

// 从帧序列中重建单个乘客的位置迁移链（供"乘客轨迹核对"面板）
export function passengerTimeline(events, pid) {
  const chain = []
  for (const e of events) {
    if (e.pid !== pid) continue
    let location = null
    if (e.type === EVENT_TYPES.arrive) location = `候梯厅 ${e.origin}层`
    else if (e.type === EVENT_TYPES.board) location = `${e.car}号轿厢内（${e.floor}层登乘）`
    else if (e.type === EVENT_TYPES.alight) location = `系统外 ${e.floor}层（到达）`
    else if (e.type === EVENT_TYPES.unserved) {
      location = e.where === 'hall' ? `候梯厅 ${e.floor}层（仿真结束仍未服务）`
        : `${e.car}号轿厢内（未到达 ${e.dest}层）`
    }
    if (location) chain.push({ t: e.t, type: e.type, location })
  }
  return chain
}

// 逐时刻校验：给定帧，检查每名乘客是否恰好占一个物理位置（纯前端二次核对）
export function auditFrame(frame) {
  const issues = []
  for (const [pid, p] of frame.pax) {
    if (!['hall', 'car', 'outside'].includes(p.location)) {
      issues.push(`乘客 ${pid} 位置非法: ${p.location}`)
    }
    if (p.location === 'car' && p.car == null) {
      issues.push(`乘客 ${pid} 在轿厢内但无轿厢编号`)
    }
  }
  // 容量
  for (const c of frame.cars) {
    if (c.load < 0) issues.push(`${c.id}号轿厢载荷为负`)
  }
  return issues
}
