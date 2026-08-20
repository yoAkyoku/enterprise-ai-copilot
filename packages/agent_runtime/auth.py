"""JWT authentication boundaries for self-hosted and OIDC deployments."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import IdentityContext
from .network import validated_https_endpoint


class AuthenticationError(ValueError):
    """Raised when a bearer token cannot establish a trusted identity."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not follow discovery or JWKS redirects across trust boundaries."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _decode_segment(segment: str) -> bytes:
    try:
        padding = "=" * (-len(segment) % 4)
        return base64.urlsafe_b64decode((segment + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise AuthenticationError("malformed token encoding") from exc


def _json_segment(segment: str) -> dict[str, Any]:
    try:
        value = json.loads(_decode_segment(segment))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("malformed token JSON") from exc
    if not isinstance(value, dict):
        raise AuthenticationError("token segment must be a JSON object")
    return value


def _identity_from_claims(
    claims: dict[str, Any],
    *,
    issuer: str | None = None,
    audience: str | None = None,
    workspace_claim: str = "workspace_id",
    tenant_claim: str = "tenant_id",
    role_claim: str = "role",
) -> IdentityContext:
    if issuer is not None and claims.get("iss") != issuer:
        raise AuthenticationError("token issuer is not trusted")
    if audience is not None:
        token_audience = claims.get("aud")
        if token_audience != audience and not (
            isinstance(token_audience, list) and audience in token_audience
        ):
            raise AuthenticationError("token audience is not trusted")
    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or now >= exp:
        raise AuthenticationError("token is expired or has no expiry")
    nbf = claims.get("nbf")
    if nbf is not None and (not isinstance(nbf, (int, float)) or now < nbf):
        raise AuthenticationError("token is not active")
    return IdentityContext(
        user_id=_claim(claims, "sub"),
        workspace_id=_claim(claims, workspace_claim),
        tenant_id=_claim(claims, tenant_claim),
        role=_claim(claims, role_claim),
    )


def _claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise AuthenticationError(f"token claim {name} is required")
    return value.strip()


class JwtHs256Authenticator:
    """Validate a short-lived HS256 token and inject identity claims.

    This is intentionally a narrow self-hosted option. Deployments that use
    an external OIDC provider can replace this boundary while preserving the
    same ``IdentityContext`` contract.
    """

    def __init__(
        self, secret: str, *, issuer: str | None = None, audience: str | None = None
    ) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("JWT secret must contain at least 32 UTF-8 bytes")
        self._secret = secret.encode("utf-8")
        self.issuer = issuer
        self.audience = audience

    def authenticate(self, authorization: str | None) -> IdentityContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("Bearer authentication is required")
        token = authorization[7:]
        parts = token.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            raise AuthenticationError("malformed bearer token")
        header = _json_segment(parts[0])
        if header.get("alg") != "HS256" or header.get("typ") not in {None, "JWT"}:
            raise AuthenticationError("only HS256 JWT tokens are accepted")
        try:
            signed = f"{parts[0]}.{parts[1]}".encode("ascii", errors="strict")
        except UnicodeEncodeError as exc:
            raise AuthenticationError("malformed bearer token") from exc
        expected = hmac.new(self._secret, signed, "sha256").digest()
        actual = _decode_segment(parts[2])
        if not hmac.compare_digest(actual, expected):
            raise AuthenticationError("invalid bearer signature")
        claims = _json_segment(parts[1])
        return _identity_from_claims(claims, issuer=self.issuer, audience=self.audience)


class JwtJwksAuthenticator:
    """Validate RS256 bearer tokens against an allowlisted OIDC JWKS endpoint.

    Discovery and key retrieval are bounded, HTTPS-only, redirect-free and
    cached. A new ``kid`` forces one refresh so normal signing-key rotation is
    supported without allowing a token to choose an arbitrary key URL.
    """

    def __init__(
        self,
        issuer: str,
        *,
        audience: str | None = None,
        allowed_hosts: list[str] | tuple[str, ...],
        jwks_uri: str | None = None,
        timeout_seconds: float = 5.0,
        cache_seconds: int = 300,
        workspace_claim: str = "workspace_id",
        tenant_claim: str = "tenant_id",
        role_claim: str = "role",
        jwks_fetcher: Any | None = None,
    ) -> None:
        parsed_issuer = urllib.parse.urlparse(issuer)
        host = (parsed_issuer.hostname or "").lower()
        approved_hosts = {item.strip().lower() for item in allowed_hosts if item.strip()}
        if parsed_issuer.scheme != "https" or not host or host not in approved_hosts:
            raise ValueError("OIDC issuer must be HTTPS and match an approved host")
        if (
            parsed_issuer.username
            or parsed_issuer.password
            or parsed_issuer.query
            or parsed_issuer.fragment
        ):
            raise ValueError("OIDC issuer must not embed credentials or query values")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("OIDC timeout must be between 0 and 30 seconds")
        if cache_seconds <= 0 or cache_seconds > 86400:
            raise ValueError("OIDC JWKS cache must be between 1 and 86400 seconds")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.workspace_claim = workspace_claim
        self.tenant_claim = tenant_claim
        self.role_claim = role_claim
        self._approved_hosts = approved_hosts
        self._jwks_uri = self._validate_endpoint(jwks_uri) if jwks_uri else None
        self._jwks_fetcher = jwks_fetcher
        self._keys: dict[str, dict[str, Any]] = {}
        self._cache_expires_at = 0.0
        self._lock = threading.RLock()

    def authenticate(self, authorization: str | None) -> IdentityContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("Bearer authentication is required")
        token = authorization[7:]
        parts = token.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            raise AuthenticationError("malformed bearer token")
        header = _json_segment(parts[0])
        if header.get("alg") != "RS256" or header.get("typ") not in {None, "JWT"}:
            raise AuthenticationError("only RS256 JWT tokens are accepted")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise AuthenticationError("token key id is required")
        try:
            signed = f"{parts[0]}.{parts[1]}".encode("ascii", errors="strict")
        except UnicodeEncodeError as exc:
            raise AuthenticationError("malformed bearer token") from exc
        key = self._key_for(kid)
        signature = _decode_segment(parts[2])
        self._verify_signature(key, signed, signature)
        claims = _json_segment(parts[1])
        return _identity_from_claims(
            claims,
            issuer=self.issuer,
            audience=self.audience,
            workspace_claim=self.workspace_claim,
            tenant_claim=self.tenant_claim,
            role_claim=self.role_claim,
        )

    def _key_for(self, kid: str) -> dict[str, Any]:
        with self._lock:
            if time.time() >= self._cache_expires_at:
                self._refresh_keys()
            key = self._keys.get(kid)
            if key is None:
                self._refresh_keys()
                key = self._keys.get(kid)
            if key is None:
                raise AuthenticationError("token signing key is not trusted")
            return key

    def _refresh_keys(self) -> None:
        body = (
            self._fetch_json(self._jwks_uri or self._discovery_jwks_uri())
            if self._jwks_fetcher is None
            else self._jwks_fetcher()
        )
        if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
            raise AuthenticationError("OIDC JWKS response is invalid")
        keys: dict[str, dict[str, Any]] = {}
        for item in body["keys"]:
            if not isinstance(item, dict) or item.get("kty") != "RSA":
                continue
            key_id = item.get("kid")
            if isinstance(key_id, str) and key_id and item.get("alg", "RS256") == "RS256":
                keys[key_id] = item
        if not keys:
            raise AuthenticationError("OIDC JWKS contains no trusted RSA keys")
        self._keys = keys
        self._cache_expires_at = time.time() + self.cache_seconds

    def _discovery_jwks_uri(self) -> str:
        discovery_url = f"{self.issuer}/.well-known/openid-configuration"
        body = self._fetch_json(discovery_url)
        if body.get("issuer") != self.issuer:
            raise AuthenticationError("OIDC discovery issuer does not match configuration")
        jwks_uri = body.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise AuthenticationError("OIDC discovery has no JWKS URI")
        self._jwks_uri = self._validate_endpoint(jwks_uri)
        return self._jwks_uri

    def _validate_endpoint(self, endpoint: str) -> str:
        return validated_https_endpoint(
            endpoint, self._approved_hosts, label="OIDC", default_path="/"
        )

    def _fetch_json(self, endpoint: str) -> dict[str, Any]:
        request = urllib.request.Request(
            endpoint, headers={"Accept": "application/json"}, method="GET"
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler)
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(1024 * 1024 + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AuthenticationError("OIDC discovery or JWKS request failed") from exc
        if len(raw) > 1024 * 1024:
            raise AuthenticationError("OIDC discovery or JWKS response exceeded the limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthenticationError("OIDC discovery or JWKS returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AuthenticationError("OIDC discovery or JWKS response must be an object")
        return value

    @staticmethod
    def _verify_signature(jwk: dict[str, Any], signed: bytes, signature: bytes) -> None:
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:
            raise AuthenticationError(
                "cryptography is required for OIDC RS256 authentication"
            ) from exc
        try:
            modulus = int.from_bytes(_decode_segment(str(jwk["n"])), "big")
            exponent = int.from_bytes(_decode_segment(str(jwk["e"])), "big")
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except (KeyError, ValueError, TypeError) as exc:
            raise AuthenticationError("OIDC token signature is invalid") from exc
