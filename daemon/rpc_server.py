import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def make_server(manager, token, host="127.0.0.1", port=8765):
    class Handler(BaseHTTPRequestHandler):
        def _auth(self):
            if self.headers.get("X-Nelix-Token") != token:
                self._send(401, {"error": "unauthorized"}); return False
            return True

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._auth():
                return
            p = urlparse(self.path)
            if p.path == "/wait":
                qs = parse_qs(p.query)
                after = int(qs.get("after_seq", ["0"])[0])
                sid = qs.get("session_id", [None])[0]
                evt = manager._events.wait_event(after_seq=after, timeout=25, session_id=sid)
                self._send(200, {"event": _evt_dict(evt) if evt else None})
            elif p.path == "/status":
                sid = parse_qs(p.query).get("session_id", [None])[0]
                self._send(200, manager.status(sid))
            elif p.path == "/dialog":
                qs = parse_qs(p.query)
                sess = manager.get(qs.get("session_id", [None])[0])
                if sess is None or sess.dialog is None:
                    self._send(404, {"error": "unknown session"}); return
                turn = qs.get("turn", [None])[0]
                turn = int(turn) if turn is not None else sess.dialog.turn_count() - 1
                offset = int(qs.get("offset", ["0"])[0])
                limit = qs.get("limit", [None])[0]
                self._send(200, sess.dialog.turn_text(
                    turn, offset, int(limit) if limit is not None else None))
            elif p.path == "/screen":
                qs = parse_qs(p.query)
                sid = qs.get("session_id", [None])[0]
                raw = qs.get("raw", ["0"])[0].lower() in ("1", "true")
                force = qs.get("force", ["0"])[0].lower() in ("1", "true")
                self._send(200, manager.screen(sid, raw=raw, force=force))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            p = urlparse(self.path); body = self._read_json()
            if p.path == "/start":
                try:
                    sid, base_seq = manager.start(body["executor"], body["task"], body["cwd"])
                except (RuntimeError, ValueError) as e:
                    self._send(409, {"error": str(e)}); return
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                self._send(200, {"session_id": sid, "next_after_seq": base_seq})
            elif p.path == "/respond":
                try:
                    seq = manager.respond(body["session_id"], body["event_id"], body["answer"])
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                if seq is None:
                    self._send(409, {"error": "stale or unknown event_id"})
                else:
                    self._send(200, {"status": "resumed", "next_after_seq": seq})
            elif p.path == "/stop":
                try:
                    stopped = manager.stop(body["session_id"])
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                self._send(200, {"stopped": stopped})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def _evt_dict(e):
    return {"seq": e.seq, "event_id": e.event_id, "session_id": e.session_id,
            "executor": e.executor, "kind": e.kind, "summary": e.summary, "state": e.state,
            "hint": e.hint, "hung": e.hung, "task_delivery": e.task_delivery,
            "requires_response": e.requires_response, "screen_excerpt": e.screen_excerpt}
