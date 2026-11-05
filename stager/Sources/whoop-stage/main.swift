// whoop-stage: run NOOP's SleepStager (StrandAnalytics) on decoded streams.
//   stdin : {"hr":[{ts,bpm}], "rr":[{ts,rrMs}], "resp":[{ts,raw}], "gravity":[{ts,x,y,z}],
//            "tzOffsetSeconds":-18000, "useSleepStagerV2":false}
//   stdout: [{"start","end","efficiency","restingHR","avgHRV","stages":[{start,end,stage}]}]
// Mirrors AnalyticsEngine's call (wristOff/bandSleepState empty: WHOOP 4 v12 has no band sleep_state).
import Foundation
import StrandAnalytics
import WhoopProtocol

struct Resp: Decodable { let ts: Int; let raw: Int }
struct Grav: Decodable { let ts: Int; let x: Double; let y: Double; let z: Double }
struct Input: Decodable {
    let hr: [HRSample]
    let rr: [RRInterval]?
    let resp: [Resp]?
    let gravity: [Grav]
    let tzOffsetSeconds: Int?
    let useSleepStagerV2: Bool?
}
struct Session: Encodable {
    let start: Int, end: Int, efficiency: Double
    let restingHR: Int?, avgHRV: Double?
    let stages: [StageSegment]
}

let data = FileHandle.standardInput.readDataToEndOfFile()
let input: Input
do { input = try JSONDecoder().decode(Input.self, from: data) } catch {
    FileHandle.standardError.write("whoop-stage: bad input JSON: \(error)\n".data(using: .utf8)!)
    exit(2)
}
var trace: ((String) -> Void)? = nil
if CommandLine.arguments.contains("--trace") {
    trace = { FileHandle.standardError.write(($0 + "\n").data(using: .utf8)!) }
}
let sessions = SleepStager.detectSleep(
    hr: input.hr,
    rr: input.rr ?? [],
    resp: (input.resp ?? []).map { RespSample(ts: $0.ts, raw: $0.raw) },
    gravity: input.gravity.map { GravitySample(ts: $0.ts, x: $0.x, y: $0.y, z: $0.z) },
    tzOffsetSeconds: input.tzOffsetSeconds ?? 0,
    useSleepStagerV2: input.useSleepStagerV2 ?? false,
    traceSink: trace)
let out = sessions.map { Session(start: $0.start, end: $0.end, efficiency: $0.efficiency,
                                 restingHR: $0.restingHR, avgHRV: $0.avgHRV, stages: $0.stages) }
let enc = JSONEncoder()
enc.outputFormatting = [.sortedKeys]
FileHandle.standardOutput.write(try enc.encode(out))
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
