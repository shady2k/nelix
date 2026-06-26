import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from daemon.events import EXTERNAL_OUTPUT_POLICY
from daemon.hygiene import PtyInputRejected

_MAX_BODY = 4 * 1024 * 1024   # 4 MiB body cap (post-auth memory hygiene; generous for tasks)


class _BadRequest(Exception):
    """A malformed request that should yield a 4xx, not an unhandled 500 + traceback."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def make_server(manager, token, host="127.0.0.1", port=8765, logger=None):
    class Handler(BaseHTTPRequestHandler):
        def _auth(self):
            if self.headers.get("X-Nelix-Token") != token:
                if logger is not None:
                    logger.warning("rpc", "unauthorized", path=self.path, status=401)
                self._send(401, {"error": "unauthorized"}); return False
            return True

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()  # UTF-8 out, not \uXXXX
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                raise _BadRequest(400, "invalid Content-Length")
            if n < 0:
                raise _BadRequest(400, "invalid Content-Length")
            if n > _MAX_BODY:
                raise _BadRequest(413, "request body too large")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:                          # JSONDecodeError subclasses ValueError
                raise _BadRequest(400, "malformed JSON body")

        def _int(self, val, default):
            if val is None:
                return default
            try:
                return int(val)
            except (TypeError, ValueError):
                raise _BadRequest(400, f"invalid integer parameter: {val!r}")

        def do_GET(self):
            if not self._auth():
                return
            try:
                self._dispatch_get(urlparse(self.path))
            except _BadRequest as e:
                if logger is not None:
                    logger.warning("rpc", "bad_request", path=self.path, status=e.code)
                self._send(e.code, {"error": e.msg})
            except Exception:
                if logger is not None:
                    logger.error("rpc", "request_exception", path=self.path, exc_info=True)
                self._send(500, {"error": "internal"})

        def _dispatch_get(self, p):
            if p.path == "/wait":
                qs = parse_qs(p.query)
                after = self._int(qs.get("after_seq", ["0"])[0], 0)
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
                turn = self._int(qs.get("turn", [None])[0], sess.dialog.turn_count() - 1)
                offset = self._int(qs.get("offset", ["0"])[0], 0)
                limit = self._int(qs.get("limit", [None])[0], None)
                page = sess.dialog.turn_text(turn, offset, limit)
                # transcript text is captured executor output -> carry the trust fence with it.
                page["external_output_policy"] = EXTERNAL_OUTPUT_POLICY
                self._send(200, page)
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
            try:
                self._dispatch_post(urlparse(self.path))
            except _BadRequest as e:
                if logger is not None:
                    logger.warning("rpc", "bad_request", path=self.path, status=e.code)
                self._send(e.code, {"error": e.msg})
            except Exception:
                if logger is not None:
                    logger.error("rpc", "request_exception", path=self.path, exc_info=True)
                self._send(500, {"error": "internal"})

        def _dispatch_post(self, p):
            body = self._read_json()
            if p.path == "/start":
                try:
                    sid, base_seq = manager.start(body["executor"], body["task"], body["cwd"])
                except PtyInputRejected as e:        # subclass of ValueError: catch BEFORE it
                    self._send(400, {"error": str(e)}); return
                except (RuntimeError, ValueError) as e:
                    self._send(409, {"error": str(e)}); return
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                self._send(200, {"session_id": sid, "next_after_seq": base_seq})
            elif p.path == "/respond":
                # respond binds to the session's CURRENT pending decision; event_id is gone and
                # decision_id is an OPTIONAL guard (sourced from the status pull, not the wake).
                try:
                    outcome = manager.respond(body["session_id"], body["answer"],
                                              decision_id=body.get("decision_id"))
                except PtyInputRejected as e:
                    self._send(400, {"error": str(e)}); return
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                sid = body.get("session_id")
                provided = body.get("decision_id")
                if outcome.status == "resumed":
                    self._send(200, {"status": "resumed", "next_after_seq": outcome.seq,
                                     "decision_id": outcome.decision_id})
                elif outcome.status == "unknown_session":
                    if logger is not None:
                        logger.warning("rpc", "respond_unknown_session", session_id=sid, status=404)
                    self._send(404, {"error": "unknown session"})
                elif outcome.status == "write_timeout":
                    # executor stopped draining stdin: the answer did not land and was not re-typed.
                    if logger is not None:
                        logger.warning("rpc", "respond_write_timeout", session_id=sid, status=503)
                    self._send(503, {"error": "write_timeout",
                                     "detail": "executor did not accept input (stdin wedged); stop and restart"})
                elif outcome.status == "stale":
                    # rich, self-contained diagnosis: who, what was sent, what is actually pending.
                    if logger is not None:
                        pend = outcome.pending or {}
                        logger.warning("rpc", "respond_stale", session_id=sid, status=409,
                                       provided_decision_id=provided,
                                       pending_decision_id=pend.get("decision_id"),
                                       pending_kind=pend.get("kind"))
                    self._send(409, {"error": "stale_decision", "pending": outcome.pending})
                else:   # no_pending
                    if logger is not None:
                        logger.warning("rpc", "respond_no_pending", session_id=sid, status=409,
                                       provided_decision_id=provided)
                    self._send(409, {"error": "no_pending_decision"})
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
            "requires_response": e.requires_response, "screen_excerpt": e.screen_excerpt,
            "external_output_policy": EXTERNAL_OUTPUT_POLICY}
