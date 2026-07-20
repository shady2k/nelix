import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, replace

import paths
from daemon import owner
from daemon.drivers import get_driver
from daemon.env_resolver import EnvResolveError, resolve_env_cmds
from daemon.events import EXTERNAL_OUTPUT_POLICY
from daemon.launchers import get_launcher
from daemon.lease_client import LeaseClient
from daemon.model_cache import ModelCache
from daemon.model_discovery import discover, auth_of, DiscoveryError
from daemon.session import RespondOutcome, Session
from nelix_contracts.errors import (
    ADMISSION_UNAVAILABLE, CONCURRENCY_LIMIT, REBUILDING,
    STALE_RECONCILIATION_ID, UNKNOWN_SESSION, NelixError,
)

_MODEL_MAX_LEN = 128     # sane shape cap; nelix keeps NO model allowlist (the CLI is the authority)

# spec §3: "The generation's start endpoint accepts a router-assigned id." And: "Widen the id.
# `uuid.uuid4().hex[:8]` is 32 random bits — needlessly collision-prone ... Use a full UUID/ULID."
# A caller-supplied id becomes a `sessions/<sid>/` directory name VERBATIM, so this is a security
# boundary, not a nicety: `s-` plus a lowercase-hex-only charset makes path traversal / separators
# moot by construction (no character in the accepted alphabet means anything to a path parser).
# The range 8..64 accepts BOTH today's legacy self-mint (`s-` + 8 hex) and the wide id the future
# router will mint (a full UUID4 rendered as 32 hex chars, e.g. `s-` + 32 hex) without committing
# to one exact width — a ULID-as-hex rendering or a UUID's dashless hex both fit. Uppercase hex is
# deliberately rejected: one canonical casing, so two spellings of one id never look like two ids.
#
# No `$`/`^` anchors: `.fullmatch()` (used by `validate_session_id_shape` below) already pins both
# ends, and unlike `$`, `fullmatch()` does NOT let a trailing "\n" sneak through — `re.match(r"...$")`
# accepts "s-deadbeef\n" because `$` matches immediately before a final newline (review finding, this
# id becomes a directory name AND is exported as `NELIX_SESSION`/interpolated into hook curl URLs, so
# a smuggled newline breaks curl and silently drops the session's hooks + message channel).
_SESSION_ID_RE = re.compile(r"s-[0-9a-f]{8,64}")


class ModelRejected(ValueError):
    """A per-session `model` override that nelix refuses BEFORE spawning: a bad-shape value
    (empty / control chars / oversized) or a driver that cannot express a model override. A
    ValueError subclass (like PtyInputRejected) so /start maps it to 400 — caught ahead of the
    generic ValueError->409 branch (client input error, not daemon-full)."""


class ModelUnavailable(ValueError):
    """nelix-kwr: an explicitly-requested model is not offered by the executor's backend. Subclasses
    ValueError so the /start route catches it (before the generic 409) and returns 400 + the list."""
    def __init__(self, available_models):
        super().__init__("requested model is not offered by this executor")
        self.available_models = available_models          # [{"id","display_name"}], sorted, capped


class SessionIdRejected(ValueError):
    """A router-supplied `session_id` on /start (spec §3) with bad shape: empty, path separators/
    traversal, or anything outside `_SESSION_ID_RE`. A ValueError subclass (like ModelRejected) so
    /start maps it to 400 (client input error) ahead of the generic ValueError->409 branch."""


class SessionIdInUse(ValueError):
    """A router-supplied `session_id` (spec §3) that already names a session — live in the
    registry, or a directory persisted on disk from a session that ran before. NEVER silently
    reused or clobbered: the id becomes a `sessions/<sid>/` directory name, and reusing one would
    mix an old (or another live) session's on-disk state into this start. A ValueError subclass
    so it reaches /start's exception chain, but rpc_server maps it to the stable error envelope
    (code `session_id_in_use`) rather than the generic bare-string 409."""

    def __init__(self, session_id):
        super().__init__(f"session_id already in use: {session_id!r}")
        self.session_id = session_id


def validate_session_id_shape(session_id):
    """Shape-check a caller-supplied `session_id`. Raises SessionIdRejected. See `_SESSION_ID_RE`
    for the accepted alphabet/width and why it is safe as a directory-name component verbatim.

    NOT underscore-prefixed: this is THE one shared daemon-local validator, reused verbatim by
    `daemon/rpc_server.py` for every route that takes a caller-supplied session_id used as a path
    component (spec review nelix-9a4.6 finding #3, plus a follow-up review pass that caught /wait,
    /respond and /stop) — /start (via `_spawn` below), /status, /dialog, /screen, /restart,
    /hook/<sid>, /message/<sid>, /wait, /respond, /stop. One regex, one place the accepted shape
    can be read, rather than a copy per route that could quietly drift apart.

    `.fullmatch()`, not `.match()` against an anchored pattern: `re.match(r"...$")` would accept
    "s-deadbeef\\n" because `$` matches just before a trailing newline. `fullmatch()` has no such
    gap — the whole string must match, full stop.
    """
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise SessionIdRejected(f"invalid session_id: {session_id!r}")


def _validate_model_shape(model):
    """Shape-only validation (spec §5): pass-through — nelix keeps no allowlist, the CLI is the
    authority on model validity. A clean value is forwarded VERBATIM; the checks run on the ORIGINAL
    string (never a normalized copy) so an edge control char or surrounding whitespace is REJECTED,
    not silently trimmed-and-accepted. `.strip()` is used ONLY to detect the empty/whitespace-only
    case. Returns the value unchanged, or raises ModelRejected."""
    if not isinstance(model, str):
        raise ModelRejected(f"model must be a string, got {type(model).__name__}")
    if not model.strip():
        raise ModelRejected("model is empty or whitespace-only")
    if len(model) > _MODEL_MAX_LEN:
        raise ModelRejected(f"model is too long (max {_MODEL_MAX_LEN} chars)")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in model):
        raise ModelRejected("model contains ASCII control characters (incl newline/tab)")
    if model != model.strip():
        raise ModelRejected("model has leading or trailing whitespace")
    return model


def _strip_model_flag(args, flag):
    """Remove any existing occurrence of `flag` in BOTH `<flag> <v>` and `<flag>=<v>` forms (the
    fold is driver-flag-based, never a globally-hardcoded '--model'). Returns (cleaned, stripped)."""
    cleaned, stripped, i, n = [], False, 0, len(args)
    eq = flag + "="
    while i < n:
        a = args[i]
        if a == flag:
            stripped = True
            i += 2 if i + 1 < n else 1      # drop the flag AND its value (or a malformed trailing flag)
            continue
        if a.startswith(eq):
            stripped = True
            i += 1
            continue
        cleaned.append(a)
        i += 1
    return cleaned, stripped


@dataclass
class StartOutcome:
    session_id: str
    base_seq: int
    snapshot: dict = None


@dataclass
class StopOutcome:
    status: str                    # 'stopped' | 'stop_requested' | 'unknown_session'
    snapshot: dict = None


@dataclass
class RestartOutcome:
    status: str                    # 'restarted' | 'unknown_session' | 'restart_budget_exhausted' | 'start_failed'
    session_id: str = None
    lineage_id: str = None
    restart_count: int = None
    max_restarts: int = None
    next_after_seq: int = None
    snapshot: dict = None


def _session_activity(d):
    """Last-activity ts: transcript.jsonl mtime, else newest file mtime, else dir mtime.
    Dir mtime alone doesn't move when files inside are written, so a live session could
    look stale by it — never use it as the safety rule (registered-id exclusion is)."""
    tj = d / "transcript.jsonl"
    try:
        if tj.exists():
            return tj.stat().st_mtime
        mtimes = [f.stat().st_mtime for f in d.iterdir() if f.is_file()]
        return max(mtimes) if mtimes else d.stat().st_mtime
    except OSError:
        return 0.0


def _rmtree(d, logger):
    try:
        shutil.rmtree(d)
    except OSError as e:
        if logger is not None:
            logger.warning("manager", "session_gc_skip", dir=str(d), err=str(e))


def gc_sessions(keep_ids, retain, max_age_days, now=None, logger=None):
    """Prune inactive session dirs by age then count. NEVER touches a dir whose name is
    in keep_ids (registered/active) — exclusion-before-delete is the only safety rule.
    retain/max_age_days of 0 disable that rake. Best-effort."""
    now = time.time() if now is None else now
    root = paths.sessions_root()
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and d.name not in keep_ids]
    except FileNotFoundError:
        return
    survivors = []
    for d in dirs:
        if max_age_days and (now - _session_activity(d)) / 86400.0 > max_age_days:
            _rmtree(d, logger)
        else:
            survivors.append(d)
    if retain and len(survivors) > retain:
        survivors.sort(key=_session_activity)              # oldest first
        for d in survivors[:len(survivors) - retain]:
            _rmtree(d, logger)


def _session_capabilities(session_id, sess):
    """The per-session /capabilities payload (spec §8) for a LIVE session, built from real facts:
    `sess._driver.hook_capable` and `sess._launcher.capabilities` (the same private attributes
    `screen()` already reads `sess._cols`/`sess._rows` off — reaching into a live Session from the
    manager is an established pattern here, not a new one).

    Review correction (nelix-9a4.6 fix pass): this used to also emit a per-operation
    `operations: {op: {"supported": ...}}` map, including a `message` entry coded
    `unsupported_by_generation` whenever `hook_capable` was False. Both reviewers flagged that as
    FABRICATED: spec §8's `unsupported_by_generation` names a CROSS-GENERATION incompatibility
    (an operation an OLDER session cannot serve, checked against THIS generation's capability) —
    it has nothing to do with `hook_capable`, a per-DRIVER fact that varies within one generation.
    Worse, `/message` (daemon/rpc_server.py `_dispatch_message`) does not actually gate on
    `hook_capable` at all, so the removed map advertised a failure code the operation could never
    return. The real §8 "OR per-session capabilities" mechanism, delivered now, is just the FACTS
    below (session-scoped, truthful, nothing fabricated); a genuine `unsupported_by_generation`
    response is DEFERRED to Plan 4 (nelix-3rm's successor, multi-generation lifecycle), where more
    than one generation coexisting makes the cross-generation case real and worth gating on.
    """
    hook_capable = bool(getattr(sess._driver, "hook_capable", False))
    caps = sess._launcher.capabilities
    return {
        "session_id": session_id,
        "executor": sess.executor,
        "hook_capable": hook_capable,
        "isolation_class": caps.isolation_class,
        "can_attach": caps.can_attach,
    }


def _default_session_factory(sid, executor, spec, events, launcher_factory,
                             driver_factory, logger):
    return Session(sid, executor, driver_factory(spec.driver),
                   launcher_factory(spec.launcher), spec, events, logger=logger)


class SessionManager:
    """Registry of sessions. Holds <= concurrency_limit (config-driven, default 5).

    When ``lease_client`` is provided, admission is managed through the ROUTER-OWNED lease
    service with local choreography (§3.3b): acquire leases outside ``_lock``, validate
    under ``_lock``, commit or release. Without ``lease_client``, the old local cap check
    is used (backward compat for tests not wired to a router).
    """

    def __init__(self, specs, events, store, launcher_factory=None, driver_factory=None,
                 concurrency_limit=5, idle_retained_limit=None, logger=None, session_factory=None,
                 session_retain=20, session_max_age_days=7, reaper_ctx=None,
                 terminal_snapshot_ttl=300.0, clock=time.time,
                 lease_client=None, generation_id=None, generation_epoch=None):
        self._specs = specs
        self._events = events
        self._limit = concurrency_limit
        # An `idle` session frees its active slot but is retained alive; this bounds how many such
        # completed-but-unclosed sessions we keep. Defaults to the active concurrency limit.
        self._idle_limit = idle_retained_limit if idle_retained_limit is not None else concurrency_limit
        self._logger = logger
        self._session_retain = session_retain
        self._session_max_age_days = session_max_age_days
        self._reaper_ctx = reaper_ctx
        self._sessions = {}
        self._lineages = {}            # lineage_id -> restart count (durable across session removal)
        self._reserved = 0             # in-flight restart slot reservations (cap accounting)
        self._terminal = {}            # sid -> (snapshot_dict, expires_at, advertised): disappeared-session relay
        # sid -> ({"id":..., "reason":"executor_finished"}, expires_at): Task 6 terminal survival for
        # an async question still outstanding when the executor exits. Written ALONGSIDE self._terminal
        # in _free_slot with the SAME expires_at (one lifetime policy, not two) — never write this
        # without also having just computed self._terminal's expiry for the same session.
        self._terminal_async = {}
        # S5a: terminal obligation ledger — every admitted live session owns ONE outstanding
        # obligation from spawn, discharged ONLY after its terminal is persisted. This set (NOT a
        # counter) is the quiescence barrier: _sessions empty is NOT sufficient to certify.
        self._terminal_obligations = set()
        # S5a: terminal-pending-confirmation inventory — sessions whose terminal has been persisted
        # but NOT yet confirmed by the router as board-visible/owner-acked/validly-expired. Entries
        # consume NEITHER active NOR live leases (both were released at persist). Dropped when the
        # router advances confirmed_high_water past this terminal's seq.
        self._terminal_pending = {}    # sid -> {terminal_kind, terminal_seq, epoch}
        # S5a: quiescence flag — when set, reject new sessions, restarts, idle-resume, pending admissions.
        self._quiescent = False
        self._terminal_ttl = terminal_snapshot_ttl
        self._clock = clock
        # nelix-9a4.4: the durable store for terminal records (generation-neutral). MANDATORY:
        # a generation daemon is always router-fronted, and the router owns the start ledger —
        # there is no standalone/storeless mode to support.
        self._store = store
        # Same seam _make uses for session construction, reused for the model-capability read (a
        # model override reads driver.model_flag). Never call get_driver() directly — that would
        # bypass an injected factory (tests / custom drivers).
        self._driver_factory = driver_factory or get_driver
        # Same seam, for the generation-level /capabilities baseline (nelix-9a4.6 deliverable C):
        # a configured executor's STATIC launcher capabilities, read without spawning anything.
        self._launcher_factory = launcher_factory or get_launcher
        # nelix-kwr: pre-flight model-membership cache, one per daemon (fresh random salt each
        # start). Protocol-agnostic; _check_model_available reads the driver's own models_protocol
        # and passes it through to .models(), so the cache never assumes which strategy runs.
        self._model_cache = ModelCache(discover_fn=discover)
        self._lock = threading.Lock()
        # S3a: router lease client for admission. When None, fall back to local cap check.
        self._lease_client = lease_client
        self._gen_id = generation_id or os.environ.get("NELIX_GENERATION_ID", "")
        self._gen_epoch = generation_epoch or os.environ.get("NELIX_GENERATION_EPOCH", "")
        # Local choreography state
        self._pending_acquire = set()           # sids with an in-flight lease acquire
        # sid -> active-token (set by start/send_turn; cleared by idle/terminal release)
        self._active_lease_tokens = {}
        # sid -> live-token (set by start; cleared by terminal release)
        self._live_lease_tokens = {}
        # S3b: activation_id used when each token was acquired (snapshot must
        # carry the ACTUAL acquisition activation_id, not the session's current
        # activation counter).
        self._active_token_activation = {}
        self._live_token_activation = {}
        # S3b: in-flight handshake guard — only one thread registers per id.
        self._handshake_in_flight = None
        self._handshake_lock = threading.Lock()
        if session_factory is not None:
            self._make = lambda sid, ex, spec: session_factory(sid, ex, spec, events)
        else:
            self._make = lambda sid, ex, spec: _default_session_factory(
                sid, ex, spec, events, launcher_factory, driver_factory, logger)

    def _store_epoch_is_certified(self):
        """Check the store directly if the epoch is certified.
        Fail-closed on error: treat unreadable as certified (reject).
        """
        if not self._gen_epoch:
            return False
        try:
            state = self._store.get_epoch_retirement_state(self._gen_epoch)
            return state == "certified"
        except Exception:
            return True

    def _check_quiescent(self):
        """Check if this epoch is quiescing. Updates local flag from store.
        D2: FAIL-CLOSED on any NelixError EXCEPT UNKNOWN_SESSION (epoch genuinely
        absent — treat as not quiescing). STORE_UNAVAILABLE and any other error
        MUST reject admission.
        FIX 5: also rejects ``process_state=dead`` — a dead epoch must not admit.
        """
        if not self._gen_epoch:
            self._quiescent = False
            return False
        try:
            ep = self._store._conn.execute(
                "SELECT process_state, retirement_state FROM epochs "
                "WHERE generation_epoch=?", (self._gen_epoch,)).fetchone()
            if ep is None:
                self._quiescent = True
                return True
            if ep["process_state"] == "dead":
                self._quiescent = True
                return True
            state = ep["retirement_state"]
            self._quiescent = (state == "quiescing" or state == "certified")
        except NelixError as e:
            if e.code == UNKNOWN_SESSION:
                self._quiescent = False
            else:
                self._quiescent = True
        except Exception:
            self._quiescent = True
        return self._quiescent

    def _owned(self, session_id, owner_id):
        """The owner-scoped session lookup behind EVERY caller-facing route (daemon/owner.py).

        Returns the live Session, or None if `owner_id` does not own it — INCLUDING when the
        session plainly exists. A non-owner gets exactly what a wrong session id gets, because
        the owner is a NAMESPACE: another harness's session does not exist in this caller's
        world, so "unknown session" is the honest answer and no route needs a new error shape.

        `owner_id` is positional-required on every caller of this, never defaulted: a default
        would be a shared owner, and a shared owner is the bug. The ownership question is asked
        of the DURABLE RECORD (outside self._lock — it is disk I/O, and the answer cannot be
        invalidated by the lock: nothing ever rewrites a session's owner).
        """
        if not owner.owns_session(session_id, owner_id):
            return None
        with self._lock:
            return self._sessions.get(session_id)

    def _owned_sids(self, session_ids, owner_id):
        return {sid for sid in session_ids if owner.owns_session(sid, owner_id)}

    def start(self, executor_name, task, cwd, *, owner_id, session_id, model=None):
        if self._lease_client is not None:
            return self._lease_start(executor_name, task, cwd, owner_id=owner_id,
                                     session_id=session_id, model=model)
        return self._spawn(executor_name, task, cwd, lineage_id=None, restarted_from=None,
                           owner_id=owner_id, model=model, session_id=session_id)

    def _lease_start(self, executor_name, task, cwd, *, owner_id, session_id, model=None):
        """Start path with router lease choreography (§3.3b).

        Pre-lock validation, acquire {active, live} atomically from the router, then
        re-take lock for final validation + session creation.

        Preserves NelixError codes so the daemon RPC layer can relay the proper retryable
        error envelope (FIX C1). Maps LeaseClient.RouterUnavailable ->
        NelixError(ADMISSION_UNAVAILABLE).
        """
        owner.validate(owner_id)
        spec = self._specs.get(executor_name)
        if spec is None:
            raise RuntimeError(f"unknown executor: {executor_name!r}")
        cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"cwd does not exist or is not a directory: {cwd!r}")
        try:
            validate_session_id_shape(session_id)
        except SessionIdRejected:
            raise
        if self._session_id_exists(session_id):
            raise SessionIdInUse(session_id)
        applied_model = None
        if model is not None:
            spec, applied_model = self._apply_model_override(spec, executor_name, model)
            self._check_model_available(spec, executor_name, applied_model)

        # S5a: reject admission if this epoch is quiescing.
        if self._check_quiescent():
            raise RuntimeError(f"epoch {self._gen_epoch} is quiescing; no new sessions")

        # Under lock: install pending-acquire marker so racing send_turns etc. see it.
        with self._lock:
            if session_id in self._pending_acquire:
                raise RuntimeError(f"concurrent start in progress for {session_id}")
            self._pending_acquire.add(session_id)

        # FIX 4: opportunistic outbox retry before admission.
        self.retry_lease_outbox()
        tokens = None
        act_start = 0
        try:
            tokens = self._lease_client.acquire(
                self._gen_id, self._gen_epoch, session_id, act_start,
                {"active", "live"})
        except LeaseClient.RouterUnavailable as e:
            with self._lock:
                self._pending_acquire.discard(session_id)
            raise NelixError(ADMISSION_UNAVAILABLE,
                             f"lease service unreachable: {e}") from e
        except NelixError:
            with self._lock:
                self._pending_acquire.discard(session_id)
            self._ensure_handshake()
            raise
        except Exception as e:
            with self._lock:
                self._pending_acquire.discard(session_id)
            raise RuntimeError(f"lease acquire failed: {e}") from e

        try:
            started = self._spawn(
                executor_name, task, cwd, lineage_id=None, restarted_from=None,
                owner_id=owner_id, model=model, session_id=session_id,
                lease_tokens=tokens)
            # Record the activation_id used for this acquire.
            act_str = str(act_start)
            if tokens.get("active", {}).get("fresh"):
                with self._lock:
                    self._active_token_activation[session_id] = act_str
            if tokens.get("live", {}).get("fresh"):
                with self._lock:
                    self._live_token_activation[session_id] = act_str
            return started
        except Exception:
            self._release_lease_tokens(tokens)
            raise
        finally:
            with self._lock:
                self._pending_acquire.discard(session_id)

    def _release_lease_tokens(self, tokens):
        """Best-effort release of lease tokens (active + live). Called on rollback paths.

        ``tokens`` is the dict returned by ``acquire``: ``{"active": {"token_id": ...,
        "fresh": ...}, "live": ...}``. Only tokens whose ``fresh`` is True are released
        (idempotent tokens are owned by the original acquirer).

        FIX D: the tokens dict is captured (already built before this call), so no lock is
        held when the release RPCs fire. Callers must ensure tokens are staged before dropping
        ``manager._lock``.
        """
        if self._lease_client is None or not tokens:
            return
        for info in tokens.values():
            if isinstance(info, dict) and info.get("fresh") is False:
                continue
            tid = info.get("token_id") if isinstance(info, dict) else info
            if tid:
                try:
                    self._lease_client.release(tid)
                except Exception:
                    if self._logger is not None:
                        self._logger.warning("manager", "lease_release_error",
                                             token_id=tid, exc_info=True)
        self.retry_lease_outbox()

    def _extract_tid(self, info):
        """Extract token_id from acquire result entry (dict or legacy string)."""
        if isinstance(info, dict):
            return info.get("token_id")
        return info

    def _release_lease_tokens_staged(self, staged):
        """Release a list of token_ids previously staged under lock. FIX D."""
        if self._lease_client is None or not staged:
            return
        for tid in staged:
            try:
                self._lease_client.release(tid)
            except Exception:
                if self._logger is not None:
                    self._logger.warning("manager", "lease_release_error",
                                         token_id=tid, exc_info=True)

    def _make_on_idle(self, sid):
        """Return a callback for ``Session.on_idle`` that releases the active lease and
        increments the activation counter for the next lease acquire."""
        def _on_idle(_sid):
            with self._lock:
                token = self._active_lease_tokens.pop(_sid, None)
                sess = self._sessions.get(_sid)
                if sess is not None:
                    sess._activation_counter = getattr(sess, "_activation_counter", 0) + 1
            if token is not None and self._lease_client is not None:
                try:
                    self._lease_client.release(token)
                except Exception:
                    if self._logger is not None:
                        self._logger.warning("manager", "lease_release_error",
                                             session_id=_sid, exc_info=True)
        return _on_idle

    def _session_id_exists(self, sid):
        """True if `sid` already names a session — live in the registry, or a directory
        persisted on disk from a session that ran before (even one long exited; gc_sessions may
        since have pruned it, in which case the id is free again). Checked before honoring a
        router-supplied id (spec §3): the id becomes a `sessions/<sid>/` directory name verbatim,
        so silently reusing one would mix another session's on-disk state into this start."""
        with self._lock:
            if sid in self._sessions:
                return True
        return (paths.sessions_root() / sid).exists()

    def _apply_model_override(self, spec, executor_name, model):
        """Validate + fold a per-session `model` into a fresh per-session ExecutorSpec (last-wins).
        Runs BEFORE the session lock and cap checks so a bad/unsupported model returns 400, not a
        409 'daemon full'. Idempotent — re-applying the already-validated value (the restart/recovery
        path does exactly this against the fresh original spec) re-strips + re-folds to the same argv.
        Returns (folded_spec, validated_model). Raises ModelRejected on bad shape or an incapable driver."""
        model = _validate_model_shape(model)
        flag = getattr(self._driver_factory(spec.driver), "model_flag", None)
        if flag is None:
            raise ModelRejected(
                f"executor {executor_name!r} (driver {spec.driver!r}) does not support a model override")
        cleaned, stripped = _strip_model_flag(spec.args, flag)
        if stripped and self._logger is not None:
            # A toml-pinned model was overridden — an operationally significant, otherwise-silent action.
            self._logger.info("manager", "model_override_applied", executor=executor_name,
                              model=model, driver=spec.driver)
        return replace(spec, args=[*cleaned, flag, model]), model

    def _check_model_available(self, spec, executor_name, model):
        """nelix-kwr pre-flight: reject a model the backend does not offer BEFORE spawning. Runs
        before the session lock. FAIL-OPEN on any discovery ambiguity (no protocol / alias / no auth
        token / discovery error) — the CLI stays the authority; raise ModelUnavailable ONLY on a
        confident miss. EnvResolveError propagates (mapped to 502 upstream), NOT swallowed."""
        driver = self._driver_factory(spec.driver)
        protocol = getattr(driver, "models_protocol", None)
        if protocol is None:
            return
        aliases = getattr(driver, "model_aliases", frozenset())
        if model.lower() in {a.lower() for a in aliases}:
            return
        # Same resolved env the child gets at spawn (EnvResolveError propagates as today's 502).
        env = {**spec.resolved_env(),
               **resolve_env_cmds(spec.env_cmd, os.environ, spec.env_cmd_timeout_seconds,
                                  logger=self._logger)}
        kind, token = auth_of(env)
        if kind is None:
            self._log_validation_skipped(executor_name, "no_auth")
            return
        base = env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        try:
            models = self._model_cache.models(executor_name, base, kind, token, env, protocol)
            if not self._model_present(model, models):
                models = self._model_cache.models(executor_name, base, kind, token, env, protocol,
                                                   force=True)
                if not self._model_present(model, models):
                    raise ModelUnavailable(sorted(models, key=lambda m: m["id"]))
        except DiscoveryError as e:
            self._log_validation_skipped(executor_name, e.reason)    # fail-open
            return

    @staticmethod
    def _model_present(model, models):
        return model.lower() in {m["id"].lower() for m in models}

    def _log_validation_skipped(self, executor, reason):
        if self._logger is not None:
            self._logger.info("manager", "model_validation_skipped", executor=executor, reason=reason)

    def _log_spawn_failure(self, event, session_id, exc):
        """Log a spawn/restart failure. An EnvResolveError (nelix-c5o) is logged REDACTED — only
        {var, reason}, WITHOUT exc_info: the exception is raised `from None` and stores no command /
        stdout / stderr (so even a traceback could not leak the secret; spec §5), and a structured
        record is cleaner. Every other error keeps the exc_info traceback for diagnosis."""
        if self._logger is None:
            return
        if isinstance(exc, EnvResolveError):
            self._logger.error("manager", event, session_id=session_id,
                               reason="env_resolve_failed", var=exc.var, resolve_reason=exc.reason)
        else:
            self._logger.error("manager", event, session_id=session_id, exc_info=True)

    def _spawn(self, executor_name, task, cwd, *, lineage_id, restarted_from, owner_id,
               session_id, reserve=False, model=None, lease_tokens=None):
        # S5a: reject admission if epoch is quiescing.
        if self._check_quiescent():
            raise RuntimeError(f"epoch {self._gen_epoch} is quiescing; no new sessions")
        # reserve=True: a slot reservation was made for us by restart() (old session popped +
        # self._reserved bumped under the lock). We OWN that reservation and must release it exactly
        # once: consume it ATOMICALLY with inserting the new session (so len(_sessions)+_reserved
        # never overcounts), or release it in `finally` if we raise before inserting.
        # lease_tokens: dict of kind->token from a router lease acquire. When provided, the local
        # cap check is skipped (the lease already verified capacity). Stored on the session for
        # eventual release on idle/terminal.
        consumed = not reserve
        try:
            # Shape-check the owner FIRST, before the lock and the cap checks (same reasoning as
            # the model override): a bad owner_id is the caller's input error -> 400, never a 409
            # "daemon full" that invites a retry. OwnerRejected subclasses ValueError; /start
            # catches it ahead of the generic ValueError->409 branch.
            owner.validate(owner_id)
            spec = self._specs.get(executor_name)
            if spec is None:
                if self._logger is not None:
                    self._logger.warning("manager", "session_start_rejected",
                                         reason="unknown_executor", executor=executor_name)
                raise RuntimeError(f"unknown executor: {executor_name!r} "
                                   f"(configured: {sorted(self._specs)})")
            cwd = os.path.abspath(os.path.expanduser(cwd))
            if not os.path.isdir(cwd):          # host-side: fail fast, no session, no auto-mkdir
                if self._logger is not None:
                    self._logger.warning("manager", "session_start_rejected",
                                         reason="bad_cwd", executor=executor_name)
                raise ValueError(f"cwd does not exist or is not a directory: {cwd!r}")
            # Router-assigned session id (spec §3, nelix-9a4.6 deliverable A): validated + collision-
            # checked BEFORE the lock/cap checks, same reasoning as owner/model above — a bad-shape or
            # colliding id is the caller's input error, never a 409 "daemon full". ALWAYS required:
            # every session arrives through the router, which owns the start ledger.
            try:
                validate_session_id_shape(session_id)
            except SessionIdRejected:
                if self._logger is not None:
                    self._logger.warning("manager", "session_start_rejected",
                                         reason="invalid_session_id", executor=executor_name)
                raise
            if self._session_id_exists(session_id):
                if self._logger is not None:
                    self._logger.warning("manager", "session_start_rejected",
                                         reason="session_id_in_use", executor=executor_name)
                raise SessionIdInUse(session_id)
            # Per-session model override (nelix-9k0): validate + fold BEFORE the lock/cap checks so a
            # bad-shape or unsupported-driver model returns 400, never a 409 "daemon full". Omitted
            # model -> spec is untouched (byte-identical to pre-feature). The validated value is stored
            # on the session (+ meta) so an auto-restart re-injects the SAME model, not the default.
            applied_model = None
            if model is not None:
                spec, applied_model = self._apply_model_override(spec, executor_name, model)
                self._check_model_available(spec, executor_name, applied_model)
            with self._lock:
                # Split accounting (spec §slots): the active cap counts only sessions occupying an
                # active slot (everything except `idle`) + in-flight restart reservations; a retained
                # `idle` session frees its active slot but is bounded by idle_retained_limit. A restart
                # (reserve=True) reuses its own net-zero slot and skips both caps.
                # When lease_tokens is provided, the router lease service already checked capacity.
                if not reserve and lease_tokens is None:
                    if self._active_count() + self._reserved >= self._limit:
                        if self._logger is not None:
                            self._logger.warning("manager", "session_start_rejected",
                                                 reason="concurrency_limit", executor=executor_name)
                        raise RuntimeError(
                            f"concurrency_limit={self._limit} reached "
                            f"(active: {sorted(self._sessions)})")
                # FIX E: when router leases are authoritative, bypass BOTH local caps.
                if not reserve and lease_tokens is None and self._idle_count() >= self._idle_limit:
                    if self._logger is not None:
                        self._logger.warning("manager", "session_start_rejected",
                                             reason="idle_retained_limit", executor=executor_name)
                    raise RuntimeError(
                        f"idle_retained_limit={self._idle_limit} reached "
                        f"(close a completed session with nelix_stop before starting more)")
                if session_id in self._sessions:   # closes the TOCTOU window vs. the pre-lock check
                    if self._logger is not None:
                        self._logger.warning("manager", "session_start_rejected",
                                             reason="session_id_in_use", executor=executor_name)
                    raise SessionIdInUse(session_id)
                sid = session_id
                base_seq = self._events.latest_seq()
                # B3: re-check quiescence under _lock so a concurrent begin_quiescence
                # between the pre-lock check and here is not missed.
                # FIX 5: re-read AUTHORITATIVE state (not cached _quiescent) at commit.
                if self._check_quiescent():
                    raise RuntimeError(
                        f"epoch {self._gen_epoch} is quiescing or dead; no new sessions")
                sess = self._make(sid, executor_name, spec)
                sess.on_terminal = self._free_slot
                sess._persist_terminal = self._persist_terminal_for_publish
                self._terminal_obligations.add(sid)
                # Task 4: the monitor delivers a queued async reply as a fresh turn but has no manager
                # handle of its own — give it one that re-acquires an active slot (send_turn), so the
                # slot accounting an idle-freed session needs is preserved on the monitor-driven write.
                sess.deliver_turn = lambda text, _sid=sid: self.send_turn(_sid, text)
                sess.reaper_ctx = self._reaper_ctx
                sess.lineage_id = lineage_id or sid          # first in chain -> lineage = own id
                sess.restarted_from = restarted_from
                sess.restart_count = self._lineages.get(sess.lineage_id, 0)
                sess.model = applied_model                   # validated override (or None): survives restart
                # S3a: store lease tokens on the session for release on idle/terminal.
                sess._activation_counter = 0
                if lease_tokens is not None:
                    active_info = lease_tokens.get("active") or {}
                    live_info = lease_tokens.get("live") or {}
                    active_tid = self._extract_tid(active_info)
                    live_tid = self._extract_tid(live_info)
                    if active_tid:
                        self._active_lease_tokens[sid] = active_tid
                        self._active_token_activation[sid] = str(0)
                    if live_tid:
                        self._live_lease_tokens[sid] = live_tid
                        self._live_token_activation[sid] = str(0)
                    # Set idle callback to release the active lease when the session goes idle.
                    sess.on_idle = self._make_on_idle(sid)
                else:
                    sess._activation_counter = 0
                self._sessions[sid] = sess
                if reserve:
                    self._reserved -= 1                      # consume atomically with the insert
                    consumed = True
                keep = set(self._sessions)
        finally:
            if not consumed:
                with self._lock:
                    self._reserved -= 1                      # raised before insert: release the reservation
        if self._logger is not None:
            self._logger.info("manager", "session_created", session_id=sid,
                              executor=executor_name, cwd=cwd,
                              lineage_id=sess.lineage_id, restarted_from=restarted_from,
                              slot=f"{len(keep)}/{self._limit}")
        gc_sessions(keep, self._session_retain, self._session_max_age_days, logger=self._logger)
        # The session is now in memory but NOT yet durable. The try block below creates
        # the durable row, writes the owner record, and spawns the PTY — in that order.
        # On any failure, ALL durable state is rolled back (nelix-9a4.4).
        try:
            self._store.create_session(
                sid, state="starting", executor=executor_name, task=task,
                cwd=cwd, model=applied_model, created_at=self._clock())
            # AUTHORITATIVE, and ordered: the owner record is durable BEFORE the PTY spawns and
            # therefore before /start can return the session id. An unwritable record raises
            # OwnerWriteFailed and the existing teardown below un-registers the session, so a
            # start that cannot be attributed spawns no process and returns no id — the caller
            # never learns of a session it could not have driven anyway. (Between the insert
            # above and this line the session is registered with no record; that window is safe
            # because owner_of fails CLOSED — the session is invisible to everyone, including
            # its own owner, rather than visible to all.)
            owner.write(paths.sessions_root() / sid, owner_id)
            sess.start(task, cwd)
        except Exception as e:
            try:
                sess.stop()                       # tear down any partially-spawned PTY / open dialog
            except Exception:
                pass
            # Roll back durable state: transition the store's session row to "failed"
            # so the router's ledger.fail() does not conflict with a live sessions row.
            try:
                self._store.transition_session(sid, owner_id=owner_id, state="failed")
            except Exception:
                pass
            with self._lock:                      # don't leak a registered-but-unstarted session
                self._sessions.pop(sid, None)     # reservation already consumed: slot frees cleanly
            self._log_spawn_failure("session_start_failed", sid, e)
            raise
        return StartOutcome(session_id=sid, base_seq=base_seq, snapshot=sess.snapshot())

    def _restart_source(self, session_id, owner_id):
        """Resolve (executor, task, cwd, lineage_id, active_session_or_None, model, stored_owner)
        for a restart. Source is an ACTIVE session if present, else the PERSISTED session-dir meta
        (the main path: a crashed/done session has already been removed from _sessions). `model` is
        the per-session override to re-apply (or None); OLD meta lacking the field defaults to None
        (no override, the pre-nelix-9k0 behaviour). Returns None if neither source exists, or if
        `owner_id` does not own the session.

        `stored_owner` comes from the DURABLE RECORD and is what the restarted session is written
        with — the request's owner_id only ever AUTHORISES, it is never propagated. The guard doing
        the real work is the comparison below: a session whose record is missing or malformed
        fails closed and is refused, rather than being silently RE-OWNED by whoever asked to
        restart it. That matters because EVERY crashed session is resolved through this path, so
        "ownerless => free" would be the common case, not a corner.

        Be honest about what is and is not proved here: once ownership is established,
        `stored_owner` and `owner_id` are the SAME string, so spawning with one or the other is
        indistinguishable — mutating `stored_owner` to `owner_id` leaves the whole suite green
        (measured). Reading it off the record is therefore structural, not behavioural: it keeps
        "the new session's owner comes from disk" true by construction if the check is ever moved
        or relaxed. The behavioural guard is `owner.session_owned_by`, and killing that turns
        three restart tests red.

        It is `session_owned_by` and NOT `owner_of` + `==` for a reason paid for once already: a
        hand-rolled comparison here silently reintroduced the None-matches-None skeleton key (a
        caller passing owner_id=None matching an ownerless session), and the whole suite stayed
        green because the RPC route validates first. One read, one primitive, one place for the
        trap to live.
        """
        stored_owner = owner.session_owned_by(session_id, owner_id)
        if stored_owner is None:      # not ours, or no trustworthy record -> fail closed
            return None
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is not None:
            return (sess.executor, sess.task, sess.cwd,
                    sess.lineage_id or session_id, sess, getattr(sess, "model", None), stored_owner)
        try:
            meta = json.loads(paths.session_meta(paths.sessions_root() / session_id).read_text())
        except (OSError, ValueError):
            return None
        if not meta.get("executor") or meta.get("cwd") is None:
            return None
        return (meta["executor"], meta.get("task"), meta["cwd"],
                meta.get("lineage_id") or session_id, None, meta.get("model"), stored_owner)

    def restart(self, session_id, *, new_session_id, owner_id, force=False):
        # S5a: reject restart spawn if epoch is quiescing.
        if self._check_quiescent():
            return RestartOutcome("start_failed")
        src = self._restart_source(session_id, owner_id)
        if src is None:
            return RestartOutcome("unknown_session")
        executor, task, cwd, lineage_id, active, model, stored_owner = src
        spec = self._specs.get(executor)
        max_restarts = spec.max_restarts if spec is not None else 0
        with self._lock:
            count = self._lineages.get(lineage_id, 0)
            if not force and count >= max_restarts:
                return RestartOutcome("restart_budget_exhausted", lineage_id=lineage_id,
                                      restart_count=count, max_restarts=max_restarts)
            if force:
                count = 0
            count += 1
            self._lineages[lineage_id] = count
            # RE-VALIDATE liveness UNDER the lock: the session resolved as active may have exited
            # between _restart_source and here (its monitor's _free_slot popped it). Only take the
            # net-zero reserve path if it is STILL in _sessions now; otherwise its slot was already
            # freed -> compete for a slot like a fresh start (normal cap check), so we can't bypass
            # the cap on a session that another start has since replaced.
            still_active = session_id in self._sessions
            if still_active:
                self._sessions.pop(session_id, None)
                self._reserved += 1
            reserve = still_active
        if still_active:
            try:
                active.stop()
            except Exception:
                if self._logger is not None:
                    self._logger.warning("manager", "restart_stop_error",
                                         session_id=session_id, exc_info=True)
        # FIX B1: ALWAYS acquire router leases for a restart spawn (both active and terminal
        # paths). _free_slot (called from the old session's stop path) releases the old leases.
        lease_tokens = None
        if self._lease_client is not None:
            try:
                lease_tokens = self._lease_client.acquire(
                    self._gen_id, self._gen_epoch, new_session_id, 0, {"active", "live"})
            except NelixError:
                self._ensure_handshake()
                if still_active:
                    with self._lock:
                        self._reserved -= 1
                if self._logger is not None:
                    self._logger.warning("manager", "restart_rejected",
                                         reason="lease_acquire_failed",
                                         session_id=session_id)
                return RestartOutcome("start_failed", lineage_id=lineage_id,
                                       restart_count=count, max_restarts=max_restarts)
            except LeaseClient.RouterUnavailable:
                if still_active:
                    with self._lock:
                        self._reserved -= 1
                if self._logger is not None:
                    self._logger.warning("manager", "restart_rejected",
                                         reason="lease_acquire_failed",
                                         session_id=session_id)
                return RestartOutcome("start_failed", lineage_id=lineage_id,
                                       restart_count=count, max_restarts=max_restarts)
        # _spawn OWNS the reservation (reserve=reserve): it consumes it atomically with the insert,
        # or releases it in its own finally if it raises before inserting. restart() must NOT also
        # touch self._reserved here (that would double-decrement).
        try:
            # Re-apply the per-session model override (same-lineage recovery must not silently drop
            # it). _spawn re-validates + re-folds against the fresh original spec (idempotent).
            started = self._spawn(executor, task, cwd, lineage_id=lineage_id,
                                  restarted_from=session_id, reserve=reserve, model=model,
                                  owner_id=stored_owner,
                                  session_id=new_session_id,
                                  lease_tokens=lease_tokens)
            new_sid, base_seq = started.session_id, started.base_seq
        except Exception as e:
            # FIX B3: release acquired tokens on _spawn failure (mirrors _lease_start).
            if lease_tokens is not None:
                self._release_lease_tokens(lease_tokens)
            self._log_spawn_failure("restart_spawn_failed", session_id, e)
            return RestartOutcome("start_failed", lineage_id=lineage_id,
                                  restart_count=count, max_restarts=max_restarts)
        if self._logger is not None:
            self._logger.info("manager", "session_restarted", session_id=new_sid,
                              restarted_from=session_id, lineage_id=lineage_id,
                              restart_count=count)
        return RestartOutcome("restarted", session_id=new_sid, lineage_id=lineage_id,
                              restart_count=count, max_restarts=max_restarts,
                              next_after_seq=base_seq, snapshot=started.snapshot)

    def _active_count(self):
        # MUST hold self._lock. Sessions occupying an ACTIVE concurrency slot: every live session
        # EXCEPT an `idle` one (turn complete, alive, awaiting a follow-up — it holds a PTY but not
        # an active slot). busy / awaiting_user / intervention_required / starting all still count:
        # the rule is exclude-idle, NOT a positive busy-only allowlist (which would wrongly free the
        # slot for a stuck/blocked/starting session that still owns a real process).
        return sum(1 for s in self._sessions.values()
                   if s.snapshot().get("control_state") != "idle")

    def _idle_count(self):
        # MUST hold self._lock. Retained `idle` sessions (completed, alive), bounded by idle_retained_limit.
        return sum(1 for s in self._sessions.values()
                   if s.snapshot().get("control_state") == "idle")

    def _persist_terminal_for_publish(self, session_id, terminal_kind, screen_excerpt):
        """S5a persist-before-visible-wake: persist terminal record BEFORE the event ring
        publishes. Called from session._finish_publish before _publish().

        Does NOT depend on _sessions membership (works even if the session was already
        removed for restart). Uses the passed kind+excerpt directly, not a session lookup.

        Discharges the terminal obligation, releases BOTH leases (active+live), and moves
        the session to the terminal-pending-confirmation inventory (outside _sessions,
        consuming neither lease). Called exactly once per terminal — _free_slot does NOT
        re-persist.
        """
        # B4/E: reject terminal publication if epoch is already certified.
        # Fail-closed on store read error: treat unreadable as certified → reject.
        # Skip check if gen_epoch is empty (test-only paths without real generation).
        if self._gen_epoch:
            try:
                rs = self._store.get_epoch_retirement_state(self._gen_epoch)
            except Exception:
                raise RuntimeError(
                    f"epoch {self._gen_epoch} store read failed; "
                    f"terminal publication rejected (fail-closed)")
            if rs == "certified":
                raise RuntimeError(
                    f"epoch {self._gen_epoch} is certified; "
                    f"terminal publication rejected (invariant violation)")

        ended_at = self._clock()
        try:
            self._store.put_terminal(
                session_id,
                terminal_kind=terminal_kind,
                summary=screen_excerpt,
                ended_at=ended_at)
        except Exception as _pe:
            if str(getattr(_pe, "code", "")) != "unknown_session":
                raise

        with self._lock:
            self._terminal_obligations.discard(session_id)
            active_tid = self._active_lease_tokens.pop(session_id, None)
            live_tid = self._live_lease_tokens.pop(session_id, None)
            # C2: record pending entry with the terminal_seq read right after persist.
            pending_seq = None
            owner_id = ""
            try:
                row = self._store._conn.execute(
                    "SELECT t.terminal_seq, st.owner_id FROM terminal t "
                    "JOIN starts st ON st.session_id = t.session_id "
                    "WHERE t.session_id=?", (session_id,)).fetchone()
                if row:
                    pending_seq = row["terminal_seq"]
                    owner_id = row["owner_id"]
            except Exception:
                pass
            self._terminal_pending[session_id] = {
                "terminal_kind": terminal_kind,
                "terminal_seq": pending_seq,
                "owner_id": owner_id,
                "epoch": self._gen_epoch,
            }

        if active_tid is not None or live_tid is not None:
            self._release_terminal_leases(active_tid, live_tid)

    def _free_slot(self, session_id):
        with self._lock:
            sess = self._sessions.get(session_id)
            snap = None
            qid = None
            if sess is not None:
                try:
                    snap = sess.terminal_snapshot()
                except Exception:
                    snap = None
                try:
                    qid = sess.pending_async_id()
                except Exception:
                    qid = None
                if qid is not None:
                    try:
                        sess.resolve_async_question(qid, None)
                    except Exception:
                        pass
            # S5a: leases were ALREADY released in _persist_terminal_for_publish.
            # Only collect tokens that weren't already released (e.g. if persist
            # callback was never invoked — terminal without a store, or test path).
            already_released = session_id not in self._active_lease_tokens
            if not already_released:
                active_tid = self._active_lease_tokens.pop(session_id, None)
                live_tid = self._live_lease_tokens.pop(session_id, None)
            else:
                active_tid = None
                live_tid = None
            if snap is not None and snap.get("terminal_kind") == "done":
                self._lineages.pop(snap.get("lineage_id"), None)
            # A3: persist was ALREADY done in _persist_terminal_for_publish BEFORE the
            # ring event. Only fall back to re-persist if the callback never ran
            # (session not in _terminal_pending, e.g. test-only path without the S5
            # callback). Use the SAME clock value so idempotency works.
            # E: never persist after certified — would invalidate final_high_water.
            # Check CERTIFIED state DIRECTLY (fail-closed on store error), independent
            # of the cached _quiescent flag which may be stale-false.
            if snap is not None and session_id not in self._terminal_pending:
                _certified = self._store_epoch_is_certified()
                if not _certified:
                    try:
                        self._store.put_terminal(
                            session_id,
                            terminal_kind=snap.get("terminal_kind", "unknown"),
                            summary=snap.get("screen_excerpt", ""),
                            ended_at=self._clock())
                    except Exception as _pe:
                        if str(getattr(_pe, "code", "")) != "unknown_session":
                            raise
            if self._terminal_ttl > 0:
                expires_at = self._clock() + self._terminal_ttl
                if snap is not None:
                    self._terminal[session_id] = (snap, expires_at, False)
                if qid is not None:
                    self._terminal_async[session_id] = (
                        {"id": qid, "reason": "executor_finished"}, expires_at)
            existed = self._sessions.pop(session_id, None) is not None
        if active_tid is not None or live_tid is not None:
            self._release_terminal_leases(active_tid, live_tid)
        if existed and self._logger is not None:
            self._logger.info("manager", "slot_freed", session_id=session_id)

    def _release_terminal_leases(self, active_tid, live_tid):
        """Release active + live lease tokens on terminal. Best-effort."""
        if self._lease_client is None:
            return
        if active_tid is not None:
            try:
                self._lease_client.release(active_tid)
            except Exception:
                if self._logger is not None:
                    self._logger.warning("manager", "lease_release_error",
                                         token_id=active_tid, exc_info=True)
        if live_tid is not None:
            try:
                self._lease_client.release(live_tid)
            except Exception:
                if self._logger is not None:
                    self._logger.warning("manager", "lease_release_error",
                                         token_id=live_tid, exc_info=True)
        # FIX 4: retry outbox after release.
        self.retry_lease_outbox()

    # ── S3b: reconciliation-id tracking ────────────────────────────────────

    def _router_reconciliation_id(self):
        """Current reconciliation id from the lease client, or None if unknown."""
        if self._lease_client is None:
            return None
        return self._lease_client.reconciliation_id

    def _lease_snapshot(self):
        """Build a snapshot of all held lease tokens for registration.

        Captured atomically under manager._lock. Each token carries the REAL
        activation_id from when it was acquired (stored in the parallel
        activation dicts), not the session's current activation counter.
        """
        with self._lock:
            active_tokens = []
            live_tokens = []
            for sid, tid in self._active_lease_tokens.items():
                act_id = self._active_token_activation.get(sid, "0")
                active_tokens.append({
                    "token_id": tid,
                    "key": (self._gen_id, self._gen_epoch, sid, act_id, "active"),
                })
            for sid, tid in self._live_lease_tokens.items():
                act_id = self._live_token_activation.get(sid, "0")
                live_tokens.append({
                    "token_id": tid,
                    "key": (self._gen_id, self._gen_epoch, sid, act_id, "live"),
                })
        return active_tokens, live_tokens

    def _ensure_handshake(self):
        """If the router has a new reconciliation id, adopt it and register.

        Called when a lease operation returns REBUILDING or STALE_RECONCILIATION_ID.
        In-flight guard + CAS ensures exactly ONE thread registers per id.
        Captures the id AT snapshot time and threads it into register_snapshot
        so the sent id == the marked id (not self._lease_client.reconciliation_id,
        which may have advanced).
        """
        if self._lease_client is None:
            return False
        target_rid = self._lease_client.reconciliation_id
        if target_rid is None:
            return False
        if not self._lease_client.needs_handshake():
            return False
        with self._handshake_lock:
            if self._handshake_in_flight == target_rid:
                return False
            self._handshake_in_flight = target_rid
        try:
            if not self._lease_client.needs_handshake():
                return False
            if self._logger is not None:
                self._logger.info("manager", "lease_handshake_start", rid=target_rid)
            active_tokens, live_tokens = self._lease_snapshot()
            # FIX 2: pass captured target_rid explicitly, not current client id.
            result = self._lease_client.register_snapshot(
                self._gen_id, self._gen_epoch,
                active_tokens, live_tokens, target_rid)
            if result is not None:
                self._lease_client.mark_handshook(target_rid)
                self._lease_client.retry_outbox()
                return True
            return False
        except (NelixError, LeaseClient.RouterUnavailable) as e:
            if self._logger is not None:
                self._logger.warning("manager", "lease_handshake_failed",
                                     rid=target_rid, error=str(e))
            return False
        finally:
            with self._handshake_lock:
                if self._handshake_in_flight == target_rid:
                    self._handshake_in_flight = None

    def retry_lease_outbox(self):
        """Retry all pending outbox releases. Returns number still pending."""
        if self._lease_client is None:
            return 0
        pending = self._lease_client.retry_outbox()
        return len(pending)

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def record_async_question(self, session_id, q):
        """Manager-level entry for the message-plane `question` route (Task 5): look up the LIVE
        session and delegate to Session.record_async_question. An absent/already-freed session
        returns the same unknown_session-equivalent shape as the in-Session already-pending error
        ((None, {"error": ...})), so the route can map either failure to an HTTP error the same way."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            return None, {"error": "unknown_session"}
        return sess.record_async_question(q)

    def append_progress_note(self, session_id, note):
        """Manager-level entry for the message-plane `note` route (Task 5): look up the LIVE session
        and delegate to Session.append_progress_note. An absent/already-freed session returns None
        (no progress_seq to report)."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            return None
        return sess.append_progress_note(note)

    def screen(self, session_id, *, owner_id, raw=False, force=False):
        sess = self._owned(session_id, owner_id)   # a non-owner reads another harness's TERMINAL
        if sess is None:
            return {"error": "unknown session"}
        # While the agent is actively working, withhold the screen (poll bait) unless explicitly
        # forced — the wake's screen_excerpt is the ground truth between events. `raw` only selects
        # cleaned-vs-raw formatting; it must NOT be an escape hatch around withholding (only force is).
        if sess.is_working() and not force:
            return {"control_state": "busy", "pending": False,
                    "message": ("Agent is still working. End your turn; nelix will wake you on the "
                                "next event. Pass force:true to see the screen anyway.")}
        # the external-output trust fence rides WITH the captured screen content (not the doorbell).
        return {"screen": sess.screen(raw=raw), "cols": sess._cols, "rows": sess._rows,
                "external_output_policy": EXTERNAL_OUTPUT_POLICY}

    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        # Ownership BEFORE anything else: answering another harness's decision is nelix-v96's class
        # at the harness boundary — the answer is typed into someone else's executor and cannot be
        # taken back. Resolved outside the lock (disk I/O), then re-checked against live state below.
        if not owner.owns_session(session_id, owner_id):
            return RespondOutcome("unknown_session")
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                # Terminal survival (Task 6): the session already exited (its slot freed by
                # _free_slot) but it had an OUTSTANDING async question when it did — terminal cleanup
                # auto-resolved that into self._terminal_async (same key + expiry as self._terminal).
                # A caller whose decision_id names THAT question gets a clean
                # not_delivered/executor_finished, never a bare unknown_session (which reads like a
                # typo'd/unrelated session id rather than "your question's answer arrived too late").
                entry = self._terminal_async.get(session_id)
                if (entry is not None and decision_id is not None
                        and entry[1] > self._clock() and entry[0].get("id") == decision_id):
                    return RespondOutcome("not_delivered", reason=entry[0].get("reason"))
                return RespondOutcome("unknown_session")
        # Async-reply id-dispatch (Task 4): a decision_id that names an OUTSTANDING ASYNC QUESTION (not
        # a blocking decision) is answered by delivering a FRESH user turn, NOT by typing into a modal
        # (the executor never paused — there is no modal). Correlation (mark_answered + clear the slot)
        # and delivery (the fresh-turn write) are separate: the session resolves + decides disposition,
        # the manager owns the slot-reacquiring write. This is checked BEFORE the idle branch because a
        # session can be idle AND hold a pending async question at once (asked, then finished the turn).
        if decision_id and sess.has_pending_async(decision_id):
            disposition, text = sess.resolve_async_question(decision_id, answer)
            if disposition == "deliver_now":
                out = self.send_turn(session_id, text)       # idle now -> re-acquire slot + fresh turn
                # resolve_async_question already cleared the slot + marked the event answered, so if
                # send_turn DECLINES WITHOUT typing (at_capacity: other sessions saturate the limit;
                # no_pending: the state flipped busy in this RPC->monitor window) the framed reply would
                # be lost and an orchestrator retry would deliver a BARE answer. Re-queue it (nothing
                # typed here) so the monitor re-delivers the full frame at the next idle — symmetric
                # with drain_async_reply's re-queue.
                if out.status in ("at_capacity", "no_pending", "admission_unavailable"):
                    sess.requeue_async_reply(text)
                return out
            if disposition == "queued_busy":
                # busy -> the monitor delivers at the next idle (drain_async_reply). Nothing typed yet.
                return RespondOutcome("queued", snapshot=sess.snapshot())
            return RespondOutcome("not_delivered", snapshot=sess.snapshot())  # closing/terminal
        # A follow-up on an IDLE session (turn complete, alive, no respondable decision) is a NEW
        # turn: route it through send_turn (re-acquire an active slot + re-open the turn) — never
        # respond(), whose no_pending path can't drive a non-respondable idle decision (plan Task 10).
        #
        # M4 (final whole-branch review, doc-only): this branch is reached — instead of the
        # decision_id-dispatch branch above — whenever `decision_id` is falsy or names no
        # outstanding async question, INCLUDING the case where the session is idle with a lone
        # outstanding async_question and the caller answered it WITHOUT a decision_id. Async answers
        # SHOULD pass decision_id (see the `question` tool
        # docs), but this is a deliberate choice, not an oversight: a strict guard here (rejecting a
        # decision_id-less idle answer) would also break legitimate idle follow-ups, which never
        # carry a decision_id. So a decision_id-less answer on an idle session is treated as a plain
        # follow-up turn — the async question is left untouched in its slot (still pending) and is
        # only ever auto-resolved if the session later goes terminal with it still outstanding
        # (Task 6 terminal survival -> reason="executor_finished"), never by this fall-through.
        if sess.snapshot().get("control_state") == "idle":
            return self.send_turn(session_id, answer)
        return sess.respond(answer, decision_id=decision_id)

    def send_turn(self, session_id, text):
        # Idle follow-up entry: RE-ACQUIRE an active slot before resuming. An idle session freed its
        # active slot, so resuming it must not push active+reserved past concurrency_limit; a capacity
        # refusal types nothing (mirrors start's honest cap). The reservation is held across the
        # (lockless) PTY write so a concurrent start cannot claim the same slot mid-resume.
        if self._lease_client is not None:
            return self._lease_send_turn(session_id, text)
        # B3: non-lease send_turn MUST check quiescence too.
        if self._check_quiescent():
            return RespondOutcome("no_pending")
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return RespondOutcome("unknown_session")
            # B3: re-check under lock.
            if self._quiescent:
                if self._logger is not None:
                    self._logger.warning("manager", "send_turn_rejected",
                                         reason="quiescing", session_id=session_id)
                return RespondOutcome("no_pending")
            if self._active_count() + self._reserved >= self._limit:
                if self._logger is not None:
                    self._logger.warning("manager", "send_turn_rejected",
                                         reason="concurrency_limit", session_id=session_id)
                return RespondOutcome("at_capacity")
            self._reserved += 1
        try:
            return sess.send_turn(text)
        finally:
            with self._lock:
                self._reserved -= 1

    def _lease_send_turn(self, session_id, text):
        """send_turn with router lease choreography (§3.3b).

        1. Under _lock: install pending-acquire marker.
        2. Drop lock, acquire active lease from router.
        3. Re-take lock: revalidate session state.
        4. Commit or release.
        """
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return RespondOutcome("unknown_session")
            if session_id in self._pending_acquire:
                # Another thread is mid-acquire for this session — refuse this one.
                if self._logger is not None:
                    self._logger.warning("manager", "send_turn_rejected",
                                         reason="concurrent_acquire",
                                         session_id=session_id)
                return RespondOutcome("at_capacity")
            # S5a: reject idle-resume if epoch is quiescing.
            if self._check_quiescent():
                if self._logger is not None:
                    self._logger.warning("manager", "send_turn_rejected",
                                         reason="quiescing", session_id=session_id)
                return RespondOutcome("no_pending")
            # Check session is idle (only idle sessions can receive a follow-up turn).
            cstate = sess.snapshot().get("control_state")
            if cstate != "idle":
                if self._logger is not None:
                    self._logger.warning("manager", "send_turn_rejected",
                                         reason="not_idle", session_id=session_id,
                                         state=cstate)
                return RespondOutcome("no_pending")
            self._pending_acquire.add(session_id)

        activation_id = getattr(sess, "_activation_counter", 0)
        # FIX 4: opportunistic outbox retry before admission.
        self.retry_lease_outbox()
        try:
            tokens = self._lease_client.acquire(
                self._gen_id, self._gen_epoch, session_id, activation_id, {"active"})
        except LeaseClient.RouterUnavailable:
            with self._lock:
                self._pending_acquire.discard(session_id)
            if self._logger is not None:
                self._logger.warning("manager", "send_turn_rejected",
                                     reason="admission_unavailable",
                                     session_id=session_id)
            return RespondOutcome("admission_unavailable")
        except NelixError as e:
            with self._lock:
                self._pending_acquire.discard(session_id)
            if e.code in (REBUILDING, STALE_RECONCILIATION_ID):
                self._ensure_handshake()
            if e.code in (ADMISSION_UNAVAILABLE, REBUILDING):
                return RespondOutcome("admission_unavailable")
            if e.code == STALE_RECONCILIATION_ID:
                return RespondOutcome("admission_unavailable")
            if e.code == CONCURRENCY_LIMIT:
                if self._logger is not None:
                    self._logger.warning("manager", "send_turn_rejected",
                                         reason="concurrency_limit",
                                         session_id=session_id)
                return RespondOutcome("at_capacity")
            if self._logger is not None:
                self._logger.warning("manager", "send_turn_rejected",
                                     reason=e.code, session_id=session_id)
            return RespondOutcome("admission_unavailable")

        # Revalidate under the lock. Keep pending_acquire set until after send_turn
        # completes so a racing thread cannot also acquire a lease for this session.
        # FIX D: stage tokens under lock, release RPCs outside lock.
        # D1: recheck _quiescent under _lock AFTER acquisition — if quiescing now,
        # RELEASE the acquired token and RETURN admission-rejected (do NOT fall
        # through to the commit/send block below).
        revalidate_ok = False
        active_tid = None
        active_info = tokens.get("active") or {}
        is_fresh = active_info.get("fresh", True)
        stale_tokens = []    # tokens to release outside the lock
        release_tokens = []  # tokens from rollback to release outside the lock
        with self._lock:
            # FIX 1: authoritative quiescence check AND token commit/reject under
            # the SAME _lock hold. NEVER set _quiescent=False in the admission path
            # — only begin_quiescence/explicit-reset transitions it False->True.
            # The resume path may set it True (and reject) but must never assign False.
            # UNKNOWN_SESSION (epoch not in store) is NOT fail-closed — test paths.
            if self._gen_epoch:
                try:
                    ep_row = self._store._conn.execute(
                        "SELECT process_state, retirement_state FROM epochs "
                        "WHERE generation_epoch=?", (self._gen_epoch,)).fetchone()
                    if ep_row is None:
                        self._quiescent = True
                    elif ep_row["process_state"] == "dead":
                        self._quiescent = True
                    elif ep_row["retirement_state"] in ("quiescing", "certified"):
                        self._quiescent = True
                except NelixError as e:
                    if e.code != UNKNOWN_SESSION:
                        self._quiescent = True
                except Exception:
                    self._quiescent = True
            # D1: if quiescing now, RELEASE the acquired token and REJECT —
            # do NOT fall through to commit/send.
            if self._quiescent:
                self._pending_acquire.discard(session_id)
                if is_fresh:
                    release_tokens = [active_info.get("token_id")] if active_info else []
            else:
                sess = self._sessions.get(session_id)
                if sess is None:
                    self._pending_acquire.discard(session_id)
                    if is_fresh:
                        release_tokens = [active_info.get("token_id")] if active_info else []
                else:
                    cstate = sess.snapshot().get("control_state")
                    if cstate != "idle":
                        self._pending_acquire.discard(session_id)
                        if is_fresh:
                            release_tokens = [active_info.get("token_id")] if active_info else []
                    else:
                        # Commit: store the new active token (replacing any stale one).
                        active_tid = active_info.get("token_id")
                        if active_tid:
                            old = self._active_lease_tokens.pop(session_id, None)
                            if old is not None:
                                stale_tokens.append(old)
                            self._active_lease_tokens[session_id] = active_tid
                            self._active_token_activation[session_id] = str(activation_id)
                        revalidate_ok = True

        # Release staged tokens outside the lock (FIX D).
        if release_tokens:
            self._release_lease_tokens_staged(release_tokens)
        for old_tid in stale_tokens:
            try:
                self._lease_client.release(old_tid)
            except Exception:
                if self._logger is not None:
                    self._logger.warning("manager", "lease_release_error",
                                         session_id=session_id, exc_info=True)

        if not revalidate_ok:
            return RespondOutcome("no_pending")

        try:
            return sess.send_turn(text)
        except Exception:
            if active_tid:
                try:
                    self._lease_client.release(active_tid)
                except Exception:
                    pass
                with self._lock:
                    self._active_lease_tokens.pop(session_id, None)
            raise
        finally:
            with self._lock:
                self._pending_acquire.discard(session_id)

    def status(self, session_id=None, *, owner_id, include_progress=False):
        """`include_progress` (Task 8): the explicit on-demand detail surface — merge
        `Session.progress_view()` into the returned snapshot(s) even during active-working, where
        `snapshot()` itself deliberately omits progress (anti-poll gate, session.py ~1260). Default
        False keeps today's behavior byte-for-byte: nothing merged, no snapshot() gate bypassed.

        OWNER-FILTERED on BOTH shapes. The session_id-less listing is THE board read this whole
        slice exists for: unfiltered it returns every session on the daemon, so the reading harness
        adopts every session it sees and arms a waiter for each. `recent_terminal` is filtered by
        the same rule and is not an afterthought — it is the terminal INVENTORY, and an unfiltered
        one leaks the other harness's task text and final state just as a live snapshot would.
        """
        if session_id is not None:
            sess = self._owned(session_id, owner_id)
            if sess is None:
                return {"error": "unknown session"}
            cursor = self._events.latest_seq(session_id)   # BEFORE snapshot: never arm past unseen
            snap = sess.snapshot()
            if include_progress:
                snap.update(sess.progress_view())
            snap["cursor"] = cursor
            return snap
        with self._lock:
            cursor = self._events.latest_seq()             # GLOBAL cursor (unchanged contract)
            per_seq = self._events.latest_seqs(self._sessions.keys())  # one _cv pass under manager._lock
            snapshot = dict(self._sessions)
            now = self._clock()
            # Sessions whose terminal snapshot has now expired are DEFINITIVELY gone: their final
            # event / terminal result is no longer observable (a harness away past the TTL has lost
            # it whether or not we retain the ring). This is the safe teardown seam at which to
            # release their event-ring retention + per-session bookkeeping (nelix-9a4.5 #4): doing it
            # at slot-free instead would discard spec §5's final wake before a waiter could read it.
            expired_terminals = [sid for sid, (snap, exp, _advertised) in self._terminal.items() if exp <= now]
            self._terminal = {sid: (snap, exp, advertised) for sid, (snap, exp, advertised) in self._terminal.items()
                              if exp > now}
            # Same expiry sweep, same policy, as self._terminal (Task 6): opportunistic purge here
            # keeps both stores from growing unbounded; respond()'s own lookup also re-checks
            # exp > now at read time, so a missed sweep is never a correctness issue, only bookkeeping.
            self._terminal_async = {sid: (rec, exp) for sid, (rec, exp) in self._terminal_async.items()
                                    if exp > now}
            # S2a.2: only advertised terminals appear in the daemon's live board. A terminal whose
            # record has been persisted to the store (advertised=False) is surfaced by the router's
            # archive read instead, preventing the resurrection bug on ack/expiry.
            recent_all = {sid: snap for sid, (snap, exp, advertised) in self._terminal.items()
                          if advertised}
            for sid in expired_terminals:
                # Guard against a same-id relive (restart mints a NEW sid, so this is belt-and-braces):
                # never forget a session that is live again. The event queue takes its own _cv here —
                # the manager._lock -> events._cv order already exists (latest_seqs above), so no new
                # lock ordering and no deadlock.
                if sid not in self._sessions:
                    self._events.forget_session(sid)
        # Ownership is decided OUTSIDE the lock (it is disk I/O; holding manager._lock across N
        # reads would stall every session's monitor). Safe: a session's owner never changes, so a
        # verdict cannot go stale — the worst a concurrent start can do is not appear in this
        # listing, which is what a snapshot taken a moment earlier would have shown anyway.
        mine = self._owned_sids(snapshot, owner_id)
        sessions = {}
        for sid, s in snapshot.items():
            if sid not in mine:
                continue
            s_snap = s.snapshot()
            if include_progress:
                s_snap.update(s.progress_view())
            sessions[sid] = {**s_snap, "seq": per_seq.get(sid, 0)}
        recent = {sid: snap for sid, snap in recent_all.items()
                  if owner.owns_session(sid, owner_id)}
        return {"sessions": sessions,
                "limit": self._limit,
                "cursor": cursor,
                "recent_terminal": recent}

    def capabilities(self, session_id=None, *, owner_id):
        """spec §8 "Internal overlap contract": "An operation unavailable on an older session
        needs a stable `unsupported_by_generation` response OR per-session capabilities. A single
        global capabilities response from N is insufficient for operations targeting N-1."

        `session_id` given: the PER-SESSION form (the primary deliverable) — owner-gated exactly
        like `status()` (`_owned`, i.e. `owner.owns_session`), so a non-owner or unknown id gets
        the same `None` sentinel `status()`'s per-session branch would treat as "unknown session"
        (rpc_server maps that to the stable error envelope, code `unknown_session`). Sourced from
        REAL per-session facts: the executor name, the driver's `hook_capable`, and the launcher's
        `ExecutorCapabilities` — never from a second generation, because there is only one.

        `session_id` omitted: the generation-level BASELINE — protocol version (stamped by
        rpc_server, same as /status) and the generation's configured executors with their static
        capabilities. Deliberately minimal (see the brief): a global response cannot answer a
        per-session question (that is the whole §8 point), so it stays a convenience, not the
        primary surface.

        Review correction: the per-session FACTS returned by `_session_capabilities` (executor,
        hook_capable, isolation_class, can_attach) ARE this generation's delivered "OR per-session
        capabilities" half of §8 — with only one generation running, a stable
        `unsupported_by_generation` RESPONSE CODE would have nothing real to be cross-generation
        about, so it is deferred to Plan 4 (multi-generation lifecycle), not fabricated here.
        """
        if session_id is not None:
            sess = self._owned(session_id, owner_id)
            if sess is None:
                return None
            return _session_capabilities(session_id, sess)
        return self._generation_capabilities()

    def _generation_capabilities(self):
        executors = {}
        for name, spec in self._specs.items():
            driver = self._driver_factory(spec.driver)
            caps = self._launcher_factory(spec.launcher).capabilities
            executors[name] = {
                "driver": spec.driver,
                "launcher": spec.launcher,
                "hook_capable": bool(getattr(driver, "hook_capable", False)),
                "isolation_class": caps.isolation_class,
                "can_attach": caps.can_attach,
            }
        return {"executors": executors}

    def stop(self, session_id, *, owner_id, reason="user_stop"):
        """The CALLER-facing stop. Owner-gated: killing another harness's session is destructive
        and irreversible, so a non-owner gets unknown_session and the session lives."""
        if not owner.owns_session(session_id, owner_id):
            return StopOutcome("unknown_session")
        return self._stop(session_id, reason=reason)

    def _stop(self, session_id, reason="user_stop"):
        """The INTERNAL stop, with no owner gate. Reachable only from stop() (which has already
        established ownership) and stop_all() (daemon shutdown, which owns everything by
        definition). Kept separate so shutdown cannot be expressed as "stop as some owner", which
        would need a wildcard owner — and a wildcard is the one thing that must not exist."""
        with self._lock:
            sess = self._sessions.get(session_id)   # look up only; DO NOT pop here
        if sess is None:
            return StopOutcome("unknown_session")
        # Release the lock before sess.stop(): it joins the monitor, whose finalization re-enters
        # manager._lock via _free_slot to capture the terminal snapshot into self._terminal.
        sess.stop()
        with self._lock:
            entry = self._terminal.get(session_id)
        snap = entry[0] if entry is not None else None
        if snap is not None and snap.get("terminal_kind") == "stopped":
            status = "stopped"                       # Invariant B: confirmed terminal
        else:
            status = "stop_requested"                # teardown not confirmed within the bounded join
            snap = {**(snap or {}), "session_id": session_id,
                    "control_state": "stopping", "pending": False}
        if self._logger is not None:
            self._logger.info("manager", "session_stopped", session_id=session_id,
                              reason=reason, status=status)
        return StopOutcome(status, snapshot=snap)

    # ── S5a: quiescence / retirement support ────────────────────────────────

    def begin_quiescence(self):
        """Begin quiescence for this epoch.
        D1: sets local _quiescent flag UNDER _lock ATOMICALLY with the store write,
        so a _spawn already holding _lock cannot observe False and admit."""
        with self._lock:
            self._store.set_epoch_retirement(
                self._gen_epoch, retirement_state="quiescing")
            self._quiescent = True
        if self._logger is not None:
            self._logger.info("manager", "quiescence_started",
                              epoch=self._gen_epoch)

    def _drop_confirmed_pending(self):
        """Drop terminal-pending entries whose terminal_seq <= confirmed_high_water
        for their epoch (the router has confirmed board-visibility).
        F6: iterate _terminal_pending UNDER _lock. A pending entry whose terminal_seq
        is None STAYS pending (never silently dropped — seq not yet resolved)."""
        with self._lock:
            expired = []
            for sid, info in self._terminal_pending.items():
                tseq = info.get("terminal_seq")
                epoch = info.get("epoch")
                if tseq is None:
                    continue
                if not epoch:
                    expired.append(sid)
                    continue
                try:
                    chw = self._store.get_generation_confirmed_high_water(epoch)
                    if chw >= tseq:
                        expired.append(sid)
                except Exception:
                    pass
            for sid in expired:
                self._terminal_pending.pop(sid, None)

    def quiescence_status(self):
        """Return a snapshot of quiescence-relevant counters.
        D4: reports NOT-quiesced while _pending_acquire non-empty or _reserved > 0."""
        self._drop_confirmed_pending()
        with self._lock:
            pending = len(self._pending_acquire)
            reserved = self._reserved
            in_flight = pending > 0 or reserved > 0
            return {
                "live_sessions": len(self._sessions),
                "outstanding_obligations": len(self._terminal_obligations),
                "terminal_pending": len(self._terminal_pending),
                "in_flight_admissions": pending + reserved,
                "_pending_acquire": pending,
                "_reserved": reserved,
                "quiescent": self._quiescent and not in_flight,
            }

    def certify_epoch(self, expected_epoch, certificate):
        """Certify this epoch after quiescence completes.
        A1: takes EXPECTED generation_epoch, verifies equals manager._gen_epoch
        (rejects a different epoch). Under ONE synchronized boundary (manager._lock):
          (a) checks the FULL barrier — zero obligations, no live PTYs,
              _pending_acquire empty, _reserved==0, all staged writes committed;
          (b) reads final persisted high-water;
          (c) sets retirement_state=certified + final_high_water.
        If the barrier is not met → raises RuntimeError (caller reports blocked).
        Returns the final_high_water and certificate on success.
        """
        if expected_epoch != self._gen_epoch:
            raise RuntimeError(
                f"expected epoch {expected_epoch!r} does not match "
                f"manager epoch {self._gen_epoch!r}")

        with self._lock:
            if self._terminal_obligations:
                raise RuntimeError(
                    f"cannot certify: {len(self._terminal_obligations)} "
                    f"outstanding terminal obligations")
            if self._sessions:
                raise RuntimeError(
                    f"cannot certify: {len(self._sessions)} live sessions")
            if self._pending_acquire:
                raise RuntimeError(
                    f"cannot certify: {len(self._pending_acquire)} "
                    f"in-flight pending acquires")
            if self._reserved:
                raise RuntimeError(
                    f"cannot certify: {self._reserved} reserved slots")
            final_hw = self._store.get_generation_persisted_high_water(
                self._gen_epoch)
            self._store.set_epoch_retirement(
                self._gen_epoch,
                retirement_state="certified",
                certificate=certificate,
                final_high_water=final_hw)
            self._quiescent = True

        if self._logger is not None:
            self._logger.info("manager", "epoch_certified",
                              epoch=self._gen_epoch,
                              final_high_water=final_hw,
                              certificate=certificate)
        return {"final_high_water": final_hw, "certificate": certificate}

    def stop_all(self, reason="shutdown"):
        # Daemon shutdown: every session goes, whoever owns it. _stop, not stop — see _stop.
        with self._lock:
            sids = list(self._sessions)
        for sid in sids:
            self._stop(sid, reason=reason)
