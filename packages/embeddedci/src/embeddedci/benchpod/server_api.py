"""HTTP client for the embeddedci **server orchestration API** — the plane the web UI drives.

Distinct from the device transports (which talk to the pod directly): this client calls the
server's own endpoints for things that live server-side, above all the **cloud waveform library**
(S3-backed recordings) and the server's DAC replay (``/dac/replay/start``). It is also a fallback
path for server-orchestrated captures (``/capture/start`` + ``/capture/{id}``).

Auth. These endpoints require a real user / API key (scope ``benchpod:control``) — **a GitHub
Actions OIDC token cannot reach them** (its subject is the repo, not a user; the server's
``requireUser``/``requireBenchpodHTTPUser`` reject it). So the library needs an API key
(``eci_…``) even when the device itself is driven over OIDC. Pass ``api_key`` (or set
``BENCHPOD_API_KEY``); a bearer ``token_provider`` is accepted for real-user/cloud-session tokens.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .cloud_auth import DEFAULT_API_BASE, USER_AGENT
from .errors import BenchPodError, CloudAuthError, TransportError

_HTTP_TIMEOUT = 60.0


class ServerApiError(BenchPodError):
    """An embeddedci server API call failed. Carries the HTTP ``status`` when there was one."""

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        self.status = status
        super().__init__(message)


class ServerApi:
    """Thin client for the embeddedci server HTTP API (``{api_base}/api/...``)."""

    def __init__(
        self,
        *,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        token_provider: Optional[Callable[[], str]] = None,
        timeout: float = _HTTP_TIMEOUT,
        lease_id: Optional[str] = None,
    ) -> None:
        if not api_key and token_provider is None:
            raise CloudAuthError(
                "the embeddedci server API needs an API key: pass api_key='eci_…' or set "
                "BENCHPOD_API_KEY. A GitHub OIDC token cannot reach the waveform-library / "
                "capture / replay endpoints (they require a user or API-key credential)."
            )
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.api_key = api_key
        self._token_provider = token_provider
        self.timeout = timeout
        self.lease_id = lease_id

    # -- auth + request -----------------------------------------------------

    def _auth_header(self) -> str:
        if self.api_key:
            return f"ApiKey {self.api_key}"
        return f"Bearer {self._token_provider()}"  # type: ignore[misc]

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        raw_body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        parse_json: bool = True,
    ) -> Tuple[int, Any]:
        """Issue one request. Returns ``(status, parsed)``; raises :class:`ServerApiError` on 4xx/5xx."""
        url = self.api_base + "/api" + path
        if query:
            q = {k: v for k, v in query.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        body: Optional[bytes] = raw_body
        headers = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        if self.lease_id:
            headers["X-Benchpod-Lease"] = self.lease_id
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not parse_json:
                    return resp.status, raw
                text = raw.decode("utf-8", "replace").strip()
                return resp.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except Exception:
                pass
            hint = ""
            if exc.code in (401, 403):
                hint = (" — this endpoint needs a user/API-key credential with the "
                        "'benchpod:control' scope; a GitHub OIDC token will not authorize it")
            raise ServerApiError(
                f"{method} {path} failed (HTTP {exc.code}): {detail}{hint}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"{method} {path} failed: {exc}") from exc

    # -- devices ------------------------------------------------------------

    def list_devices(self) -> List[Dict[str, Any]]:
        _, data = self.request("GET", "/benchpod/devices")
        return list((data or {}).get("devices") or [])

    def resolve_device_id(self, name: str) -> str:
        """Resolve a device *name* to its server id (raises if not found)."""
        for d in self.list_devices():
            if d.get("name") == name or d.get("id") == name:
                return str(d["id"])
        raise ServerApiError(f"device {name!r} not found in this organization")

    def device_parameters(self, name_or_id: str) -> Dict[str, Any]:
        """Return the ``cap.*`` parameter map the server holds for a device."""
        for d in self.list_devices():
            if d.get("name") == name_or_id or d.get("id") == name_or_id:
                return dict(d.get("parameters") or {})
        return {}

    # -- server-orchestrated capture (fallback path) ------------------------

    def scope_capture_start(self, device_id: str, *, samples: int = 256,
                            sample_rate_mhz: float = 1.0) -> str:
        _, data = self.request("POST", "/scope/captures/start", json_body={
            "device_id": device_id, "samples": samples, "sample_rate_mhz": sample_rate_mhz,
        })
        cid = (data or {}).get("capture_id")
        if not cid:
            raise ServerApiError("scope capture start returned no capture_id")
        return str(cid)

    def dual_capture_start(self, device_id: str, *, adc_samples: int, adc_rate_mhz: float,
                           la_samples: int, la_rate_mhz: float) -> str:
        _, data = self.request("POST", "/capture/start", json_body={
            "device_id": device_id, "adc_samples": adc_samples, "adc_rate_mhz": adc_rate_mhz,
            "la_samples": la_samples, "la_rate_mhz": la_rate_mhz,
        })
        cid = (data or {}).get("capture_id")
        if not cid:
            raise ServerApiError("capture start returned no capture_id")
        return str(cid)

    def capture_snapshot(self, capture_id: str) -> Dict[str, Any]:
        _, data = self.request("GET", f"/capture/{capture_id}")
        return data or {}

    # -- DAC replay (server-side DSP) ---------------------------------------

    def replay_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST ``/dac/replay/start`` (server windows/downsamples/maps + arms the replay)."""
        _, data = self.request("POST", "/dac/replay/start", json_body=payload)
        return data or {}
