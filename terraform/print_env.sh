#!/usr/bin/env bash
# terraform/print_env.sh — D-18, D-67 (terraform/README.md, step 6).
#
# A wizard for the part Terraform can't do: it walks through the Google
# Auth Platform pages one step at a time (prints the URL and what to do,
# waits for Enter), asks for the OAuth client ID and secret that the
# Console shows in a dialog when the Desktop client is created (pasted
# here; nothing is saved to a file), then runs the consent
# (scripts/oauth_consent.py), terminal only: it prints the consent URL, and
# the address the browser ends on after consent is pasted back. Nothing opens
# a browser or listens on a port, so it also works inside Docker.
#
# At the end it writes the three values into the project's .env, which must
# exist (checked first): only the GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and
# GOOGLE_REFRESH_TOKEN lines change (a missing one is appended); comments,
# order and every other line stay as they were. The file is rewritten in
# place (same inode), so a bind-mounted .env inside Docker is updated on the
# host too; it's read back to confirm the replacement. .env is parsed, never
# sourced: real GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET values there are
# reused.
#
# stdout gets only the three GOOGLE_* lines (also shown on screen); the
# rest is on stderr, and answers are read from the terminal. gcloud
# is only read (the active configuration's account and project). Works on
# macOS bash 3.2.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
ENV_FILE="${PROJECT_ROOT}/.env"
TFVARS_FILE="${SCRIPT_DIR}/gmail/terraform.tfvars"
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

echo "print_env.sh: a wizard that sets up the Google OAuth consent screen and client for the dedicated Gmail account, then prints GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN and writes them into .env (nothing else in .env changes)." >&2

fail() {
  echo "print_env.sh: error: $*" >&2
  exit 1
}

say() {
  printf '%s\n' "$*" >&2
}

# Waits for Enter on the terminal.
confirm() {
  read -r -p "   Press Enter when done (Ctrl-C to stop) " _ </dev/tty
  say ""
}

step() {
  say ""
  say "=== Step $1 of 5: $2 ==="
}

# The value of KEY in .env: last assignment wins, surrounding quotes removed.
env_value() {
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -n1 \
    | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# Sets the three GOOGLE_* keys in .env to the values in NEW_GOOGLE_CLIENT_ID,
# NEW_GOOGLE_CLIENT_SECRET and NEW_GOOGLE_REFRESH_TOKEN (read by awk from the
# environment, so no value is ever interpreted). Every existing `KEY=` line
# of those three keys gets the new value; a key with no line is appended;
# every other line is copied unchanged. Written through a private temp file
# next to .env, then copied back over .env in place (same inode and
# permissions, so a bind-mounted .env is updated too).
update_env_file() {
  tmp_file="$(umask 077 && mktemp "${ENV_FILE}.XXXXXX")"
  if ! awk '
    BEGIN { n = split("GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_REFRESH_TOKEN", keys, " ") }
    {
      replaced = 0
      for (i = 1; i <= n; i++) {
        if ($0 ~ ("^[ \t]*" keys[i] "[ \t]*=")) {
          print keys[i] "=" ENVIRON["NEW_" keys[i]]
          seen[keys[i]] = 1
          replaced = 1
          break
        }
      }
      if (!replaced) print
    }
    END {
      for (i = 1; i <= n; i++) {
        if (!(keys[i] in seen)) print keys[i] "=" ENVIRON["NEW_" keys[i]]
      }
    }
  ' "$ENV_FILE" >"$tmp_file"; then
    rm -f "$tmp_file"
    fail "could not update .env (it was left unchanged)."
  fi
  cat "$tmp_file" >"$ENV_FILE"
  rm -f "$tmp_file"
}

is_placeholder() {
  case "$1" in
    "" | your* | *"<"*) return 0 ;;
    *) return 1 ;;
  esac
}

# --- Checks (README steps 1-5) ----------------------------------------------

[ -r /dev/tty ] || fail "this wizard needs an interactive terminal."

[ -f "$ENV_FILE" ] || fail ".env not found at the project root ($ENV_FILE). Create it first: cp .env.sample .env"
[ -w "$ENV_FILE" ] || fail ".env is not writable ($ENV_FILE)."

command -v gcloud >/dev/null 2>&1 || fail "'gcloud' is not installed (terraform/README.md, step 1)."

# The Python that runs the consent helper: `uv run` on a laptop; inside the
# app's Docker image (no uv) its own virtualenv, which has the dependencies.
if command -v uv >/dev/null 2>&1; then
  PYTHON_RUN="uv run --quiet python"
elif python -c "import google_auth_oauthlib, dt_agent" >/dev/null 2>&1; then
  PYTHON_RUN="python"
else
  fail "'uv' is not installed (terraform/README.md, step 1)."
fi
[ -f "$PROJECT_ROOT/scripts/oauth_consent.py" ] \
  || fail "scripts/oauth_consent.py not found (inside Docker, docker-compose.yml mounts ./scripts)."

account="$(gcloud config get-value account 2>/dev/null || true)"
if [ -z "$account" ] || [ "$account" = "(unset)" ]; then
  fail "the active gcloud configuration has no account (terraform/README.md, steps 2-3)."
fi

project="$(gcloud config get-value project 2>/dev/null || true)"
if [ -z "$project" ] || [ "$project" = "(unset)" ]; then
  project="$(sed -n 's/^[[:space:]]*project_id[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$TFVARS_FILE" 2>/dev/null | head -n1)"
fi
[ -n "$project" ] || fail "no project: the active gcloud configuration has none and terraform/gmail/terraform.tfvars has no project_id (terraform/README.md, steps 4-5)."

say ""
say "Account: $account"
say "Project: $project"
say "Open every link below in a browser signed in as $account."

# --- Step 1: branding and audience ------------------------------------------

step 1 "consent screen (branding and audience)"
say "   Open: https://console.cloud.google.com/auth/branding?project=$project"
say "   - If you see \"Get started\", click it. App name: dt-agent; user support email: $account."
say "   - Audience: External. Contact email: $account. Agree to the policy, then Create."
say "   - If the branding page already shows the app, it's done: just continue."
confirm

# --- Step 2: scopes ---------------------------------------------------------

step 2 "data access (the five scopes)"
say "   Open: https://console.cloud.google.com/auth/scopes?project=$project"
say "   - Click \"Add or remove scopes\". Under \"Manually add scopes\", paste these five lines:"
say "       https://www.googleapis.com/auth/gmail.modify"
say "       https://www.googleapis.com/auth/gmail.send"
say "       https://www.googleapis.com/auth/gmail.labels"
say "       https://www.googleapis.com/auth/calendar.events"
say "       https://www.googleapis.com/auth/calendar.readonly"
say "   - Click \"Add to table\", then Update, then Save at the bottom of the page."
confirm

# --- Step 3: publish --------------------------------------------------------

step 3 "add the account as a test user (the app stays in Testing)"
say "   Open: https://console.cloud.google.com/auth/audience?project=$project"
say "   - Leave \"Publishing status\" as Testing."
say "   - Under \"Test users\", click \"Add users\", enter $account, then Save."
say "   - In Testing, the refresh token expires after 7 days: then run this wizard again"
say "     (steps 1-4 are already done, just press Enter); it writes the new token into .env."
confirm

# --- Step 4: the OAuth client -----------------------------------------------

step 4 "the Desktop-app OAuth client"
client_id="$(env_value GOOGLE_CLIENT_ID)"
client_secret="$(env_value GOOGLE_CLIENT_SECRET)"
if ! is_placeholder "$client_id" && ! is_placeholder "$client_secret"; then
  say "   .env already has GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET; using them ($client_id)."
else
  say "   Open: https://console.cloud.google.com/auth/clients/create?project=$project"
  say "   - Application type: Desktop app. Name: dt-agent. Click Create."
  say "   - A dialog shows the Client ID and the Client secret (the secret is shown only now)."
  say "     Copy them into the two prompts below. Nothing needs to be downloaded."
  say ""
  read -r -p "   Client ID: " client_id </dev/tty
  read -r -s -p "   Client secret (hidden): " client_secret </dev/tty
  say ""
fi
case "$client_id" in
  *.apps.googleusercontent.com) ;;
  *) fail "that doesn't look like a Desktop OAuth client ID (it should end in .apps.googleusercontent.com)." ;;
esac
is_placeholder "$client_secret" && fail "the client secret is empty."

# --- Step 5: browser consent for the refresh token --------------------------

step 5 "consent (gets the refresh token)"
say "   Terminal only: nothing opens by itself, so this also works inside Docker."
say "   In the browser, when asked:"
say "   - Sign in as $account."
say "   - \"Google hasn't verified this app\": click Advanced, then \"Go to dt-agent (unsafe)\"."
say "   - Tick all the permissions and click Continue."
say ""

# The helper talks to the person on stderr and reads the pasted address from
# the terminal; its stdout carries only the GOOGLE_REFRESH_TOKEN= line. A
# function, because bash 3.2 (macOS) misparses a `case` pattern's `)` inside
# `$( ... )`.
filter_consent_output() {
  while IFS= read -r line; do
    case "$line" in
      GOOGLE_REFRESH_TOKEN=*) printf '%s\n' "${line#GOOGLE_REFRESH_TOKEN=}" ;;
    esac
  done
}

run_consent() {
  cd "$PROJECT_ROOT" \
    && GOOGLE_CLIENT_ID="$client_id" GOOGLE_CLIENT_SECRET="$client_secret" PYTHONUNBUFFERED=1 \
      $PYTHON_RUN scripts/oauth_consent.py </dev/tty
}

set +e
refresh_token="$(run_consent | filter_consent_output)"
consent_status=$?
set -e

[ "$consent_status" -eq 0 ] || fail "the consent flow failed (scripts/oauth_consent.py exited with status $consent_status)."
[ -n "$refresh_token" ] || fail "the consent flow returned no refresh token."

say ""
say "The three values:"
say ""
printf 'GOOGLE_CLIENT_ID=%s\n' "$client_id"
printf 'GOOGLE_CLIENT_SECRET=%s\n' "$client_secret"
printf 'GOOGLE_REFRESH_TOKEN=%s\n' "$refresh_token"

NEW_GOOGLE_CLIENT_ID="$client_id" \
  NEW_GOOGLE_CLIENT_SECRET="$client_secret" \
  NEW_GOOGLE_REFRESH_TOKEN="$refresh_token" \
  update_env_file

[ "$(env_value GOOGLE_CLIENT_ID)" = "$client_id" ] \
  && [ "$(env_value GOOGLE_CLIENT_SECRET)" = "$client_secret" ] \
  && [ "$(env_value GOOGLE_REFRESH_TOKEN)" = "$refresh_token" ] \
  || fail ".env was written but doesn't read back the new values; set the three lines above in .env by hand."

say ""
say "Done. Replaced GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN in $ENV_FILE successfully (nothing else in it changed)."
