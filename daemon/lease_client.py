"""Generation-to-router lease API client.

The daemon connects to the router's AF_UNIX socket (``NELIX_ROUTER_SOCK``) using the same
peercred auth the router uses for all same-host connections. The router's ``peer_is_self()``
check allows any same-uid caller, so no additional auth is needed.
"""
import json

from nelix_contracts.errors import NelixError


class LeaseClient:
    """HTTP client for the router's lease acquire/release endpoints.

    Raises ``RouterUnavailable`` when the router cannot be reached (connection refused,
    socket absent, etc.) — callers map this to ``admission_unavailable`` for retryable
    error propagation.
    """

    class RouterUnavailable(Exception):
        """The router's lease service could not be reached."""

    def __init__(self, router_sock_path: str, timeout=5):
        self._path = router_sock_path
        self._timeout = timeout

    def _call(self, endpoint, body):
        from rpc_client import UnixHTTPConnection
        import http.client
        try:
            conn = UnixHTTPConnection(self._path, timeout=self._timeout)
            try:
                conn.request(
                    "POST", endpoint,
                    body=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                data = json.loads(resp.read() or b"{}")
                return resp.status, data
            finally:
                conn.close()
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            raise self.RouterUnavailable(str(e)) from e
        except http.client.RemoteDisconnected as e:
            raise self.RouterUnavailable(str(e)) from e

    def acquire(self, generation_id, generation_epoch, session_id, activation_id, kinds):
        """Acquire leases from the router.

        ``kinds`` is a list of ``"active"`` and/or ``"live"``.

        Returns a dict mapping kind -> token string on success.

        Raises:
            LeaseClient.RouterUnavailable: router cannot be reached.
            NelixError: router returned an error envelope (CONCURRENCY_LIMIT,
                       ADMISSION_UNAVAILABLE, REBUILDING, etc.).
        """
        body = {
            "generation_id": generation_id,
            "generation_epoch": generation_epoch,
            "session_id": session_id,
            "activation_id": str(activation_id),
            "kinds": list(kinds),
        }
        status, data = self._call("/lease/acquire", body)
        if status == 200:
            tokens = data.get("tokens", {})
            if not tokens:
                raise self.RouterUnavailable(
                    "lease acquire returned 200 without tokens")
            return tokens
        err = data.get("error", {})
        code = err.get("code", "internal_error")
        msg = err.get("message", "lease acquire failed")
        raise NelixError(code, msg)

    def release(self, token_id):
        """Release a single lease token from the router.

        Raises:
            LeaseClient.RouterUnavailable: router cannot be reached.
        """
        body = {"token_id": token_id}
        status, data = self._call("/lease/release", body)
        if status != 200:
            err = data.get("error", {})
            code = err.get("code", "internal_error")
            msg = err.get("message", "lease release failed")
            raise NelixError(code, msg)
        return data.get("released", False)
