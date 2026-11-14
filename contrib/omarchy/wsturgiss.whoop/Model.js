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

// "Z1 6 · Z3 14" from {"Z1": 6, "Z3": 14}; zones with no minutes are left out.
function fmtZoneMinutes(zoneMin) {
  if (!isObj(zoneMin)) return ""
  var parts = []
  for (var z = 0; z <= 5; z++) {
    var v = num(zoneMin["Z" + z])
    if (v !== null && v > 0) parts.push("Z" + z + " " + Math.round(v))
  }
  return parts.join(" · ")
}

// "Z1 125 · Z2 138 · …" lower bounds from the summary's zones block.
function fmtZoneBounds(zones) {
  if (!isObj(zones)) return ""
  var parts = []
  for (var z = 1; z <= 5; z++) {
    var v = num(zones["Z" + z])
    if (v !== null) parts.push("Z" + z + " " + Math.round(v))
  }
  return parts.join(" · ")
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

// Panel rows: [{label, value}], each value a plain string ("" means "—").
function detailRows(status, summary) {
  var rows = []
  var sl = lastSleep(summary)
  if (sl) {
    var v = fmtMinutes(sl.asleep_min)
    if (sl.local) v += "  " + str(sl.local)
    if (num(sl.eff_pct) !== null) v += "  " + Math.round(sl.eff_pct) + "%"
    rows.push({ label: "Last sleep", value: v })
    var st = isObj(sl.stage_min) ? sl.stage_min : null
    var extra = []
    if (st) {
      ["deep", "rem", "light", "wake"].forEach(function (k) {
        if (num(st[k]) !== null) extra.push(k + " " + Math.round(st[k]))
      })
    }
    if (num(sl.resting_hr) !== null) extra.push("RHR " + Math.round(sl.resting_hr))
    if (extra.length) rows.push({ label: "", value: extra.join(" · ") })
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
    var zm = fmtZoneMinutes(w.zone_min)
    if (zm) we.push(zm)
    if (we.length) rows.push({ label: "", value: we.join(" · ") })
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
    rows.push({ label: "Zones", value: fmtZoneBounds(zones) })
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
