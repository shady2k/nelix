import hmac
import json
import os
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import paths
from daemon import owner
from daemon.config import MSG_MAX_BODY, DEFAULT_DIALOG_PAGE_CHARS
from daemon.dialog import DialogReader
from daemon.env_resolver import EnvResolveError
from daemon.errors import error_envelope
from daemon.events import EXTERNAL_OUTPUT_POLICY
from daemon.generation import generation_id
from daemon.hooks import HookEvent
from daemon.hygiene import PtyInputRejected
from daemon.manager import (
    ModelRejected, ModelUnavailable, SessionIdInUse, SessionIdRejected, validate_session_id_shape,
)
from daemon.messages import parse_message_body
from daemon.protocol import RPC_PROTOCOL_VERSION
from daemon.transport import peer_is_self

_MAX_BODY = 4 * 1024 * 1024   # 4 MiB body cap (post-auth memory hygiene; generous for tasks)
_HOOK_MAX_BODY = 256 * 1024   # tight cap for hook payloads: they are small lifecycle events
_HOOK_RATE_CAPACITY = 60      # per-session token-bucket burst (generous for a busy turn's tool events)
_HOOK_RATE_REFILL = 30.0      # tokens/sec sustained; a genuine flood/forge attempt is dropped


def _query_sid(query, key="session_id"):
    """Extract `key` from a raw query string, treating a `key=` with an EMPTY value as PRESENT
    ("") rather than absent (None). `parse_qs`'s default (`keep_blank_values=False`) drops a blank
    value entirely, conflating "the caller omitted this" with "the caller sent an explicit empty
    string" -- which matters wherever an omitted sid is legitimate (the global /status and
    /capabilities forms) but an explicit empty one is not (nelix-9a4.6 review finding #4: an empty
    `sid` on /capabilities used to silently fall through to the global baseline instead of 400ing).
    """
    return parse_qs(query, keep_blank_values=True).get(key, [None])[0]


class _HookRateLimiter:
    """Minimal per-session token bucket for the /hook route (spec §7: rate-limit alongside the body
    cap). A same-uid process (or a flapping agent — the bg-subagent flap fired ~35 in a window) could
    otherwise POST an unbounded flood of lifecycle events; a sane per-session rate drops the excess.
    Buckets are created only AFTER secret auth, so a wrong-secret caller never grows the map. Hooks
    are best-effort (`curl … || true`), so a dropped POST just doesn't advance the belief engine."""

    def __init__(self, capacity=_HOOK_RATE_CAPACITY, refill=_HOOK_RATE_REFILL, clock=time.monotonic):
        self._capacity = capacity
        self._refill = refill
        self._clock = clock
        self._buckets = {}                 # sid -> [tokens, last_ts]
        self._lock = threading.Lock()

    def allow(self, sid):
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(sid, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
            if tokens < 1.0:
                self._buckets[sid] = (tokens, now)
                return False
            self._buckets[sid] = (tokens - 1.0, now)
            return True


class _BadRequest(Exception):
    """A malformed request that should yield a 4xx, not an unhandled 500 + traceback."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def make_server(manager, transport, logger=None, *, clock=time.monotonic):
    is_unix = transport.kind == "unix"
    token = transport.token
    # `clock` is the monotonic time source both flood-guard buckets refill against. It defaults to
    # the real clock in production; tests inject a frozen/controlled clock so bucket exhaustion is
    # decided purely by request COUNT, never by wall-clock timing that a busy machine could flake
    # (see test_message_route.py::test_message_bucket_exhausted_returns_429, nelix-3s3).
    hook_limiter = _HookRateLimiter(clock=clock)   # per-session flood guard for /hook (shared across threads)
    # A SEPARATE instance (same class/config) for /message: distinct bucket per sid so an executor
    # flooding questions/notes can never starve /hook delivery (spec — see
    # test_message_limiter_separate_from_hooks).
    msg_limiter = _HookRateLimiter(clock=clock)

    class Handler(BaseHTTPRequestHandler):
        def _auth(self):
            # unix: no token — the 0600 node is the boundary; peercred rejects a known foreign uid.
            # tcp: shared-secret token (the credential that crosses the container line).
            if is_unix:
                if peer_is_self(self.connection):
                    return True
                if logger is not None:
                    logger.warning("rpc", "unauthorized_peer", path=self.path, status=401)
                self._send(401, {"error": "unauthorized"}); return False
            if self.headers.get("X-Nelix-Token") != token:
                if logger is not None:
                    logger.warning("rpc", "unauthorized", path=self.path, status=401)
                self._send(401, {"error": "unauthorized"}); return False
            return True

        def _send(self, code, obj):
            if obj is None:                       # explicit empty response (e.g. 204 No Content)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers(); return
            body = json.dumps(obj, ensure_ascii=False).encode()  # UTF-8 out, not \uXXXX
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _read_json(self, max_body=_MAX_BODY):
            try:
                n = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                raise _BadRequest(400, "invalid Content-Length")
            if n < 0:
                raise _BadRequest(400, "invalid Content-Length")
            if n > max_body:
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

        def _owner(self, val):
            """The caller's owner_id, REQUIRED and shape-checked, on every caller-facing route.

            Required with no default, and no route may skip it: a missing owner cannot be
            resolved to "any owner" without recreating the exact bug this closes (one harness
            reading the whole board). Absent => 400 with a message that says what to send, not a
            silent empty result, because a caller that forgot the field should learn it forgot —
            an empty board reads like an idle daemon.

            NOT applied to /hook and /message: those authenticate with the per-session secret,
            which is strictly stronger than an owner id (daemon/owner.py).
            """
            if val is None:
                raise _BadRequest(400, "missing owner_id")
            try:
                return owner.validate(val)
            except owner.OwnerRejected as e:
                raise _BadRequest(400, str(e))

        def _require_valid_sid(self, sid):
            """Shape-check a caller-supplied session id BEFORE it is used as a `sessions/<sid>/`
            path component ANYWHERE downstream -- the owner lookup, the transcript read, the meta
            read (nelix-9a4.6 review finding #3: /start alone had this check; every other route
            taking a caller-supplied sid read straight past it). Sends the stable 400 envelope and
            returns False on a bad shape; returns True and sends nothing on a good one.

            Callers with an OPTIONAL sid (the global /status and /capabilities forms) must check
            `is None` themselves first -- an omitted sid is not this method's business, only a
            PRESENT bad-shape one is.
            """
            try:
                validate_session_id_shape(sid)
                return True
            except SessionIdRejected as e:
                self._send(400, error_envelope("invalid_session_id", str(e), retryable=False))
                return False

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

        def _log_read(self, tool, sid):
            # nelix-jwv gap 4: a light per-read record (which tool, session, current event seq) at
            # debug — high-volume, so off the info plane, but it makes "nelix_screen called twice in a
            # turn" visible from the log instead of only by replaying the raw capture.
            if logger is not None:
                logger.debug("rpc", "read", session_id=sid, tool=tool,
                             seq=manager._events.latest_seq(sid))

        def _dispatch_get(self, p):
            if p.path == "/health":
                # spec §8/§10: a liveness/identity probe. Deliberately NO owner_id and NO session
                # id — every other caller-facing route needs one or both; this one must not, or a
                # health check would need to already know a caller identity to ask "are you up".
                self._send(200, {"status": "ok", "rpc_protocol": RPC_PROTOCOL_VERSION,
                                 "generation_id": generation_id()})
            elif p.path == "/wait":
                qs = parse_qs(p.query)
                after = self._int(qs.get("after_seq", ["0"])[0], 0)
                sid = qs.get("session_id", [None])[0]
                owner_id = self._owner(qs.get("owner_id", [None])[0])
                if not sid:
                    # A global (session_id-less) wait returns on ANY session's event, leaking one
                    # session's result into another's orchestrator when the daemon is shared. Refuse
                    # it structurally: session_id is mandatory (mirrors the /dialog guard below).
                    self._send(400, {"error": "missing session_id"}); return
                # Events are NOT owner-tagged (the queue is a global ordered log), so the filter has
                # to be here: establish ownership of `sid` BEFORE arming, and never arm otherwise.
                # This is the "arms a waiter for every session it sees" half of the bug — a waiter
                # is how one harness ends up answering another's decision.
                #
                # 404, NOT a 200 with event:null. Both keep the event in, but a waiter's whole
                # contract is "this call blocks ~25s, so a null means re-issue" — answering an
                # un-armable wait instantly and nullly tells every correct waiter to try again at
                # once, forever. MEASURED before this was a 404: bin/nelix-wait spun at ~3400
                # req/s against the daemon. An unownable session is not "no events yet", it is a
                # wait that can NEVER return, and the caller has to be able to tell the difference.
                if not owner.owns_session(sid, owner_id):
                    self._send(404, {"error": "unknown session",
                                     "hint": "unknown session, or not this owner's; a wait on it"
                                             " would never wake. Do not retry."})
                    return
                evt = manager._events.wait_event(after_seq=after, timeout=25, session_id=sid)
                self._send(200, {"event": _evt_dict(evt) if evt else None})
            elif p.path == "/status":
                qs = parse_qs(p.query)
                sid = qs.get("session_id", [None])[0]
                owner_id = self._owner(qs.get("owner_id", [None])[0])
                if sid is not None and not self._require_valid_sid(sid):
                    return
                # Task 8: explicit on-demand progress detail, off by default (anti-poll: an
                # active-working snapshot stays progress-free unless the caller asks for it).
                include_progress = qs.get("include_progress", ["0"])[0].lower() in ("1", "true")
                self._log_read("status", sid)
                # Stamp the RPC protocol version at the wire layer (always present, regardless of
                # session_id) so a supervisor can tell our protocol from an old daemon's.
                self._send(200, {**manager.status(sid, owner_id=owner_id,
                                                  include_progress=include_progress),
                                 "rpc_protocol": RPC_PROTOCOL_VERSION})
            elif p.path == "/dialog":
                qs = parse_qs(p.query)
                sid = qs.get("session_id", [None])[0]
                owner_id = self._owner(qs.get("owner_id", [None])[0])
                self._log_read("dialog", sid)
                if not sid:
                    self._send(400, {"error": "missing session_id"}); return
                # Shape-check BEFORE the owner-gate below or the disk read past it (nelix-9a4.6
                # review finding #3): a bad-shape sid (traversal, separators, control chars) must
                # never reach `paths.sessions_root() / sid`, regardless of what owner-gating would
                # have done with it.
                if not self._require_valid_sid(sid):
                    return
                # THE route the session id alone must never open: everything below reads the
                # transcript straight off DISK, so it never passes through the manager and would
                # otherwise hand any caller the full dialog of any session whose id it holds. The
                # gate goes here, ahead of the read — not inside DialogReader, which is also the
                # capture tool's reader and has no business knowing about owners.
                if not owner.owns_session(sid, owner_id):
                    self._send(404, {"error": "unknown session",
                                     "hint": "the session may have exited or not started;"
                                             " call nelix_status (no session_id) to list sessions."})
                    return
                reader = DialogReader(paths.sessions_root() / sid)
                if not reader.available:
                    # No transcript on disk — fall back to live session if present
                    sess = manager.get(sid)
                    if sess is None or sess.dialog is None:
                        self._send(404, {"error": "unknown session",
                                         "hint": "the session may have exited or not started;"
                                                 " call nelix_status (no session_id) to list sessions."})
                        return
                    reader = sess.dialog   # duck-typed: same page/tail interface
                offset = self._int(qs.get("offset", ["0"])[0], 0)
                limit = self._int(qs.get("limit", [None])[0], None)
                if offset < 0:
                    raise _BadRequest(400, "offset must be >= 0")
                if limit is not None and limit <= 0:
                    raise _BadRequest(400, "limit must be > 0")
                if limit is None:
                    limit = DEFAULT_DIALOG_PAGE_CHARS   # bounded page + continuation cursor
                page = reader.page(offset, limit)
                # at_end is derivable from the page's own fields (no data-layer change). A caller
                # stops on at_end without a wasted extra read past the end.
                page["at_end"] = page["next_offset"] >= page["total_len"]
                if page["at_end"]:
                    page["hint"] = (
                        f"This offset is at or beyond the current transcript end "
                        f"({page['total_len']} chars). If the session is still active, wait for a"
                        " nelix wake / nelix_status before reading from total_len again.")
                page["external_output_policy"] = EXTERNAL_OUTPUT_POLICY
                self._send(200, page)
            elif p.path == "/screen":
                qs = parse_qs(p.query)
                sid = qs.get("session_id", [None])[0]
                owner_id = self._owner(qs.get("owner_id", [None])[0])
                if sid is not None and not self._require_valid_sid(sid):
                    return
                self._log_read("screen", sid)
                raw = qs.get("raw", ["0"])[0].lower() in ("1", "true")
                force = qs.get("force", ["0"])[0].lower() in ("1", "true")
                # manager.screen owner-gates via _owned. Note `force` bypasses the anti-poll
                # withhold, NOT the owner gate: they are checked in that order inside screen().
                self._send(200, manager.screen(sid, owner_id=owner_id, raw=raw, force=force))
            elif p.path == "/capabilities":
                # spec §8: per-session capabilities. NOTE the query key is `sid` (not
                # `session_id` like every other route) — verbatim per the brief. owner_id is
                # required regardless of whether `sid` is present, exactly like /status (the same
                # `_owner` helper, the same missing-owner 400 shape; no new auth path).
                #
                # `_query_sid` (not a plain qs.get): a PRESENT but empty `sid=` must 400, not be
                # silently treated as "omitted -> global baseline" (nelix-9a4.6 review finding #4)
                # -- parse_qs()'s default drops a blank value, conflating the two.
                qs = parse_qs(p.query)
                sid = _query_sid(p.query, "sid")
                owner_id = self._owner(qs.get("owner_id", [None])[0])
                if sid is not None and not self._require_valid_sid(sid):
                    return
                result = manager.capabilities(sid, owner_id=owner_id)
                if sid is not None and result is None:
                    self._send(404, error_envelope(
                        "unknown_session",
                        "unknown session, or not this owner's", retryable=False))
                    return
                self._send(200, {**result, "rpc_protocol": RPC_PROTOCOL_VERSION})
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

        def _dispatch_hook(self, p):
            # POST /hook/<sid>: a hook-capable agent reports one lifecycle event. Authenticated by
            # the per-session secret (X-Nelix-Hook-Secret), IN ADDITION to the transport's
            # peercred/token in _auth. Tight body cap; hands a typed HookEvent to Session.on_hook.
            sid = p.path[len("/hook/"):]
            # Shape-check the path-embedded sid first (nelix-9a4.6 review finding #3): every
            # caller-supplied session id used as a path component gets the SAME check, /hook
            # included, even though this route's own lookup (manager.get, a dict read) is not
            # itself filesystem-reachable via sid today.
            if not self._require_valid_sid(sid):
                return
            body = self._read_json(max_body=_HOOK_MAX_BODY)      # 413 (too large) / 400 (malformed)
            sess = manager.get(sid)
            secret = getattr(sess, "hook_secret", None) if sess is not None else None
            provided = self.headers.get("X-Nelix-Hook-Secret", "")
            # Fail closed and identically for unknown session, missing secret, and bad secret — no
            # existence oracle. compare_digest keeps the check constant-time.
            if not secret or not hmac.compare_digest(provided, secret):
                if logger is not None:
                    logger.warning("rpc", "hook_unauthorized", session_id=sid, status=401)
                self._send(401, {"error": "unauthorized"}); return
            # Per-session flood guard (spec §7): drop hook POSTs past the sane per-session rate. After
            # auth, so only real sessions create buckets; best-effort hooks ignore the 429.
            if not hook_limiter.allow(sid):
                if logger is not None:
                    logger.warning("rpc", "hook_rate_limited", session_id=sid, status=429)
                self._send(429, {"error": "rate_limited"}); return
            if not isinstance(body, dict) or "hook_event_name" not in body:
                raise _BadRequest(400, "missing hook_event_name")
            ev = HookEvent(session_id=sid, event=body["hook_event_name"],
                           tool_name=body.get("tool_name"),
                           tool_input=body.get("tool_input") or {},
                           is_interrupt=bool(body.get("is_interrupt")),
                           notification=body.get("message") or body.get("matcher"))
            sess.on_hook(ev)
            self._send(204, None)

        def _dispatch_message(self, p):
            # POST /message/<sid>: the executor-facing async message channel — a `question` it
            # doesn't want to block on, or a non-waking `note`. Authenticated identically to /hook
            # (same per-session secret, X-Nelix-Hook-Secret) but rate-limited from a SEPARATE bucket
            # (msg_limiter) so message spam can never starve hook delivery. Never touches the PTY
            # (single-writer PTY invariant): only manager state methods are called here.
            sid = p.path[len("/message/"):]
            # Shape-check first, same reasoning as _dispatch_hook above (nelix-9a4.6 review finding #3).
            if not self._require_valid_sid(sid):
                return
            body = self._read_json(max_body=MSG_MAX_BODY)   # 413 (too large) / 400 (malformed)
            sess = manager.get(sid)
            secret = getattr(sess, "hook_secret", None) if sess is not None else None
            provided = self.headers.get("X-Nelix-Hook-Secret", "")
            # Fail closed and identically for unknown session, missing secret, and bad secret — no
            # existence oracle (mirrors _dispatch_hook exactly). compare_digest keeps it constant-time.
            if not secret or not hmac.compare_digest(provided, secret):
                if logger is not None:
                    logger.warning("rpc", "message_unauthorized", session_id=sid, status=401)
                self._send(401, {"error": "unauthorized"}); return
            # Per-session flood guard, SEPARATE bucket from /hook's — see msg_limiter above.
            if not msg_limiter.allow(sid):
                if logger is not None:
                    logger.warning("rpc", "message_rate_limited", session_id=sid, status=429)
                self._send(429, {"error": "rate_limited"}); return
            if not isinstance(body, dict):
                raise _BadRequest(400, "malformed JSON body")
            kind = body.get("kind")
            obj, err = parse_message_body(kind, body)
            if err is not None:
                status, msg = err
                self._send(status, {"error": msg}); return
            if kind == "question":
                qid, qerr = manager.record_async_question(sid, obj)
                if qerr is not None:
                    if "id" in qerr:      # already pending — not an error, a conflicting state
                        self._send(409, {"status": "already_pending",
                                         "pending": {"id": qerr["id"],
                                                      "question": qerr["question"]}}); return
                    # {"error": "unknown_session"} — the rare post-auth race (session freed between
                    # the auth lookup above and this call); auth already 401s a truly-unknown sid.
                    self._send(404, {"error": "unknown_session"}); return
                self._send(200, {"status": "queued", "id": qid}); return
            # kind == "note" (the only other value parse_message_body accepts)
            seq = manager.append_progress_note(sid, obj)
            if seq is None:
                self._send(404, {"error": "unknown_session"}); return
            self._send(200, {"status": "recorded", "progress_seq": seq})

        def _dispatch_post(self, p):
            if p.path.startswith("/hook/"):
                self._dispatch_hook(p); return
            if p.path.startswith("/message/"):
                self._dispatch_message(p); return
            body = self._read_json()
            if p.path == "/start":
                owner_id = self._owner(body.get("owner_id") if isinstance(body, dict) else None)
                try:
                    outcome = manager.start(body["executor"], body["task"], body["cwd"],
                                            owner_id=owner_id, model=body.get("model"),
                                            session_id=body.get("session_id"))
                except owner.OwnerWriteFailed as e:
                    # The owner record could not be persisted, so no session was started (spec §7:
                    # start FAILS if the owner cannot be written). 500, not 4xx: the request was
                    # well-formed and the daemon failed it. Caught BEFORE ValueError/RuntimeError
                    # below so it can never be mistaken for a 409 "daemon full", which would invite
                    # a retry loop against a disk that is not going to get emptier.
                    if logger is not None:
                        logger.error("rpc", "start_owner_unwritable", status=500)
                    self._send(500, {"error": str(e)}); return
                except ModelUnavailable as e:        # nelix-kwr: model not offered by the backend
                    self._send(400, {"error": str(e), "available_models": e.available_models}); return
                except SessionIdInUse as e:           # spec §3: never silently reuse/clobber
                    self._send(409, error_envelope("session_id_in_use", str(e), retryable=False)); return
                except SessionIdRejected as e:        # bad-shape router-supplied session_id
                    self._send(400, error_envelope("invalid_session_id", str(e), retryable=False)); return
                except (ModelRejected, owner.OwnerRejected) as e:   # ValueError subclasses: BEFORE the 409
                    self._send(400, {"error": str(e)}); return   # bad-shape/unsupported model = client input error
                except PtyInputRejected as e:        # subclass of ValueError: catch BEFORE it
                    self._send(400, {"error": str(e)}); return
                except EnvResolveError as e:         # nelix-c5o: upstream resolver/secret-backend failure
                    # 502 (not the client 400, not the capacity 409): the daemon is healthy, an env_cmd
                    # command failed. str(e) is REDACTED (var + reason only, no command/stdout/stderr);
                    # the orchestrator relays it and stops (does not blind-retry).
                    self._send(502, {"error": str(e)}); return
                except (RuntimeError, ValueError) as e:
                    self._send(409, {"error": str(e)}); return
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                self._send(200, {"operation": "start", "status": "started",
                                 "session_id": outcome.session_id, "snapshot": outcome.snapshot,
                                 "next_after_seq": outcome.base_seq, "next_action": "end_turn"})
            elif p.path == "/respond":
                owner_id = self._owner(body.get("owner_id") if isinstance(body, dict) else None)
                try:
                    outcome = manager.respond(body["session_id"], body["answer"],
                                              owner_id=owner_id,
                                              decision_id=body.get("decision_id"))
                except PtyInputRejected as e:
                    self._send(400, {"error": str(e)}); return
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                sid = body.get("session_id")
                provided = body.get("decision_id")
                if outcome.status == "resumed":
                    self._send(200, {"operation": "respond", "status": "resumed", "session_id": sid,
                                     "snapshot": outcome.snapshot, "next_after_seq": outcome.seq,
                                     "answered_decision_id": outcome.answered_decision_id,
                                     "decision_id": outcome.decision_id, "next_action": "end_turn"})
                elif outcome.status == "write_timeout":
                    if logger is not None:
                        logger.warning("rpc", "respond_write_timeout", session_id=sid, status=503)
                    self._send(503, {"operation": "respond", "status": "write_timeout", "session_id": sid,
                                     "snapshot": outcome.snapshot,
                                     "answered_decision_id": outcome.answered_decision_id,
                                     "next_action": "recover", "error": "write_unconfirmed"})
                elif outcome.status == "respond_failed":
                    # nelix-sud: the answer was typed but never LEFT the box (Enter never landed).
                    # Surface as 503/recover (like write_timeout) so the MCP layer arms no waiter and
                    # the orchestrator recovers, instead of a false 200/end_turn into infinite silence.
                    if logger is not None:
                        logger.warning("rpc", "respond_unconfirmed", session_id=sid, status=503)
                    self._send(503, {"operation": "respond", "status": "respond_failed", "session_id": sid,
                                     "snapshot": outcome.snapshot,
                                     "answered_decision_id": outcome.answered_decision_id,
                                     "next_action": "recover", "error": "submit_unconfirmed"})
                elif outcome.status == "missing_decision_id":
                    if logger is not None:
                        logger.warning("rpc", "respond_missing_decision_id", session_id=sid, status=409)
                    self._send(409, {"operation": "respond", "status": "missing_decision_id",
                                     "session_id": sid, "error": "missing_decision_id",
                                     "pending": outcome.pending, "next_action": "fix_call"})
                elif outcome.status == "stale":
                    if logger is not None:
                        logger.warning("rpc", "respond_stale", session_id=sid, status=409)
                    self._send(409, {"operation": "respond", "status": "stale", "session_id": sid,
                                     "error": "stale_decision", "pending": outcome.pending,
                                     "next_action": "fix_call"})
                elif outcome.status == "invalid_option":
                    if logger is not None:
                        logger.warning("rpc", "respond_invalid_option", session_id=sid, status=409)
                    self._send(409, {"operation": "respond", "status": "invalid_option", "session_id": sid,
                                     "error": "invalid_option", "pending": outcome.pending,
                                     "next_action": "fix_call"})
                elif outcome.status == "queued":
                    # Task 4/8: an async-question answer accepted while the executor is BUSY — the
                    # COMMON async case (it asked, then kept working). resolve_async_question already
                    # correlated + enqueued it; the monitor (sole PTY writer) delivers it at the next
                    # working->idle edge. Nothing was typed yet and nothing FAILED, so this is a 200,
                    # NOT the no_pending catch-all (a false 409/fix_call). next_action=refresh_status:
                    # the answer is in flight, so Hermes reconciles via status rather than ending its
                    # turn blindly on an unarmed waiter.
                    resp = {"operation": "respond", "status": "queued", "session_id": sid,
                            "next_action": "refresh_status"}
                    if outcome.snapshot is not None:
                        resp["snapshot"] = outcome.snapshot
                    self._send(200, resp)
                elif outcome.status == "not_delivered":
                    # Task 6/8: an async-question answer that could not be delivered — either the
                    # session went closing/terminal WHILE we resolved it (in-Session path, Task 4:
                    # reason=None) or the executor had ALREADY exited before the answer arrived
                    # (manager-level terminal-survival path, Task 6: reason="executor_finished").
                    # Either way the answer was correlated (mark_answered ran; nothing is left
                    # dangling) but nothing was typed. 200, not 4xx: this is a defined outcome, not
                    # a caller mistake to fix_call — next_action=refresh_status so Hermes reads the
                    # session's real final state (done/crashed/gone) before reporting to the user.
                    resp = {"operation": "respond", "status": "not_delivered", "session_id": sid,
                            "reason": outcome.reason, "next_action": "refresh_status"}
                    if outcome.snapshot is not None:
                        resp["snapshot"] = outcome.snapshot
                    self._send(200, resp)
                elif outcome.status == "terminal":
                    self._send(409, {"operation": "respond", "status": "terminal", "session_id": sid,
                                     "error": "session_terminal", "next_action": "refresh_status"})
                elif outcome.status == "unknown_session":
                    self._send(404, {"operation": "respond", "status": "unknown_session",
                                     "session_id": sid, "error": "unknown session",
                                     "next_action": "refresh_status"})
                elif outcome.status == "at_capacity":
                    # An idle follow-up that could not re-acquire an active slot (concurrency cap
                    # full). Surface HONEST backpressure (503), NOT no_pending — the decision exists,
                    # the slot doesn't. The orchestrator refreshes/retries once a slot frees.
                    if logger is not None:
                        logger.warning("rpc", "respond_at_capacity", session_id=sid, status=503)
                    self._send(503, {"operation": "respond", "status": "at_capacity", "session_id": sid,
                                     "error": "at_capacity", "next_action": "refresh_status"})
                else:   # no_pending
                    if logger is not None:
                        logger.warning("rpc", "respond_no_pending", session_id=sid, status=409,
                                       provided_decision_id=provided)
                    self._send(409, {"operation": "respond", "status": "no_pending", "session_id": sid,
                                     "error": "no_pending_decision", "next_action": "fix_call"})
            elif p.path == "/stop":
                owner_id = self._owner(body.get("owner_id") if isinstance(body, dict) else None)
                try:
                    outcome = manager.stop(body["session_id"], owner_id=owner_id)
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                if outcome.status == "unknown_session":
                    self._send(404, {"operation": "stop", "status": "unknown_session",
                                     "session_id": body["session_id"], "error": "unknown session",
                                     "next_action": "refresh_status"})
                else:
                    self._send(200, {"operation": "stop", "status": outcome.status,
                                     "session_id": body["session_id"], "snapshot": outcome.snapshot,
                                     "next_action": "report" if outcome.status == "stopped" else "refresh_status"})
            elif p.path == "/restart":
                # The owner is used ONLY to authorise the restart of the OLD session. The NEW
                # session's owner is read back off disk by the manager, never taken from this body.
                owner_id = self._owner(body.get("owner_id") if isinstance(body, dict) else None)
                try:
                    restart_sid = body["session_id"]
                except (KeyError, TypeError):
                    self._send(400, {"error": "missing field: 'session_id'"}); return
                # Shape-check BEFORE the manager ever resolves it (nelix-9a4.6 review finding #3):
                # a bad-shape sid here reaches `paths.sessions_root() / sid` on the crashed-session
                # disk-meta fallback path (`_restart_source`), and may spawn from what it finds.
                if not self._require_valid_sid(restart_sid):
                    return
                try:
                    outcome = manager.restart(restart_sid, owner_id=owner_id,
                                              force=bool(body.get("force", False)))
                except KeyError as e:
                    self._send(400, {"error": f"missing field: {e.args[0]}"}); return
                if outcome.status == "restarted":
                    self._send(200, {"operation": "restart", "status": "restarted",
                                     "session_id": outcome.session_id, "snapshot": outcome.snapshot,
                                     "lineage_id": outcome.lineage_id, "restart_count": outcome.restart_count,
                                     "next_after_seq": outcome.next_after_seq,
                                     "restarted_from": body["session_id"], "next_action": "end_turn"})
                elif outcome.status == "unknown_session":
                    self._send(404, {"operation": "restart", "status": "unknown_session",
                                     "error": "unknown session", "next_action": "refresh_status"})
                elif outcome.status == "restart_budget_exhausted":
                    if logger is not None:
                        logger.warning("rpc", "restart_budget_exhausted",
                                       session_id=body.get("session_id"),
                                       restart_count=outcome.restart_count,
                                       max_restarts=outcome.max_restarts, status=409)
                    self._send(409, {"operation": "restart", "status": "restart_budget_exhausted",
                                     "error": "restart_budget_exhausted",
                                     "restart_count": outcome.restart_count,
                                     "max_restarts": outcome.max_restarts, "next_action": "ask_user"})
                else:   # start_failed
                    self._send(409, {"operation": "restart", "status": "start_failed",
                                     "error": "start_failed", "next_action": "recover"})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    if is_unix:
        return _make_unix_server(transport.path, Handler)
    return ThreadingHTTPServer((transport.host, transport.port), Handler)


class UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        # Stale node from a prior daemon would EADDRINUSE; unlink first.
        try:
            os.unlink(self.server_address)
        except FileNotFoundError:
            pass
        # Bind via the grandparent so HTTPServer.server_bind's getfqdn()/(host,port) slicing of the
        # AF_UNIX path string never runs (it would set server_name/server_port to garbage and do a
        # reverse-DNS attempt at startup). server_name/port are meaningless for AF_UNIX.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


def _make_unix_server(path, handler):
    # Check BEFORE constructing the server: server_bind() unlinks any existing node before it
    # binds, so letting an over-long path reach it destroys the node and then dies with a bare
    # OSError("AF_UNIX path too long") that names nothing actionable.
    problem = paths.sun_path_overflow(path)
    if problem:
        raise ValueError(f"nelix daemon cannot bind its RPC socket: {problem}")
    server = UnixHTTPServer(path, handler, bind_and_activate=False)
    server.server_bind()
    os.chmod(path, 0o600)                 # node readable/writable by owner only; no listen yet
    server.server_activate()
    return server


def _evt_dict(e):
    return {"seq": e.seq, "event_id": e.event_id, "session_id": e.session_id,
            "executor": e.executor, "kind": e.kind, "summary": e.summary, "state": e.state,
            "hint": e.hint, "hung": e.hung, "task_delivery": e.task_delivery,
            "requires_response": e.requires_response, "screen_excerpt": e.screen_excerpt,
            "external_output_policy": EXTERNAL_OUTPUT_POLICY}
