"""One-off OAuth consent flow for the dedicated Gmail/Calendar account (D-18).

Terminal only: it never opens a browser and never listens on a port, so it
works the same on a laptop and inside Docker. Normally run by the setup
wizard `terraform/print_env.sh` (terraform/README.md, step 6).

Flow (a "manual loopback" redirect; Google no longer allows the old
copy-a-code flow for Desktop clients):
1. It prints the consent URL. Open it in any browser, on any machine, and
   sign in as the dedicated account.
2. After Allow, Google sends the browser to `http://localhost:<port>/?code=...`.
   Nothing listens there, so the page fails to load; that's expected.
3. Paste that full address from the browser's address bar into the
   terminal. The code in it is exchanged (with PKCE) for the refresh token.

Usage:
    uv run --env-file .env python scripts/oauth_consent.py

GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are read from the environment if set
there, otherwise prompted for. Everything meant for the person goes to
stderr; stdout gets only the final `GOOGLE_REFRESH_TOKEN=...` line.
"""

from __future__ import annotations

import os
import secrets
import sys
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow

from dt_agent.adapters.google_auth import SCOPES


class ConsentError(Exception):
    """The pasted text doesn't yield a usable authorization code."""


def _say(text: str = "") -> None:
    print(text, file=sys.stderr)


def _ask(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline().strip()


def _client_id() -> str:
    value = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    return value or _ask("Google OAuth client ID: ")


def _client_secret() -> str:
    value = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    return value or _ask("Google OAuth client secret: ")


def _client_config(client_id: str, client_secret: str) -> dict[str, dict[str, object]]:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def extract_code(pasted: str, expected_state: str) -> str:
    """The authorization code from the pasted redirect address (or a bare code).

    A full address must carry the `state` this run generated, so a stale or
    foreign redirect is rejected. Raises `ConsentError` otherwise.
    """
    text = pasted.strip()
    if not text:
        raise ConsentError("nothing was pasted.")
    if not text.startswith(("http://", "https://")):
        return text

    query = parse_qs(urlparse(text).query)
    error = query.get("error", [""])[0]
    if error:
        raise ConsentError(f"Google returned an error: {error}.")
    if query.get("state", [""])[0] != expected_state:
        raise ConsentError(
            "the pasted address's state doesn't match this run; paste the "
            "address from the consent you just completed."
        )
    code = query.get("code", [""])[0]
    if not code:
        raise ConsentError("the pasted address has no code parameter.")
    return code


def main() -> int:
    client_id = _client_id()
    client_secret = _client_secret()
    if not client_id or not client_secret:
        _say("Both a client ID and a client secret are required.")
        return 1

    # A random high port nothing listens on: the browser's request fails to
    # load, and the code stays in its address bar for pasting.
    port = 20000 + secrets.randbelow(40000)
    flow = InstalledAppFlow.from_client_config(
        _client_config(client_id, client_secret),
        scopes=list(SCOPES),
        redirect_uri=f"http://localhost:{port}/",
    )
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")

    _say("1. Open this URL in any browser (on any machine):")
    _say()
    _say(f"   {auth_url}")
    _say()
    _say("2. Sign in as the dedicated account and allow access.")
    _say(f"3. The browser then shows an error page at http://localhost:{port}/...")
    _say("   That's expected. Copy the full address from the address bar.")
    _say()
    pasted = _ask("4. Paste the address here: ")

    try:
        code = extract_code(pasted, state)
        flow.fetch_token(code=code)
    except ConsentError as exc:
        _say(f"Consent failed: {exc}")
        return 1
    except Exception as exc:  # the token endpoint's own error, shown as-is
        _say(f"Consent failed: Google refused the code ({exc}). Run the consent again.")
        return 1

    refresh_token = flow.credentials.refresh_token
    if not refresh_token:
        _say(
            "No refresh token was returned. This usually happens when the "
            "account already granted consent once; revoke access at "
            "https://myaccount.google.com/permissions and try again."
        )
        return 1

    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
