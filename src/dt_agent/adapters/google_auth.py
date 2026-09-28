"""Shared Google OAuth credentials (D-18).

One dedicated Gmail account is authorized once, offline, via
`scripts/oauth_consent.py`; the resulting refresh token (plus the OAuth
client ID/secret) is all this module needs to rebuild usable credentials
at process startup. No token cache and no interactive flow live here.
"""

from __future__ import annotations

from google.oauth2.credentials import Credentials

#: Exactly D-18's scopes: Gmail read/write/send/labels, and the manager's
#: own Calendar (events + free/busy). Never widened without updating D-18.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


def build_credentials(client_id: str, client_secret: str, refresh_token: str) -> Credentials:
    """Rebuild `Credentials` from a stored refresh token (D-18).

    No access token is carried over (`token=None`); `google-auth` fetches
    one lazily on first use via `refresh_token` / `client_id` /
    `client_secret` against Google's token endpoint.
    """
    return Credentials(  # type: ignore[no-untyped-call]  # google-auth ships no parameter types
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(SCOPES),
    )
