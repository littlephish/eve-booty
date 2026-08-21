"""EVE SSO: PKCE authorisation-code flow with a loopback callback.

A desktop app cannot keep a client secret, so PKCE is the flow to use. If a
secret is configured anyway (some registered apps require it) we fall back to
basic-auth token exchange.

Refresh tokens go into the OS credential store via keyring, never into the
SQLite database.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import secrets
import socket
import stat
import threading
import time
import urllib.parse
import warnings
import webbrowser
from dataclasses import dataclass

import httpx
import keyring
import keyring.backends.fail
from jose import jwt
from jose.exceptions import JWTError

from ..config import (
    APP_NAME,
    DATA_DIR,
    KNOWN_TAKEN_PORTS,
    SSO_AUDIENCE,
    SSO_ISSUERS,
    SSO_METADATA_URL,
    Settings,
    user_agent,
)

KEYRING_SERVICE = f"{APP_NAME}-refresh-token"

_metadata: dict | None = None
_metadata_at: float = 0.0
_jwks: dict | None = None
_METADATA_TTL = 3600.0


class AuthError(RuntimeError):
    pass


def _metadata_doc(settings: Settings) -> dict:
    global _metadata, _metadata_at
    if _metadata is None or time.time() - _metadata_at > _METADATA_TTL:
        r = httpx.get(SSO_METADATA_URL, headers={"User-Agent": user_agent(settings)}, timeout=30)
        r.raise_for_status()
        _metadata = r.json()
        _metadata_at = time.time()
    return _metadata


def _jwks_doc(settings: Settings) -> dict:
    global _jwks
    meta = _metadata_doc(settings)
    if _jwks is None:
        r = httpx.get(meta["jwks_uri"], headers={"User-Agent": user_agent(settings)}, timeout=30)
        r.raise_for_status()
        _jwks = r.json()
    return _jwks


def verify_token(access_token: str, settings: Settings) -> dict:
    """Validate signature, issuer, audience and expiry; return the claims."""
    keys = _jwks_doc(settings)["keys"]
    header = jwt.get_unverified_header(access_token)
    matching = [
        k for k in keys if k["kid"] == header["kid"] and k.get("alg", header["alg"]) == header["alg"]
    ]
    if not matching:
        raise AuthError("no matching JWKS key for token")
    try:
        claims = jwt.decode(
            access_token,
            key=matching[0],
            algorithms=[header["alg"]],
            issuer=list(SSO_ISSUERS),
            audience=SSO_AUDIENCE,
        )
    except JWTError as exc:
        raise AuthError(f"token failed validation: {exc}") from exc
    if settings.client_id and settings.client_id not in claims.get("aud", []):
        raise AuthError("token was issued for a different client_id")
    return claims


def character_id_from_claims(claims: dict) -> int:
    sub = claims.get("sub", "")
    # sub looks like "EVE:CHARACTER:2112625428"
    return int(sub.rsplit(":", 1)[-1])


# --------------------------------------------------------------- token store
# Refresh tokens go to the OS credential store: Windows Credential Manager,
# macOS Keychain, or Secret Service on Linux. A headless Linux box often has
# none of those, so rather than refusing to run we fall back to a 0600 file in
# the data directory and say so. That file is as sensitive as a password.
_FALLBACK_PATH = DATA_DIR / "tokens.json"
_fallback_warned = False


def _keyring_available() -> bool:
    try:
        return not isinstance(keyring.get_keyring(), keyring.backends.fail.Keyring)
    except Exception:  # noqa: BLE001
        return False


def using_fallback_store() -> bool:
    return not _keyring_available()


def _read_fallback() -> dict:
    if not _FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_fallback(data: dict) -> None:
    global _fallback_warned
    _FALLBACK_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(_FALLBACK_PATH, stat.S_IRUSR | stat.S_IWUSR)
    if not _fallback_warned:
        _fallback_warned = True
        warnings.warn(
            "No OS credential store found; refresh tokens are being written to "
            f"{_FALLBACK_PATH}. Treat that file as a password.",
            RuntimeWarning,
            stacklevel=2,
        )


def store_refresh_token(character_id: int, token: str) -> None:
    if _keyring_available():
        keyring.set_password(KEYRING_SERVICE, str(character_id), token)
        return
    data = _read_fallback()
    data[str(character_id)] = token
    _write_fallback(data)


def load_refresh_token(character_id: int) -> str | None:
    if _keyring_available():
        with contextlib.suppress(Exception):
            return keyring.get_password(KEYRING_SERVICE, str(character_id))
        return None
    return _read_fallback().get(str(character_id))


def delete_refresh_token(character_id: int) -> None:
    if _keyring_available():
        with contextlib.suppress(Exception):
            keyring.delete_password(KEYRING_SERVICE, str(character_id))
        return
    data = _read_fallback()
    if data.pop(str(character_id), None) is not None:
        _write_fallback(data)


# ------------------------------------------------------------------- pkce
def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    character_id: int
    character_name: str
    scopes: list[str]

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - 30


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in params.items()}
        body = (
            b"<html><body style='font-family:sans-serif;padding:3rem'>"
            b"<h2>Character linked.</h2><p>You can close this tab and return "
            b"to the app.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the stdlib access log
        pass


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def check_callback_port(settings: Settings) -> None:
    """Fail early and legibly if something else already owns the port.

    The callback URL is registered at CCP, so we cannot fall back to a
    different port mid-login. Better to say which app is probably squatting on
    it than to let bind() throw from inside the SSO flow.
    """
    port = settings.callback_port
    if port_is_free(port):
        return
    culprit = KNOWN_TAKEN_PORTS.get(port)
    hint = f" That port is {culprit}'s SSO callback." if culprit else ""
    raise AuthError(
        f"Port {port} is already in use, so the SSO callback cannot start.{hint} "
        "Close whatever is holding it, or pick another port in Settings and "
        "update the callback URL on your application at developers.eveonline.com "
        "to match."
    )


def login(settings: Settings, scopes: list[str], timeout: float = 300.0) -> TokenSet:
    """Open the browser, wait for the SSO callback, exchange for tokens."""
    if not settings.client_id:
        raise AuthError("No client_id configured. Set one in Settings first.")
    check_callback_port(settings)

    meta = _metadata_doc(settings)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    query = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(query)}"

    _CallbackHandler.result = {}
    server = http.server.HTTPServer(("127.0.0.1", settings.callback_port), _CallbackHandler)
    server.timeout = 1.0
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        webbrowser.open(url)
        deadline = time.time() + timeout
        while not _CallbackHandler.result and time.time() < deadline:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise AuthError(
            "Timed out waiting for the SSO callback. If the browser tab showed an "
            "error instead of a login page, EVE SSO likely rejected one of the "
            "requested scopes (invalid_scope) before it could redirect back here -- "
            "that happens pre-redirect, so our side never hears about it and just "
            "times out. Check the scope list in Settings against what your "
            "application actually has approved at developers.eveonline.com."
        )
    if "error" in result:
        raise AuthError(f"SSO returned an error: {result.get('error_description', result['error'])}")
    if result.get("state") != state:
        raise AuthError("State mismatch on SSO callback -- aborting.")

    return _exchange(settings, {
        "grant_type": "authorization_code",
        "code": result["code"],
        "client_id": settings.client_id,
        "code_verifier": verifier,
    })


def refresh(settings: Settings, refresh_token: str) -> TokenSet:
    return _exchange(settings, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.client_id,
    })


def _exchange(settings: Settings, payload: dict) -> TokenSet:
    meta = _metadata_doc(settings)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": user_agent(settings),
        "Host": urllib.parse.urlparse(meta["token_endpoint"]).netloc,
    }
    if settings.client_secret:
        basic = base64.urlsafe_b64encode(
            f"{settings.client_id}:{settings.client_secret}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {basic}"
        payload = {k: v for k, v in payload.items() if k not in ("client_id", "code_verifier")}

    r = httpx.post(meta["token_endpoint"], headers=headers, data=payload, timeout=30)
    if r.status_code >= 400:
        raise AuthError(f"Token endpoint returned {r.status_code}: {r.text[:300]}")
    data = r.json()

    claims = verify_token(data["access_token"], settings)
    scp = claims.get("scp") or []
    if isinstance(scp, str):
        scp = [scp]
    ts = TokenSet(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=time.time() + float(data.get("expires_in", 1200)),
        character_id=character_id_from_claims(claims),
        character_name=claims.get("name", ""),
        scopes=scp,
    )
    store_refresh_token(ts.character_id, ts.refresh_token)
    return ts


class TokenCache:
    """Keeps live access tokens in memory, refreshing from keyring as needed."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._tokens: dict[int, TokenSet] = {}
        self._lock = threading.Lock()

    def put(self, ts: TokenSet) -> None:
        with self._lock:
            self._tokens[ts.character_id] = ts

    def access_token(self, character_id: int) -> str:
        with self._lock:
            ts = self._tokens.get(character_id)
            if ts and not ts.expired:
                return ts.access_token
        rt = (ts.refresh_token if ts else None) or load_refresh_token(character_id)
        if not rt:
            raise AuthError(
                f"No stored refresh token for character {character_id}. Re-add the character."
            )
        fresh = refresh(self.settings, rt)
        self.put(fresh)
        return fresh.access_token

    def scopes(self, character_id: int) -> list[str]:
        with self._lock:
            ts = self._tokens.get(character_id)
        return ts.scopes if ts else []

    def forget(self, character_id: int) -> None:
        with self._lock:
            self._tokens.pop(character_id, None)
        delete_refresh_token(character_id)
