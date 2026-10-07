import React from 'react'

const FLOOR_PX = 46

function FloorLabel({ floors }) {
  return (
    <div className="floor-labels">
      {Array.from({ length: floors }, (_, i) => floors - i).map(f => (
        <div key={f} className="floor-label">{f}F</div>
      ))}
    </div>
  )
}

function CarShaft({ car, floors, capacity }) {
  // 位置用连续像素：按当前层与行进方向插值交给 CSS transition
  const top = (floors - car.floor) * FLOOR_PX
  const loadPct = Math.min(100, Math.round((car.load / capacity) * 100))
  const dirGlyph = car.blocked ? '✋'
    : car.dir === 1 ? '▲' : car.dir === -1 ? '▼' : '■'
  const doorClass = car.blocked ? 'doors-blocked'
    : car.doors === 'open' ? 'doors-open' : 'doors-closed'
  return (
    <div className="shaft">
      {Array.from({ length: floors }, (_, i) => floors - i).map(f => (
        <div key={f} className={`shaft-floor ${f === 1 ? 'lobby' : ''}`}>
          <span className="rail" />
        </div>
      ))}
      <div className={`car ${doorClass}`} style={{ top }}
           title={car.blocked
             ? `门被挡住保持打开：${car.blocked.enterT?.toFixed(1)}s → ${car.blocked.end.toFixed(1)}s`
             : undefined}>
        <div className="car-head">
          <span className="car-dir">{dirGlyph}</span>
          <span className="car-id">{car.id}#</span>
        </div>
        <div className="car-body">
          <div className="car-load">{car.load}/{capacity}</div>
          <div className="load-bar"><i style={{ width: `${loadPct}%` }} /></div>
        </div>
        {car.blocked && (
          <div className="car-block-tag">
            门阻挡 {car.blocked.end - car.blocked.enterT > 0
              ? `至 ${car.blocked.end.toFixed(0)}s` : ''}
          </div>
        )}
        <div className="car-dests">
          {car.dests.sort((a, b) => a - b).map(d => (
            <span key={d} className="dest-lamp">{d}</span>
          ))}
        </div>
      </div>
    </div>
  )
}

function HallQueue({ floor, dir, pids, paxById, assigned, supportCars }) {
  if (pids.length === 0 && assigned == null) return null
  const arrow = dir === 1 ? '▲' : '▼'
  return (
    <div className={`hall-q ${dir === 1 ? 'up' : 'down'}`}>
      <div className="hall-call">
        <span className="hall-arrow">{arrow}</span>
        {assigned != null && <span className="assign-badge">→{assigned}#</span>}
        {supportCars?.size > 0 && (
          <span className="support-badge">+{[...supportCars].join(',')}#</span>
        )}
      </div>
      <div className="hall-pax">
        {pids.map(pid => {
          const p = paxById.get(pid)
          return <span key={pid} className="pax-chip" title={`#${pid} → ${p?.dest}F`}>
            {pid}
          </span>
        })}
      </div>
    </div>
  )
}

export default function Shaft({ frame, floors, carCount, capacity, policy }) {
  const paxById = frame.pax
  const blocksNow = frame.cars
    .map(c => c.blocked)
    .filter(Boolean)
  const blocksByFloor = {}
  for (const b of blocksNow) blocksByFloor[b.floor] = b
  return (
    <div className="shaft-wrap">
      <FloorLabel floors={floors} />
      <div className="shafts-row">
        {Array.from({ length: carCount }, (_, i) => (
          <CarShaft key={i} car={frame.cars[i]} floors={floors}
                    capacity={capacity} />
        ))}
      </div>
      <div className="halls">
        {Array.from({ length: floors }, (_, i) => floors - i).map(f => {
          const h = frame.halls[f]
          const block = blocksByFloor[f]
          return (
            <div key={f} className={`hall-row ${block ? 'blocked-row' : ''}`}>
              <HallQueue floor={f} dir={1} pids={h.up} paxById={paxById}
                         assigned={frame.assignments[`${f}:1`]}
                         supportCars={frame.supports[`${f}:1`]} />
              <HallQueue floor={f} dir={-1} pids={h.down} paxById={paxById}
                         assigned={frame.assignments[`${f}:-1`]}
                         supportCars={frame.supports[`${f}:-1`]} />
              {block && (
                <span className="floor-block-badge"
                      title={`${block.enterT?.toFixed(1)}s–${block.end.toFixed(1)}s 门保持打开，禁动/禁重复登乘`}>
                  ✋ {block.blockId}号阻挡 · {block.enterT?.toFixed(0)}s–{block.end.toFixed(0)}s
                </span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

export { FLOOR_PX }
