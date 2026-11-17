// Tests for the bar widget's Model.js. Run: node contrib/omarchy/test_model.mjs
// Model.js is a QML `.pragma library` script; the pragma line is dropped and
// the rest evaluated as plain JavaScript.
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"
import vm from "node:vm"
import assert from "node:assert/strict"

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(here, "wsturgiss.whoop", "Model.js"), "utf8").replace(/^\.pragma library\s*$/m, "")
const M = {}
vm.runInNewContext(src, M)

const status = (over = {}) => ({
  schema: "whoop-poc/status@1", now_unix: 1000, demo: false,
  data: { newest_unix: 900, age_min: 3 },
  battery: { pct: 68, unix: 900 },
  sync: { running: false, progress: null, last: { outcome: "complete", finished_unix: 800, age_min: 12 } },
  live: null,
  ...over,
})

const summary = {
  schema: "whoop-poc/snapshot@3", cursor_unix: 900,
  latest_hr: { unix: 900, bpm: 63 },
  zones: { max_hr: 190, Z1: 125, Z2: 138, Z3: 151, Z4: 164, Z5: 177, max_hr_source: "default", observed_peak: { peak_hr_5s: 182 } },
  days: [
    { date: "a", sleep: { asleep_min: 400, local: "23:00-06:00", eff_pct: 95, resting_hr: 60, stage_min: { deep: 10, rem: 20, light: 360, wake: 10 } } },
    { date: "b", no_data: true },
  ],
  workouts: { cols: ["start_unix", "local", "duration_min", "avg_hr", "max_hr", "zone_min"], rows: [[1, "11-11 08:24-08:46", 20, 157, 172, { Z1: 2, Z3: 14 }]] },
}

const tests = {
  fmtMinutes() {
    assert.equal(M.fmtMinutes(432), "7h12m")
    assert.equal(M.fmtMinutes(61), "1h01m")
    assert.equal(M.fmtMinutes(45), "45m")
    assert.equal(M.fmtMinutes(null), "")
  },
  fmtAge() {
    assert.equal(M.fmtAge(0), "now")
    assert.equal(M.fmtAge(112), "1h52m")
    assert.equal(M.fmtAge(3 * 1440 + 5), "3d")
  },
  barOffline() {
    const b = M.barState(false, status(), summary, 60, true)
    assert.equal(b.state, "offline")
    assert.equal(b.text, "--")
  },
  barOk() {
    const b = M.barState(true, status(), summary, 60, true)
    assert.equal(b.state, "ok")
    assert.equal(b.text, "63 · 6h40m")
    assert.equal(M.barState(true, status(), summary, 60, false).text, "63")
  },
  barStale() {
    assert.equal(M.barState(true, status({ data: { newest_unix: 900, age_min: 120 } }), summary, 60, true).state, "stale")
    assert.equal(M.barState(true, status({ data: { newest_unix: null, age_min: null } }), summary, 60, true).state, "stale")
  },
  barSyncing() {
    const s = status({ sync: { running: true, progress: { phase: "draining", pct: 40, eta: "2m" }, last: null } })
    const b = M.barState(true, s, summary, 60, true)
    assert.equal(b.state, "syncing")
    assert.equal(b.text, "63 · 6h40m 40%")
    assert.equal(M.syncLine(s), "Syncing draining 40% eta 2m")
  },
  barLive() {
    const s = status({ live: { state: "live", bpm: 88.4, until_unix: 2000 } })
    const b = M.barState(true, s, summary, 60, true)
    assert.equal(b.state, "live")
    assert.equal(b.text, "88")
    assert.equal(M.barState(true, status({ live: { state: "connecting" } }), summary, 60, true).text, "connecting")
    assert.match(M.liveLine(s), /^Live live · 88 bpm · until \d\d:\d\d$/)
  },
  barNoSummary() {
    assert.equal(M.barState(true, status(), null, 60, true).text, "--")
  },
  detailRows() {
    const rows = M.detailRows(status(), summary)
    const by = Object.fromEntries(rows.filter(r => r.label).map(r => [r.label, r.value]))
    assert.equal(by["Last sleep"], "6h40m  23:00-06:00  95%")
    assert.equal(by["Last workout"], "11-11 08:24-08:46  20m")
    assert.equal(by["Max HR"], "max 190 (default) · peak 182")
    assert.equal(by["Zones Z1–Z5"], "125/138/151/164/177")
    assert.equal(by["Strap battery"], "68%")
    assert.equal(by["Data age"], "3m")
    assert.ok(rows.some(r => r.value === "avg 157 · max 172"))
    assert.ok(rows.some(r => r.value === "RHR 60"))
    const bars = rows.filter(r => r.bar)
    assert.equal(bars.length, 2)
    assert.deepEqual([...bars[0].bar.map(b => b.key)], ["deep", "rem", "light", "wake"])
    assert.deepEqual([...bars[1].bar.map(b => b.text)], ["Z1 125–137 · 2m", "Z3 151–163 · 14m"])
  },
  zoneBar() {
    const zb = M.zoneBar({ Z0: 0, Z1: 2, Z2: 2, Z3: 14, Z4: 4, Z5: 2 }, summary.zones)
    assert.deepEqual([...zb.map(b => b.key)], ["Z1", "Z2", "Z3", "Z4", "Z5"])
    assert.equal(zb[3].text, "Z4 164–176 · 4m")
    assert.equal(zb[4].text, "Z5 177+ · 2m")
    assert.equal(zb[4].urgent, true)
    assert.ok(Math.abs(zb.reduce((a, b) => a + b.frac, 0) - 1) < 1e-9)
    assert.equal(zb[2].frac, 14 / 24)
    assert.equal(M.zoneBar({ Z0: 5 }, null)[0].text, "Z0 · 5m")
    assert.equal(M.zoneRange(summary.zones, 0), "Z0 <125")
    assert.equal(M.zoneBar({}, summary.zones), null)
    assert.equal(M.zoneBar(null, summary.zones), null)
    assert.equal(M.zoneBar({ Z3: "x" }, summary.zones), null)
  },
  sleepBar() {
    const sb = M.sleepBar({ wake: 0, rem: 49, light: 378, deep: 0 })
    assert.deepEqual([...sb.map(b => b.text)], ["Deep 0m", "REM 49m", "Light 6h18m", "Awake 0m"])
    assert.equal(sb[0].frac, 0)
    assert.equal(sb[2].frac, 378 / 427)
    assert.equal(M.sleepBar({ deep: 0, rem: 0, light: 0, wake: 0 }), null)
    assert.equal(M.sleepBar(undefined), null)
  },
  detailRowsEmpty() {
    const rows = M.detailRows(null, null)
    assert.ok(rows.every(r => typeof r.value === "string"))
  },
  untrustedStringsAreBounded() {
    const long = "<img src=x>".repeat(50)
    const s = status({ sync: { running: false, last: { outcome: long, age_min: 1 } } })
    assert.ok(M.syncLine(s).length < 120)
  },
  pollInterval() {
    assert.equal(M.pollInterval(true, status(), 15), 15000)
    assert.equal(M.pollInterval(true, status({ live: { state: "live" } }), 15), 2000)
    assert.equal(M.pollInterval(true, status({ sync: { running: true } }), 15), 5000)
    assert.equal(M.pollInterval(false, status({ live: { state: "live" } }), 15), 15000)
    assert.equal(M.pollInterval(true, status(), 1), 5000)
  },
  summaryDue() {
    assert.equal(M.summaryDue(status(), null, 0, 0, 900), true)
    assert.equal(M.summaryDue(status(), summary, 0, 1000, 900), false)
    assert.equal(M.summaryDue(status({ data: { newest_unix: 950, age_min: 1 } }), summary, 0, 1000, 900), true)
    assert.equal(M.summaryDue(status(), summary, 0, 901000, 900), true)
  },
  actionMessage() {
    assert.equal(M.actionMessage(202, { started: true, message: "sync started" }), "sync started")
    assert.equal(M.actionMessage(415, { error: "POST needs Content-Type: application/json" }), "Error 415: POST needs Content-Type: application/json")
    assert.equal(M.actionMessage(200, { stopped: false }), "No live session was running")
    assert.equal(M.actionMessage(0, null), "API not reachable")
  },
}

let failed = 0
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); console.log("ok   " + name) } catch (e) { failed++; console.log("FAIL " + name + "\n  " + e.message) }
}
if (failed) { console.log(failed + " failed"); process.exit(1) }
console.log("all " + Object.keys(tests).length + " passed")
