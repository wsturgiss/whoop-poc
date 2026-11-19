"""api.py (the local JSON API) and client.py (its in-process fallback). Demo data and fakes only."""
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import api
import client
import whoop_poc as w

DEMO = SimpleNamespace(db=Path("/nonexistent"), address=None, demo=True, heuristic=True, days=7)


class StateDir(unittest.TestCase):
    """Every test runs against an empty state dir (no observed max, no live session files)."""

    def setUp(self):
        self.addCleanup(w.refresh_max_hr)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.object(w, "STATE_DIR", Path(tmp.name)), mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        for k in ("WHOOP_MAX_HR", "WHOOP_API_PORT"):
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


class SharedConnection(unittest.TestCase):
    """serve's read-only connection is opened once and still sees rows written after it opened."""

    def test_reuses_connection_and_sees_new_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "whoop.db"
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE devices (id INTEGER PRIMARY KEY, address TEXT, name TEXT)")
        writer.execute("CREATE TABLE frames (device_id INTEGER, inner_type INTEGER, unix INTEGER)")
        writer.execute("INSERT INTO devices VALUES (1, 'AA', 'strap')")
        writer.execute("INSERT INTO frames VALUES (1, ?, 1000)", (w.TYPE_HISTORICAL,))
        writer.commit()

        p = mock.patch.object(w, "_SHARED_RO", None)
        p.start()
        self.addCleanup(p.stop)
        w.share_ro_connections()
        self.addCleanup(lambda: [c.close() for c in w._SHARED_RO.values()])
        real, opened = sqlite3.connect, []
        with mock.patch("sqlite3.connect", lambda *a, **k: opened.append(a) or real(*a, **k)):
            self.assertEqual(w.newest_unix(db), 1000)
            writer.execute("INSERT INTO frames VALUES (1, ?, 2000)", (w.TYPE_HISTORICAL,))
            writer.commit()
            self.assertEqual(w.newest_unix(db), 2000)
            t = threading.Thread(target=lambda: opened.append(("thread", w.newest_unix(db))))
            t.start()
            t.join()
        self.assertEqual(len(opened), 2)                # one connect, plus the thread's result
        self.assertEqual(opened[1], ("thread", 2000))
