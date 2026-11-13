"""webhook.py: the Coach webhook after a sync. Synthetic workouts and a fake sender; nothing leaves the host."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import coach
import webhook
import whoop_poc as w
from test_workout_detail import T0, hr_trace, window

ENV = {"WHOOP_COACH_WEBHOOK_URL": "https://coach.invalid/hook", "WHOOP_COACH_WEBHOOK_AUTH": "Bearer t0ken"}


class Sender:
    """Records each POST; replies with the next status in `statuses` (the last one repeats)."""

    def __init__(self, *statuses):
        self.statuses, self.calls = list(statuses), []

    def __call__(self, url, auth, doc):
        self.calls.append((url, auth, doc))
        return (self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]), b'{"ok":true}'


def two_runs():
    """A run at 08:24 and a second at 09:39, each followed by 15 min of easy HR."""
    return hr_trace((1200, 160), (900, 80), (2400, 80), (1200, 160), (900, 80), t0=T0)


class Webhook(unittest.TestCase):
    def setUp(self):
        self.addCleanup(w.refresh_max_hr)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / webhook.STATE_FILE
        for p in (mock.patch.object(w, "STATE_DIR", self.dir), mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        for k in (*ENV, "WHOOP_MAX_HR"):
            os.environ.pop(k, None)
        w.refresh_max_hr()
        self.logs = []

    def send_new(self, win, sender):
        return webhook.send_new(win, ENV["WHOOP_COACH_WEBHOOK_URL"], ENV["WHOOP_COACH_WEBHOOK_AUTH"], self.path,
                                sender, self.logs.append)

    def test_skips_quietly_when_unset(self):
        sender = Sender(200)
        for env in ({}, {"WHOOP_COACH_WEBHOOK_URL": ENV["WHOOP_COACH_WEBHOOK_URL"]}):
            with mock.patch.dict(os.environ, env):
                a = SimpleNamespace(test=False, demo=False)
                self.assertEqual(webhook.run(a, sender, self.logs.append), 0)
        self.assertEqual((sender.calls, self.logs, self.path.exists()), ([], [], False))

    def test_first_run_seeds_without_posting(self):
        full = window(two_runs())
        first = window({t: v for t, v in two_runs().items() if t < T0 + 2100})   # only the first run so far
        sender = Sender(200)
        self.assertEqual(self.send_new(first, sender), 0)
        self.assertEqual(sender.calls, [])
        self.assertEqual(json.loads(self.path.read_text())["seeded"], ["2026-11-11-0824"])
        self.assertEqual(self.send_new(full, sender), 0)                     # only the new one goes out
        (_, _, doc), = sender.calls
        self.assertEqual(doc["id"], "2026-11-11-0939")
        self.assertEqual(set(doc), set(webhook.PAYLOAD_KEYS))
        self.assertEqual((doc["minutes"], doc["needs_confirm"], doc["avg_hr"]), (20, True, 160))
        self.assertEqual(self.send_new(full, sender), 0)                     # idempotent
        self.assertEqual(len(sender.calls), 1)

    def test_marks_sent_only_on_2xx_and_retries(self):
        self.send_new(window(hr_trace((600, 80), t0=T0 - 600)), Sender(200))       # seed: nothing yet
        win = window(two_runs())
        sender = Sender(500, 503, 200, 201)
        self.assertEqual(self.send_new(win, sender), 1)                      # both fail
        self.assertEqual(json.loads(self.path.read_text())["sent"], {})
        self.assertEqual(self.send_new(win, sender), 0)                      # retried: 200, 201
        self.assertEqual([c[2]["id"] for c in sender.calls],
                         ["2026-11-11-0824", "2026-11-11-0939"] * 2)
        self.assertEqual(set(json.loads(self.path.read_text())["sent"]), {"2026-11-11-0824", "2026-11-11-0939"})
        self.assertIn("webhook: 2026-11-11-0824 -> 500", self.logs)

    def test_skips_confirmed_and_unfinished(self):
        self.send_new(window(hr_trace((600, 80), t0=T0 - 600)), Sender(200))
        hr = two_runs()
        ongoing = window({t: v for t, v in hr.items() if t < T0 + 1260})     # 1 min after the first run's end
        sender = Sender(200)
        self.send_new(ongoing, sender)
        self.assertEqual(sender.calls, [])                                   # could still grow
        confirmed = window(hr, confirmations=[{"start_unix": T0, "minutes": 18, "set_unix": 0}])
        self.send_new(confirmed, sender)
        self.assertEqual([c[2]["id"] for c in sender.calls], ["2026-11-11-0939"])

    def test_test_fire_payload_and_state_untouched(self):
        sender = Sender(200)
        with mock.patch.dict(os.environ, ENV):
            code = webhook.run(SimpleNamespace(test=True, demo=False), sender, self.logs.append)
        self.assertEqual(code, 0)
        (url, auth, doc), = sender.calls
        self.assertEqual((url, auth), (ENV["WHOOP_COACH_WEBHOOK_URL"], ENV["WHOOP_COACH_WEBHOOK_AUTH"]))
        self.assertIs(doc["test"], True)
        self.assertTrue(doc["id"].startswith("test-"))
        self.assertTrue(set(webhook.PAYLOAD_KEYS) <= set(doc))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.logs, ["webhook: test -> 200, body looks like success: yes"])

    def test_output_never_carries_url_or_auth(self):
        sender = Sender(401)
        with mock.patch.dict(os.environ, ENV):
            webhook.run(SimpleNamespace(test=True, demo=False), sender, self.logs.append)
        self.send_new(window(hr_trace((600, 80), t0=T0 - 600)), sender)
        self.send_new(window(two_runs()), sender)
        text = "\n".join(self.logs)
        for v in ENV.values():
            self.assertNotIn(v, text)
        self.assertNotIn("coach.invalid", text)

    def test_plain_http_refused_except_loopback(self):
        self.assertIsNone(webhook.url_problem("https://example.invalid/x"))
        self.assertIsNone(webhook.url_problem("http://127.0.0.1:8080/x"))
        self.assertIsNotNone(webhook.url_problem("http://example.invalid/x"))
        sender = Sender(200)
        with mock.patch.dict(os.environ, {**ENV, "WHOOP_COACH_WEBHOOK_URL": "http://example.invalid/x"}):
            self.assertEqual(webhook.run(SimpleNamespace(test=True, demo=False), sender, self.logs.append), 1)
        self.assertEqual(sender.calls, [])

    def test_body_check(self):
        for body, want in ((b"", True), (b'{"ok":true}', True), (b'{"ok":false}', False),
                           (b'{"error":"bad"}', False), (b"accepted", True), (b"Error: nope", False)):
            self.assertEqual(webhook.body_looks_ok(body), want, body)



class Post(unittest.TestCase):
    """webhook.post against a loopback server: status codes, headers, and redirects not followed."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.seen = []
        seen = self.seen

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen.append((self.path, self.headers["Authorization"], json.loads(body)))
                code = {"/ok": 200, "/fail": 500, "/moved": 302}[self.path]
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "/ok")
                self.end_headers()
                self.wfile.write(b'{"ok":true}' if code == 200 else b"")

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def test_status_codes_and_no_redirect(self):
        self.assertEqual(webhook.post(self.base + "/ok", "Bearer x", {"id": "a"}), (200, b'{"ok":true}'))
        self.assertEqual(webhook.post(self.base + "/fail", "Bearer x", {"id": "b"})[0], 500)
        self.assertEqual(webhook.post(self.base + "/moved", "Bearer x", {"id": "c"})[0], 302)
        self.assertEqual([p for p, _, _ in self.seen], ["/ok", "/fail", "/moved"])   # /moved not followed
        self.assertEqual(self.seen[0][1:], ("Bearer x", {"id": "a"}))

    def test_no_server_is_no_response(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.assertEqual(webhook.post(self.base + "/ok", "Bearer x", {}), (None, b""))


if __name__ == "__main__":
    unittest.main()
