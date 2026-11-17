.pragma library

// Pure helpers for the whoop-poc bar widget. No QML types here, so
// contrib/omarchy/test_model.mjs can run them under node.
//
// Inputs are documents from the local API (API.md): `status` is
// whoop-poc/status@1, `summary` is whoop-poc/snapshot@3. Either may be null.

var ICON_HEART = "\u{f02d1}"       // nf-md-heart
var ICON_HEART_PULSE = "\u{f05f6}" // nf-md-heart_pulse
var ICON_HEART_OFF = "\u{f1761}"   // nf-md-heart_off
var ICON_SYNC = "\u{f04e6}"        // nf-md-sync
var ICON_ALERT = "\u{f0026}"       // nf-md-alert

var MAX_TEXT = 80

function isObj(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v)
}

function num(v) {
  return typeof v === "number" && isFinite(v) ? v : null
}

// Bounded plain string for display; API values are untrusted.
function str(v) {
  if (v === null || v === undefined) return ""
  var s = String(v)
  return s.length > MAX_TEXT ? s.slice(0, MAX_TEXT) : s
}

// 378 -> "6h18m", 45 -> "45m", null -> "".
function fmtMinutes(min) {
  var m = num(min)
  if (m === null) return ""
  m = Math.max(0, Math.round(m))
  if (m < 60) return m + "m"
  var h = Math.floor(m / 60)
  var r = m % 60
  return h + "h" + (r < 10 ? "0" : "") + r + "m"
}

// Age in minutes as a short "how long ago": "now", "12m", "1h52m", "3d".
function fmtAge(min) {
  var m = num(min)
  if (m === null) return ""
  if (m < 1) return "now"
  if (m < 48 * 60) return fmtMinutes(m)
  return Math.floor(m / 1440) + "d"
}

// Unix seconds -> "HH:MM" in local time.
function fmtClock(unix) {
  var u = num(unix)
  if (u === null) return ""
  var d = new Date(u * 1000)
  var h = d.getHours()
  var m = d.getMinutes()
  return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m
}

function liveRunning(status) {
  return isObj(status) && isObj(status.live) ? status.live : null
}

function syncRunning(status) {
  return isObj(status) && isObj(status.sync) && status.sync.running === true
}

function dataAgeMin(status) {
  return isObj(status) && isObj(status.data) ? num(status.data.age_min) : null
}

// The newest day in the summary that has a sleep record.
function lastSleep(summary) {
  if (!isObj(summary) || !Array.isArray(summary.days)) return null
  for (var i = summary.days.length - 1; i >= 0; i--) {
    var d = summary.days[i]
    if (isObj(d) && isObj(d.sleep)) return d.sleep
  }
  return null
}

// The newest workout row, as an object keyed by the summary's column names.
function lastWorkout(summary) {
  if (!isObj(summary) || !isObj(summary.workouts)) return null
  var cols = summary.workouts.cols
  var rows = summary.workouts.rows
  if (!Array.isArray(cols) || !Array.isArray(rows) || rows.length === 0) return null
  var row = rows[rows.length - 1]
  if (!Array.isArray(row)) return null
  var out = {}
  for (var i = 0; i < cols.length && i < row.length; i++) out[String(cols[i])] = row[i]
  return out
}

function latestBpm(summary) {
  return isObj(summary) && isObj(summary.latest_hr) ? num(summary.latest_hr.bpm) : null
}

// "125/138/151/164/177": Z1–Z5 lower bounds from the summary's zones block.
function fmtZoneBounds(zones) {
  if (!isObj(zones)) return ""
  var parts = []
  for (var z = 1; z <= 5; z++) {
    var v = num(zones["Z" + z])
    if (v !== null) parts.push(String(Math.round(v)))
  }
  return parts.join("/")
}

// What the bar shows.
//   apiUp      - the last /v1/status request answered
//   status     - its document (null before the first answer)
//   summary    - the last /v1/summary document, or null
//   staleMin   - data older than this many minutes is "stale"
//   showSleep  - add last sleep's asleep time after the HR
// Returns {state, icon, text, tooltip}; state is one of
// "offline", "live", "syncing", "stale", "ok", "loading".
function barState(apiUp, status, summary, staleMin, showSleep) {
  if (!apiUp) {
    return {
      state: "offline", icon: ICON_HEART_OFF, text: "--",
      tooltip: "whoop-poc API not running (systemctl --user start whoop-api)"
    }
  }
  if (!isObj(status)) {
    return { state: "loading", icon: ICON_HEART, text: "…", tooltip: "whoop-poc: loading" }
  }

  var live = liveRunning(status)
  if (live) {
    var lb = num(live.bpm)
    var ls = str(live.state)
    return {
      state: "live", icon: ICON_HEART_PULSE,
      text: (lb !== null && ls === "live" ? String(Math.round(lb)) : ls || "live"),
      tooltip: "Live HR session: " + (ls || "starting")
    }
  }

  var age = dataAgeMin(status)
  var stale = age === null || age > staleMin
  var bpm = latestBpm(summary)
  var parts = [bpm !== null ? String(Math.round(bpm)) : "--"]
  if (showSleep) {
    var sl = lastSleep(summary)
    if (sl && num(sl.asleep_min) !== null) parts.push(fmtMinutes(sl.asleep_min))
  }
  var text = parts.join(" · ")

  var tip = "Data " + (age === null ? "missing" : fmtAge(age) + " old")
  if (syncRunning(status)) {
    var p = isObj(status.sync.progress) ? status.sync.progress : null
    var pct = p ? num(p.pct) : null
    return {
      state: "syncing", icon: ICON_SYNC,
      text: text + (pct !== null ? " " + Math.round(pct) + "%" : ""),
      tooltip: tip + " · syncing"
    }
  }
  if (stale) {
    return { state: "stale", icon: ICON_ALERT, text: text, tooltip: tip + " (stale)" }
  }
  return { state: "ok", icon: ICON_HEART, text: text, tooltip: tip }
}

// One line describing the sync state for the panel.
function syncLine(status) {
  if (!isObj(status) || !isObj(status.sync)) return ""
  var s = status.sync
  if (s.running === true) {
    var p = isObj(s.progress) ? s.progress : null
    if (!p) return "Sync starting"
    var bits = ["Syncing", str(p.phase)]
    if (num(p.pct) !== null) bits.push(Math.round(p.pct) + "%")
    if (p.eta) bits.push("eta " + str(p.eta))
    return bits.filter(function (b) { return b !== "" }).join(" ")
  }
  if (isObj(s.last)) {
    var out = str(s.last.outcome) || "unknown"
    var a = num(s.last.age_min)
    return "Last sync " + out + (a !== null ? ", " + fmtAge(a) + " ago" : "")
  }
  return "Never synced"
}

// Bar segments for the panel's stacked bars: [{key, text, min, frac, shade, urgent}].
//   frac   - share of the bar's width (0 for a segment that is only in the legend)
//   shade  - opacity of the foreground colour the segment is drawn in, 0–1
//   urgent - draw it in the theme's urgent colour instead
//   text   - its legend entry
// Returns null when there is nothing to draw.
function barSegments(parts) {
  var total = 0
  parts.forEach(function (p) { total += p.min })
  if (total <= 0) return null
  return parts.map(function (p) {
    return { key: p.key, text: p.text, min: p.min, frac: p.min / total, shade: p.shade, urgent: p.urgent === true }
  })
}

// "Z4 164–176" from the summary's zones block (lower bounds Z1–Z5); "Z0 <125",
// "Z5 177+"; just "Z4" when the bounds are missing.
function zoneRange(zones, z) {
  if (!isObj(zones)) return "Z" + z
  var lo = z === 0 ? null : num(zones["Z" + z])
  var next = z === 5 ? null : num(zones["Z" + (z + 1)])
  if (z === 0) return next !== null ? "Z0 <" + Math.round(next) : "Z0"
  if (lo === null) return "Z" + z
  if (next === null) return "Z" + z + " " + Math.round(lo) + "+"
  return "Z" + z + " " + Math.round(lo) + "–" + (Math.round(next) - 1)
}

var ZONE_SHADES = [0.15, 0.3, 0.45, 0.62, 0.8, 1]

// Time in each HR zone for a workout's zone_min ({"Z1": 2, "Z3": 14}), Z0 to Z5.
// Zones with no minutes are left out; Z5 is drawn in the urgent colour.
function zoneBar(zoneMin, zones) {
  if (!isObj(zoneMin)) return null
  var parts = []
  for (var z = 0; z <= 5; z++) {
    var v = num(zoneMin["Z" + z])
    if (v === null || v <= 0) continue
    parts.push({ key: "Z" + z, min: v, shade: ZONE_SHADES[z], urgent: z === 5,
                 text: zoneRange(zones, z) + " · " + fmtMinutes(v) })
  }
  return barSegments(parts)
}

var SLEEP_STAGES = [
  { key: "deep", name: "Deep", shade: 1 },
  { key: "rem", name: "REM", shade: 0.62 },
  { key: "light", name: "Light", shade: 0.3 },
  { key: "wake", name: "Awake", shade: 0.8, urgent: true },
]

// Minutes per sleep stage from a sleep record's stage_min (the NOOP stager's
// deep/rem/light/wake). Every stage present gets a legend entry, 0 included.
function sleepBar(stageMin) {
  if (!isObj(stageMin)) return null
  var parts = []
  SLEEP_STAGES.forEach(function (st) {
    var v = num(stageMin[st.key])
    if (v === null) return
    v = Math.max(0, v)
    parts.push({ key: st.key, min: v, shade: st.shade, urgent: st.urgent === true,
                 text: st.name + " " + fmtMinutes(v) })
  })
  return barSegments(parts)
}

// Panel rows: [{label, value, bar}], each value a plain string ("" means "—").
// A row with `bar` (from zoneBar or sleepBar) is drawn as a stacked bar with
// a legend instead of text.
function detailRows(status, summary) {
  var rows = []
  var sl = lastSleep(summary)
  if (sl) {
    var v = fmtMinutes(sl.asleep_min)
    if (sl.local) v += "  " + str(sl.local)
    if (num(sl.eff_pct) !== null) v += "  " + Math.round(sl.eff_pct) + "%"
    rows.push({ label: "Last sleep", value: v })
    var sb = sleepBar(sl.stage_min)
    if (sb) rows.push({ label: "", value: "", bar: sb })
    if (num(sl.resting_hr) !== null) rows.push({ label: "", value: "RHR " + Math.round(sl.resting_hr) })
  } else {
    rows.push({ label: "Last sleep", value: "" })
  }

  var w = lastWorkout(summary)
  if (w) {
    var wv = str(w.local)
    if (num(w.duration_min) !== null) wv += "  " + fmtMinutes(w.duration_min)
    rows.push({ label: "Last workout", value: wv })
    var we = []
    if (num(w.avg_hr) !== null) we.push("avg " + Math.round(w.avg_hr))
    if (num(w.max_hr) !== null) we.push("max " + Math.round(w.max_hr))
    if (we.length) rows.push({ label: "", value: we.join(" · ") })
    var zb = zoneBar(w.zone_min, isObj(summary.zones) ? summary.zones : null)
    if (zb) rows.push({ label: "", value: "", bar: zb })
  } else {
    rows.push({ label: "Last workout", value: "" })
  }

  var zones = isObj(summary) && isObj(summary.zones) ? summary.zones : null
  if (zones) {
    var mv = "max " + (num(zones.max_hr) !== null ? Math.round(zones.max_hr) : "?")
    if (zones.max_hr_source) mv += " (" + str(zones.max_hr_source) + ")"
    if (isObj(zones.observed_peak) && num(zones.observed_peak.peak_hr_5s) !== null) {
      mv += " · peak " + Math.round(zones.observed_peak.peak_hr_5s)
    }
    rows.push({ label: "Max HR", value: mv })
    rows.push({ label: "Zones Z1–Z5", value: fmtZoneBounds(zones) })
  }

  var bat = isObj(status) && isObj(status.battery) ? num(status.battery.pct) : null
  rows.push({ label: "Strap battery", value: bat !== null ? Math.round(bat) + "%" : "" })

  var age = dataAgeMin(status)
  rows.push({ label: "Data age", value: age !== null ? fmtAge(age) : "" })
  return rows
}

// The live line for the panel, or "" when no session runs.
function liveLine(status) {
  var live = liveRunning(status)
  if (!live) return ""
  var bits = ["Live " + (str(live.state) || "starting")]
  if (num(live.bpm) !== null) bits.push(Math.round(live.bpm) + " bpm")
  if (num(live.until_unix) !== null) bits.push("until " + fmtClock(live.until_unix))
  return bits.join(" · ")
}

// Status poll interval in ms: fast while something is moving.
function pollInterval(apiUp, status, baseSec) {
  if (apiUp && liveRunning(status)) return 2000
  if (apiUp && syncRunning(status)) return 5000
  return Math.max(5, baseSec) * 1000
}

// Whether the summary should be fetched again: the strap's newest frame moved
// past what the summary holds, or it is older than maxAgeSec.
function summaryDue(status, summary, fetchedAtMs, nowMs, maxAgeSec) {
  if (!isObj(summary)) return true
  if (nowMs - fetchedAtMs > maxAgeSec * 1000) return true
  var newest = isObj(status) && isObj(status.data) ? num(status.data.newest_unix) : null
  var cursor = num(summary.cursor_unix)
  return newest !== null && cursor !== null && newest > cursor
}

// A short message from a POST response document.
function actionMessage(code, doc) {
  if (isObj(doc) && doc.error) return "Error " + code + ": " + str(doc.error)
  if (isObj(doc) && doc.message) return str(doc.message)
  if (isObj(doc) && doc.stopped !== undefined) return doc.stopped ? "Live session stopped" : "No live session was running"
  if (code === 0) return "API not reachable"
  return code >= 200 && code < 300 ? "OK" : "HTTP " + code
}
