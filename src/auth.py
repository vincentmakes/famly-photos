"""Famly authentication via GraphQL mutation, matching the real Famly web app flow."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import requests

logger = logging.getLogger("famly.auth")

GRAPHQL_URL = "https://app.famly.co/graphql"

AUTHENTICATE_MUTATION = """
mutation Authenticate($email: EmailAddress!, $password: Password!, $deviceId: DeviceId, $legacy: Boolean) {
    me {
        authenticateWithPassword(
            email: $email
            password: $password
            deviceId: $deviceId
            legacy: $legacy
        ) {
            ... on AuthenticationSucceeded {
                accessToken
                deviceId
                __typename
            }
            ... on AuthenticationFailed {
                status
                errorDetails
                errorTitle
                __typename
            }
            ... on AuthenticationChallenged {
                loginId
                deviceId
                choices {
                    context { id __typename }
                    hmac
                    requiresTwoFactor
                    __typename
                }
                __typename
            }
            __typename
        }
        __typename
    }
}
"""


_DEVICE_ID_FILE = "device_id"


def _stable_device_id(data_dir: Path) -> str:
    """Load or generate a persistent device ID.

    Stored alongside the token so it survives container rebuilds.
    """
    path = data_dir / _DEVICE_ID_FILE
    if path.exists():
        device_id = path.read_text().strip()
        if device_id:
            return device_id
    device_id = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(device_id)
    return device_id


@dataclass
class TokenState:
    access_token: str = ""
    session_marker: str = ""
    installation_id: str = ""
    obtained_at: float = 0.0

    def is_valid(self) -> bool:
        return bool(self.access_token)


@dataclass
class FamlyAuth:
    """Manages Famly access tokens with auto-refresh on 401/403."""

    email: str
    password: str
    installation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    token_path: Path = Path("/data/token.json")
    static_token: str = ""

    _state: TokenState = field(default_factory=TokenState, init=False)
    _device_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self._device_id = _stable_device_id(self.token_path.parent)
        if self.static_token:
            self._state = TokenState(
                access_token=self.static_token,
                installation_id=self.installation_id,
                obtained_at=time.time(),
            )
            logger.info("Using static access token (no auto-refresh)")
        else:
            self._load_cached_token()

    # ── Public API ───────────────────────────────────────────────────────

    def get_session(self) -> requests.Session:
        if not self._state.is_valid():
            self._login()
        sess = requests.Session()
        sess.headers.update(self._build_headers())
        return sess

    def refresh(self) -> None:
        logger.info("Forcing token refresh via login")
        self._login()

    def handle_auth_error(self, response: requests.Response) -> bool:
        if response.status_code in (401, 403):
            logger.warning("Got %d – refreshing token", response.status_code)
            self._login()
            return True
        return False

    @property
    def token_age_hours(self) -> float:
        if self._state.obtained_at == 0:
            return -1.0
        return (time.time() - self._state.obtained_at) / 3600

    @property
    def access_token(self) -> str:
        return self._state.access_token

    # ── GraphQL helper (also used by fetcher) ────────────────────────────

    def graphql(
        self, operation: str, query: str, variables: dict
    ) -> dict:
        """Execute a GraphQL query/mutation against the Famly API."""
        resp = requests.post(
            f"{GRAPHQL_URL}?{operation}",
            json={
                "operationName": operation,
                "query": query,
                "variables": variables,
            },
            headers={
                "Content-Type": "application/json",
                "x-famly-accesstoken": self._state.access_token,
                "User-Agent": "FamlyPhotoDashboard/1.0",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {data['errors']}")
        return data.get("data", {})

    # ── Private ──────────────────────────────────────────────────────────

    def _build_headers(self) -> dict[str, str]:
        return {
            "x-famly-accesstoken": self._state.access_token,
            "X-Famly-InstallationId": self.installation_id,
            "X-Famly-Platform": "html",
            "User-Agent": "FamlyPhotoDashboard/1.0",
            "Accept": "*/*",
        }

    def _login(self) -> None:
        if not self.email or not self.password:
            raise ValueError(
                "FAMLY_EMAIL and FAMLY_PASSWORD are required for auto-login"
            )

        logger.info("Logging in to Famly as %s (GraphQL)", self.email)

        resp = requests.post(
            f"{GRAPHQL_URL}?Authenticate",
            json={
                "operationName": "Authenticate",
                "query": AUTHENTICATE_MUTATION,
                "variables": {
                    "email": self.email,
                    "password": self.password,
                    "deviceId": self._device_id,
                    "legacy": False,
                },
            },
            headers={
                "Content-Type": "application/json",
                "User-Agent": "FamlyPhotoDashboard/1.0",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            raise RuntimeError(f"Famly login failed: {data['errors']}")

        result = data.get("data", {}).get("me", {}).get("authenticateWithPassword", {})
        typename = result.get("__typename", "")

        if typename == "AuthenticationSucceeded":
            token = result["accessToken"]
            self._state = TokenState(
                access_token=token,
                installation_id=self.installation_id,
                obtained_at=time.time(),
            )
            self._save_token()
            logger.info("Login successful – token obtained")

        elif typename == "AuthenticationChallenged":
            # Multi-context account – auto-pick the first context
            choices = result.get("choices", [])
            login_id = result.get("loginId", "")
            device_id = result.get("deviceId", "")
            if not choices:
                raise RuntimeError("Login challenged but no choices returned")
            logger.info(
                "Login challenged with %d context(s) – picking first", len(choices)
            )
            # TODO: if you have multiple nurseries, you may want to make
            # this configurable. For now, auto-select the first choice.
            self._resolve_challenge(login_id, device_id, choices[0])

        elif typename == "AuthenticationFailed":
            title = result.get("errorTitle", "Unknown error")
            details = result.get("errorDetails", "")
            raise RuntimeError(f"Famly login failed: {title} – {details}")

        else:
            raise RuntimeError(
                f"Unexpected auth response type: {typename}. "
                f"Keys: {list(result.keys())}"
            )

    def _resolve_challenge(
        self, login_id: str, device_id: str, choice: dict
    ) -> None:
        """Complete login when Famly returns an AuthenticationChallenged response."""
        context_id = choice["context"]["id"]
        hmac = choice["hmac"]

        CHOOSE_MUTATION = """
        mutation ChooseContext($loginId: LoginId!, $deviceId: DeviceId!, $contextId: UserContextId!, $hmac: String!) {
            me {
                chooseContext(loginId: $loginId, deviceId: $deviceId, contextId: $contextId, hmac: $hmac) {
                    ... on ChooseContextSucceeded {
                        accessToken
                        __typename
                    }
                    ... on ChooseContextFailed {
                        errorTitle
                        __typename
                    }
                    __typename
                }
                __typename
            }
        }
        """

        resp = requests.post(
            f"{GRAPHQL_URL}?ChooseContext",
            json={
                "operationName": "ChooseContext",
                "query": CHOOSE_MUTATION,
                "variables": {
                    "loginId": login_id,
                    "deviceId": device_id,
                    "contextId": context_id,
                    "hmac": hmac,
                },
            },
            headers={
                "Content-Type": "application/json",
                "User-Agent": "FamlyPhotoDashboard/1.0",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        result = data.get("data", {}).get("me", {}).get("chooseContext", {})
        if result.get("__typename") == "ChooseContextSucceeded":
            token = result["accessToken"]
            self._state = TokenState(
                access_token=token,
                installation_id=self.installation_id,
                obtained_at=time.time(),
            )
            self._save_token()
            logger.info("Login successful (via context selection)")
        else:
            raise RuntimeError(
                f"Context selection failed: {result.get('errorTitle', 'unknown')}"
            )

    def _load_cached_token(self) -> None:
        if self.token_path.exists():
            try:
                raw = json.loads(self.token_path.read_text())
                self._state = TokenState(**raw)
                logger.info("Loaded cached token (age: %.1fh)", self.token_age_hours)
            except Exception as exc:
                logger.warning("Failed to load cached token: %s", exc)

    def _save_token(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(
            json.dumps({
                "access_token": self._state.access_token,
                "session_marker": self._state.session_marker,
                "installation_id": self._state.installation_id,
                "obtained_at": self._state.obtained_at,
            })
        )
