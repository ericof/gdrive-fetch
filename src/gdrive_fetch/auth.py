from __future__ import annotations

from google.auth.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path

import asyncio


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "gdrive-fetch" / "token.json"


def load_credentials(
    *,
    service_account_file: Path | None = None,
    client_secrets_file: Path | None = None,
    token_file: Path = DEFAULT_TOKEN_FILE,
) -> Credentials:
    """Return Google credentials.

    Priority:
      1. Service account JSON (headless; the folder must be shared with the SA email).
      2. Cached OAuth user token.
      3. Interactive OAuth flow using an "installed app" client secrets file.
    """
    if service_account_file:
        return service_account.Credentials.from_service_account_file(
            str(service_account_file), scopes=SCOPES
        )

    creds: UserCredentials | None = None
    if token_file.exists():
        creds = UserCredentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not client_secrets_file:
            raise SystemExit(
                "No cached token and no --client-secrets given. "
                "Provide a service account (--service-account) or an OAuth "
                "client secrets file."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_file), SCOPES
        )
        creds = flow.run_local_server(port=0)

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    return creds


class TokenProvider:
    """Hands out a valid bearer token to concurrent coroutines.

    google-auth refresh is blocking, so it runs in a thread under a lock
    to avoid a thundering herd of refreshes when the token expires mid-run.
    """

    def __init__(self, credentials: Credentials) -> None:
        self._creds = credentials
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        if not self._creds.valid:
            async with self._lock:
                if not self._creds.valid:
                    await asyncio.to_thread(self._creds.refresh, Request())
        return str(self._creds.token)
