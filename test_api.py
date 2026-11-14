"""api.py (the local JSON API) and client.py (its in-process fallback). Demo data and fakes only."""
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import api
import client
import coach
import whoop_poc as w

DEMO = SimpleNamespace(db=Path("/nonexistent"), address=None, demo=True, heuristic=True, days=7)
REAL = SimpleNamespace(**{**vars(DEMO), "demo": False})
TOKEN = "t" * 43
T0 = 1_762_000_000 - 1_762_000_000 % 60     # a minute boundary


def trace_samples(*legs, t0=T0):
    """Per-second samples from legs (seconds, bpm), still wrist."""
    out, t = [], t0
    for n, bpm in legs:
        out += [w.Sample(t + i, bpm, 0.1, -0.4, 0.91) for i in range(n)]
        t += n
    return out


def fake_build(samples, newest=None):
    """A w.build_report over synthetic samples; newest_unix (the newest frame in whoop.db) defaults to the
    last sample's."""
    def build(db, hours, address, now=None, demo=False, heuristic=False):
        since = now - int(hours * 3600)
        ss = [x for x in samples if since <= x.unix <= now + 300]
        meta = {"address": "AA:BB", "name": "test", "labels": [], "sleep_source": "heuristic",
                "newest_unix": samples[-1].unix if newest is None else newest}
        return w.Report(meta, since, now, ss, w.per_minute(ss), [])
    return build


class StateDir(unittest.TestCase):
    """Every test runs against an empty state dir (no observed max, no live session files)."""

    def setUp(self):
        self.addCleanup(w.refresh_max_hr)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.object(w, "STATE_DIR", Path(tmp.name)), mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        for k in ("WHOOP_MAX_HR", "WHOOP_API_PORT", "WHOOP_API_TOKEN"):
            os.environ.pop(k, None)
        w.refresh_max_hr()


class Dispatch(StateDir):
    def test_unknown_path_and_method(self):
        self.assertEqual(api.dispatch(DEMO, "GET", "/v1/nope")[0], 404)
        self.assertEqual(api.dispatch(DEMO, "POST", "/v1/status")[0], 405)
        self.assertEqual(api.dispatch(DEMO, "GET", "/v1/sync")[0], 405)

    def test_bad_query_is_400(self):
        for q in ({"hours": "x"}, {"hours": "0"}, {"hours": "nan"}, {"bins": "3"}, {"series": "seconds"}):
            code, doc = api.dispatch(DEMO, "GET", "/v1/report", q)
            self.assertEqual(code, 400, q)
            self.assertIn("error", doc)

    def test_handler_crash_is_500(self):
        with mock.patch.dict(api.ROUTES, {("GET", "/v1/health"): (lambda *a: 1 / 0, False)}):
            code, doc = api.dispatch(DEMO, "GET", "/v1/health")
        self.assertEqual((code, doc["error"]), (500, "ZeroDivisionError: division by zero"))

    def test_health_and_status(self):
        self.assertEqual(api.dispatch(DEMO, "GET", "/healthz"), (200, {"ok": True, "demo": True}))
        self.assertEqual(api.dispatch(DEMO, "GET", "/v1/health")[0], 200)
        cfg = SimpleNamespace(**{**vars(DEMO), "demo": False})
        with mock.patch.object(w, "drain_status", return_value={"phase": "draining", "pct": 40}), \
                mock.patch.object(w, "live_status", return_value=None):
            code, doc = api.dispatch(cfg, "GET", "/v1/status")
        self.assertEqual(code, 200)
        self.assertEqual(set(doc), {"schema", "now_unix", "demo", "data", "battery", "sync", "live"})
        self.assertEqual(doc["sync"], {"running": True, "progress": {"phase": "draining", "pct": 40}, "last": None})
        self.assertEqual(doc["data"], {"newest_unix": None, "age_min": None})        # no db

    def test_report_shape_and_compact_series(self):
        code, doc = api.dispatch(DEMO, "GET", "/v1/report", {"hours": "12", "bins": "100"})
        self.assertEqual(code, 200)
        self.assertEqual(set(doc), {"schema", "demo", "device", "window", "newest_unix", "hr", "zones", "max_hr",
                                    "sleep", "workouts"})
        self.assertEqual(doc["schema"], "whoop-poc/report@1")
        self.assertEqual((doc["max_hr"]["max_hr"], doc["max_hr"]["source"]), (190, "default"))
        self.assertEqual(doc["zones"]["max_hr_source"], "default")
        series = doc["hr"]["series"]
        self.assertEqual((series["agg"], series["bins"]), ("max", 100))
        self.assertLessEqual(len(series["points"]), 100)
        minute = api.dispatch(DEMO, "GET", "/v1/report", {"hours": "12", "series": "minute"})[1]["hr"]["series"]
        self.assertGreater(len(minute["points"]), 5 * len(series["points"]))      # ~600 worn minutes
        self.assertTrue(doc["sleep"]["sessions"])
        self.assertLess(len(json.dumps(doc)), 12_000)

    def test_series_draws_the_same_chart_as_every_minute(self):
        import coach
        r = coach.report_with_workouts(None, 24, None, demo=True, heuristic=True)
        for cols in (50, 116, 200):
            pts = [tuple(p) for p in api.hr_series(r, cols, False)["points"]]
            full = [(m.t, m.hr) for m in r.hr_minutes]
            draw = lambda p: w.zone_chart(p, cols + 4, 6, t0=r.since, t1=r.until, agg="max", gap_fill_s=61)
            self.assertEqual(draw(pts), draw(full), cols)

    def test_sleep_window_round_trip(self):
        import coach
        r = coach.report_with_workouts(None, 24, None, demo=True, heuristic=True)       # heuristic minutes
        s = r.sleep[-1]
        back = api.sleep_window(api.sleep_doc(r)["last"])
        self.assertEqual((w.sleep_hours(back), [len(b) and sum(m.asleep for m in b) for b in w.bucket(back.minutes, s.start, s.end, 80)]),
                         (w.sleep_hours(s), [len(b) and sum(m.asleep for m in b) for b in w.bucket(s.minutes, s.start, s.end, 80)]))
        st = [{"start": 0, "end": 1800, "stage": "light"}, {"start": 1800, "end": 3600, "stage": "deep"}]
        staged = w.SleepWindow(0, 3600, [], stages=st)
        r.sleep, r.meta["sleep_source"] = [staged], "noop"
        back = api.sleep_window(api.sleep_doc(r)["last"])
        self.assertEqual((w.stage_bar(back, 30), w.sleep_hours(back)), (w.stage_bar(staged, 30), w.sleep_hours(staged)))

    def test_live_incremental_samples(self):
        (w.STATE_DIR / "live-samples.csv").write_text("100,60\n101,61\n102,62\n")
        cfg = SimpleNamespace(**{**vars(DEMO), "demo": False})
        full = api.dispatch(cfg, "GET", "/v1/live")[1]
        self.assertEqual((full["full"], full["session_start_unix"], full["samples"]), (True, 100, [[100, 60], [101, 61], [102, 62]]))
        new = api.dispatch(cfg, "GET", "/v1/live", {"session": "100", "since": "101"})[1]
        self.assertEqual((new["full"], new["samples"]), (False, [[102, 62]]))
        other = api.dispatch(cfg, "GET", "/v1/live", {"session": "99", "since": "101"})[1]   # a new session
        self.assertEqual((other["full"], len(other["samples"])), (True, 3))


class Windows(StateDir):
    """/v1/hr and /v1/workouts over a synthetic morning: 30 min easy, 20 min at 150, 30 min easy."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(w, "build_report", fake_build(trace_samples((1800, 70), (1200, 150), (1800, 70))))
        p.start()
        self.addCleanup(p.stop)

    def get(self, path, **q):
        return api.dispatch(REAL, "GET", path, {k: str(v) for k, v in q.items()})

    def test_workouts_lists_the_high_hr_period_with_stats(self):
        code, doc = self.get("/v1/workouts", since=T0 - 3600, until=T0 + 4700)
        self.assertEqual(code, 200)
        self.assertEqual(doc["schema"], "whoop-poc/workouts@1")
        self.assertEqual(doc["window"], {"since_unix": T0 - 3600, "until_unix": T0 + 4700})
        self.assertTrue(doc["covered"])
        (x,) = doc["workouts"]
        self.assertEqual((x["start_unix"], x["end_unix"], x["duration_min"]), (T0 + 1800, T0 + 3000, 20))
        self.assertEqual((x["avg_hr"], x["max_hr"], x["peak_hr_5s"]), (150, 150, 150))
        self.assertEqual(x["id"], coach.workout_id(T0 + 1800))
        self.assertEqual(x["zone_sec"], {"Z0": 0, "Z1": 0, "Z2": 1200, "Z3": 0, "Z4": 0, "Z5": 0})
        for k in coach.TUI_WORKOUT_KEYS:
            self.assertIn(k, x)

    def test_workouts_only_those_starting_in_the_window(self):
        self.assertEqual(self.get("/v1/workouts", since=T0 + 2000, until=T0 + 4800)[1]["workouts"], [])

    def test_not_covered_until_whoop_db_reaches_the_window_end(self):
        doc = self.get("/v1/workouts", since=T0, until=T0 + 9000)[1]
        self.assertFalse(doc["covered"])         # newest sample is T0 + 4799: a later sync may add more
        self.assertEqual(doc["newest_unix"], T0 + 4799)

    def test_hr_window_stats(self):
        code, doc = self.get("/v1/hr", since=T0 + 1800, until=T0 + 3000)
        self.assertEqual(code, 200)
        self.assertEqual(doc["schema"], "whoop-poc/hr-window@1")
        self.assertTrue(doc["covered"])
        hr = doc["hr"]
        self.assertEqual((hr["stats"]["avg"], hr["stats"]["max"], hr["stats"]["worn_min"]), (150, 150, 20))
        self.assertEqual((hr["peak_hr_5s"], hr["peak_hr_1s"]), (150, 150))
        self.assertEqual(hr["zone_sec"]["Z2"], 1200)
        self.assertEqual(sum(hr["zone_sec"].values()), 1200)
        wide = self.get("/v1/hr", since=T0, until=T0 + 4800)[1]["hr"]
        self.assertEqual((wide["zone_sec"]["Z0"], wide["zone_sec"]["Z2"], wide["stats"]["worn_min"]), (3600, 1200, 80))

    def test_hr_window_with_no_data(self):
        doc = self.get("/v1/hr", since=T0 - 7200, until=T0 - 3600)[1]
        self.assertIsNone(doc["hr"]["stats"])
        self.assertIsNone(doc["hr"]["peak_hr_5s"])
        self.assertTrue(doc["covered"])

    def test_bad_windows(self):
        for path, q in (("/v1/hr", {"since": T0, "until": T0}), ("/v1/hr", {"since": T0, "until": T0 + 86401}),
                        ("/v1/hr", {"since": "x"}), ("/v1/workouts", {"since": T0, "until": T0 + 32 * 86400})):
            code, doc = self.get(path, **q)
            self.assertEqual(code, 400, (path, q))
            self.assertIn("error", doc)

    def test_demo_shapes(self):
        for path in ("/v1/hr", "/v1/workouts"):
            code, doc = api.dispatch(DEMO, "GET", path)
            self.assertEqual((code, doc["demo"]), (200, True), path)
        self.assertTrue(api.is_heavy("GET", "/v1/hr") and api.is_heavy("GET", "/v1/workouts"))


class Actions(StateDir):
    def test_sync(self):
        with mock.patch.object(w, "drain_running", return_value=True), \
                mock.patch.object(w, "drain_status", return_value={"phase": "connecting"}), \
                mock.patch.object(w, "start_sync_background") as start:
            code, doc = api.dispatch(DEMO, "POST", "/v1/sync")
        self.assertEqual((code, doc["started"]), (200, False))
        start.assert_not_called()                          # already running: report it, don't start another
        with mock.patch.object(w, "drain_running", return_value=False), \
                mock.patch.object(w, "start_sync_background", return_value=(True, "Sync started (whoop-sync.service)")):
            self.assertEqual(api.dispatch(DEMO, "POST", "/v1/sync"),
                             (202, {"started": True, "message": "Sync started (whoop-sync.service)"}))
        with mock.patch.object(w, "drain_running", return_value=False), \
                mock.patch.object(w, "start_sync_background", return_value=(False, "Set WHOOP_ADDRESS")):
            self.assertEqual(api.dispatch(DEMO, "POST", "/v1/sync"), (500, {"error": "Set WHOOP_ADDRESS"}))

    def test_live_start_extend_stop(self):
        running = {"state": "live", "pid": 4242, "bpm": 70}
        with mock.patch.object(w, "live_status", return_value=running), mock.patch.object(w, "start_live") as start:
            code, doc = api.dispatch(DEMO, "POST", "/v1/live/start")
        self.assertEqual((code, doc["started"], doc["running"]), (200, False, running))
        start.assert_not_called()
        with mock.patch.object(w, "live_status", return_value=None), \
                mock.patch.object(w, "start_live", return_value="Live HR starting") as start:
            self.assertEqual(api.dispatch(DEMO, "POST", "/v1/live/start", body={"minutes": 15}),
                             (202, {"started": True, "message": "Live HR starting", "minutes": 15}))
        start.assert_called_once_with(15)
        self.assertEqual(api.dispatch(DEMO, "POST", "/v1/live/start", body={"minutes": 500})[0], 400)
        with mock.patch.object(w, "extend_live", return_value=False):
            self.assertEqual(api.dispatch(DEMO, "POST", "/v1/live/extend"), (409, {"error": "Live HR isn't on"}))
        with mock.patch.object(w, "extend_live", return_value=True):
            self.assertEqual(api.dispatch(DEMO, "POST", "/v1/live/extend")[0], 200)
        with mock.patch.object(w, "stop_live", return_value=False):
            self.assertEqual(api.dispatch(DEMO, "POST", "/v1/live/stop"), (200, {"stopped": False}))


class Port(StateDir):
    def test_flag_then_env_then_default(self):
        self.assertEqual(api.port(SimpleNamespace(port=None)), 8787)
        os.environ["WHOOP_API_PORT"] = "9123"
        self.assertEqual(api.port(SimpleNamespace(port=None)), 9123)
        self.assertEqual(api.port(SimpleNamespace(port=9001)), 9001)
        os.environ["WHOOP_API_PORT"] = "http"
        self.assertRaises(SystemExit, api.port, SimpleNamespace(port=None))


class Server(StateDir):
    """make_server on an ephemeral port, driven with raw HTTP."""

    def start(self):
        srv = api.make_server(DEMO, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def call(self, srv, method, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}{path}", method=method, data=body,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_binds_loopback_only(self):
        srv = self.start()
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "::", "192.168.1.10", ""):
            self.assertRaises(SystemExit, api.make_server, DEMO, host, 0)
        with self.assertRaises(SystemExit):
            api.serve(SimpleNamespace(**vars(DEMO), host="0.0.0.0", port=0))

    def test_get_json_compact_and_errors(self):
        srv = self.start()
        code, body = self.call(srv, "GET", "/healthz")
        self.assertEqual((code, body), (200, b'{"ok":true,"demo":true}'))
        code, body = self.call(srv, "GET", "/v1/report?hours=6&bins=60")
        doc = json.loads(body)
        self.assertEqual((code, doc["schema"]), (200, "whoop-poc/report@1"))
        self.assertEqual(body, json.dumps(doc, separators=(",", ":")).encode())   # compact separators
        self.assertEqual(self.call(srv, "GET", "/v1/nope")[0], 404)
        self.assertEqual(json.loads(self.call(srv, "GET", "/v1/report?hours=-1")[1])["error"],
                         "hours must be between 0.1 and 744")

    def test_host_header_and_post_content_type(self):
        srv = self.start()
        self.assertEqual(self.call(srv, "GET", "/healthz", headers={"Host": "evil.example:8787"})[0], 403)
        self.assertEqual(self.call(srv, "GET", "/healthz", headers={"Host": f"localhost:{srv.server_port}"})[0], 200)
        self.assertEqual(self.call(srv, "POST", "/v1/live/stop", b"{}", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.call(srv, "POST", "/v1/live/stop", b"[1]", {"Content-Type": "application/json"})[0], 400)
        with mock.patch.object(w, "stop_live", return_value=False):
            code, body = self.call(srv, "POST", "/v1/live/stop", b"", {"Content-Type": "application/json"})
        self.assertEqual((code, json.loads(body)), (200, {"stopped": False}))

    def test_log_skips_polls_and_never_has_queries(self):
        lines = []
        srv = api.make_server(DEMO, "127.0.0.1", 0, log=lines.append)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.call(srv, "GET", "/v1/status")
        self.call(srv, "GET", "/v1/live?since=1")
        self.call(srv, "GET", "/v1/report?hours=2&bins=40")
        self.call(srv, "GET", "/v1/nope?x=secret")
        with mock.patch.object(w, "stop_live", return_value=False):
            self.call(srv, "POST", "/v1/live/stop", b"{}", {"Content-Type": "application/json"})
        self.assertEqual([ln.rsplit(" ", 1)[0] for ln in lines],
                         ["GET /v1/report 200", "GET /v1/nope 404", "POST /v1/live/stop 200"])

    def test_heavy_routes_use_the_pool(self):
        calls = []

        class Pool:
            def submit(self, fn, *a):
                calls.append(a[1:3])
                f = __import__("concurrent.futures").futures.Future()
                f.set_result(fn(*a))
                return f

        srv = api.make_server(DEMO, "127.0.0.1", 0, pool=Pool())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertEqual(self.call(srv, "GET", "/v1/report?hours=2")[0], 200)
        self.assertEqual(self.call(srv, "GET", "/v1/status")[0], 200)
        self.assertEqual(calls, [("GET", "/v1/report")])      # status stayed in the request thread


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LanMode(StateDir):
    """make_server in LAN mode with a token, bound to 127.0.0.2 so the tests never listen on a real
    network. A LAN Host header makes a request non-local the same way a LAN peer does (is_local needs
    both a loopback peer and a loopback Host)."""

    def start(self, tok=TOKEN):
        srv = api.make_server(DEMO, "127.0.0.2", 0, tok=tok)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def call(self, srv, method, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.2:{srv.server_port}{path}", method=method, data=body,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def lan(self, srv, auth=None):
        h = {"Host": f"192.168.1.20:{srv.server_port}"}
        if auth:
            h["Authorization"] = auth
        return h

    def test_needs_a_long_enough_token(self):
        self.assertRaises(SystemExit, api.make_server, DEMO, "0.0.0.0", 0)
        self.assertRaises(SystemExit, api.make_server, DEMO, "0.0.0.0", 0, tok="short")
        os.environ["WHOOP_API_TOKEN"] = "short"
        self.assertRaises(SystemExit, api.serve, SimpleNamespace(**vars(DEMO), host="0.0.0.0", port=0))
        self.assertEqual(self.start().server_address[0], "127.0.0.2")

    def test_lan_requests_need_the_bearer_token(self):
        srv = self.start()
        code, _ = self.call(srv, "GET", "/healthz", headers=self.lan(srv))
        self.assertEqual(code, 401)
        req = urllib.request.Request(f"http://127.0.0.2:{srv.server_port}/healthz", headers=self.lan(srv))
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.headers["WWW-Authenticate"], 'Bearer realm="whoop-poc"')
        for bad in ("Bearer " + "u" * 43, TOKEN, "Basic " + TOKEN, "Bearer"):
            self.assertEqual(self.call(srv, "GET", "/healthz", headers=self.lan(srv, bad))[0], 401, bad)
        code, body = self.call(srv, "GET", "/healthz", headers=self.lan(srv, "Bearer " + TOKEN))
        self.assertEqual((code, json.loads(body)), (200, {"ok": True, "demo": True}))
        self.assertEqual(self.call(srv, "GET", "/v1/workouts", headers=self.lan(srv, "bearer " + TOKEN))[0], 200)

    def test_lan_can_only_get(self):
        srv = self.start()
        h = {**self.lan(srv, "Bearer " + TOKEN), "Content-Type": "application/json"}
        with mock.patch.object(w, "stop_live", return_value=False) as stop:
            self.assertEqual(self.call(srv, "POST", "/v1/live/stop", b"{}", h)[0], 403)
        stop.assert_not_called()

    def test_local_requests_need_no_token(self):
        srv = self.start()
        local = {"Host": f"127.0.0.1:{srv.server_port}"}
        self.assertEqual(self.call(srv, "GET", "/healthz", headers=local)[0], 200)
        with mock.patch.object(w, "stop_live", return_value=False):
            h = {**local, "Content-Type": "application/json"}
            self.assertEqual(self.call(srv, "POST", "/v1/live/stop", b"{}", h)[0], 200)

    def test_is_local_needs_a_loopback_peer_and_host(self):
        self.assertTrue(api.is_local("127.0.0.1", "localhost"))
        self.assertTrue(api.is_local("::1", "127.0.0.1"))
        self.assertFalse(api.is_local("192.168.1.20", "localhost"))     # a LAN peer naming localhost
        self.assertFalse(api.is_local("127.0.0.1", "evil.example"))      # DNS rebinding
        self.assertFalse(api.is_local("not-an-ip", "localhost"))

    def test_loopback_bind_ignores_tokens(self):
        srv = api.make_server(DEMO, "127.0.0.1", 0, tok=TOKEN)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        h = {"Host": "evil.example", "Authorization": "Bearer " + TOKEN}
        self.assertEqual(Server.call(self, srv, "GET", "/healthz", headers=h)[0], 403)


class Client(StateDir):
    def test_server_up_and_down_give_the_same_document(self):
        port = free_port()
        down = client.Client(DEMO, port)
        local_doc = down.get("/v1/report", hours=6, bins=60)
        self.assertEqual(down.mode, "local")
        srv = api.make_server(DEMO, "127.0.0.1", port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        up = client.Client(DEMO, port)
        api_doc = up.get("/v1/report", hours=6, bins=60)
        self.assertEqual(up.mode, "api")
        for doc in (local_doc, api_doc):                      # its newest_unix etc. come from the demo clock
            doc["max_hr"]["observed"] = None
        self.assertEqual(api_doc, local_doc)
        self.assertEqual(up.get("/v1/status")["schema"], down.get("/v1/status")["schema"])

    def test_fallback_is_sticky_then_retries(self):
        port = free_port()
        c = client.Client(DEMO, port)
        with mock.patch.object(client.Client, "_http", side_effect=ConnectionRefusedError) as h:
            c.get("/v1/health")
            c.get("/v1/health")
            self.assertEqual((c.mode, h.call_count), ("local", 1))      # the second call didn't retry
            c._down_until = 0                                     # RETRY_S has passed
            c.get("/v1/health")
            self.assertEqual(h.call_count, 2)

    def test_error_from_a_running_server_is_not_a_fallback(self):
        c = client.Client(DEMO, free_port())
        with mock.patch.object(client.Client, "_http", return_value=(409, {"error": "Live HR isn't on"})), \
                mock.patch.object(api, "dispatch") as local:
            with self.assertRaises(api.ApiError) as e:
                c.post("/v1/live/extend")
        self.assertEqual((e.exception.code, e.exception.message, c.mode), (409, "Live HR isn't on", "api"))
        local.assert_not_called()

    def test_local_errors_raise_the_same_way(self):
        c = client.Client(DEMO, free_port(), use_api=False)
        with self.assertRaises(api.ApiError) as e:
            c.get("/v1/report", hours="x")
        self.assertEqual((e.exception.code, c.mode), (400, "local"))

    def test_local_heavy_calls_use_the_pool_and_survive_a_broken_one(self):
        class Broken:
            def submit(self, *a):
                raise RuntimeError("pool is dead")

        c = client.Client(DEMO, free_port(), pool=Broken(), use_api=False)
        self.assertEqual(c.get("/v1/report", hours=2)["schema"], "whoop-poc/report@1")
        self.assertIsNone(c.pool)

    def test_tui_uses_the_server_only_for_the_shared_config(self):
        import tui
        base = dict(db=w.DEFAULT_DB, address=None, demo=False, port=None, max_hr=None, days=7)
        self.assertTrue(tui.make_client(SimpleNamespace(**base)).use_api)
        for k, v in (("demo", True), ("address", "AA:BB"), ("max_hr", 200), ("db", Path("/tmp/other.db"))):
            self.assertFalse(tui.make_client(SimpleNamespace(**{**base, k: v})).use_api, k)


if __name__ == "__main__":
    unittest.main()
