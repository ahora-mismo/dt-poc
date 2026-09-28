# Terraform — Google Cloud setup for the agent's dedicated Gmail account

Infrastructure as code for the Google Cloud project that backs D-18's
dedicated Gmail account: `terraform/gmail/` creates the parts of the D-18
setup that Terraform (and the underlying Google APIs) can actually
automate. It is a small, one-time convenience, not a general-purpose IaC
setup — see "What can't be automated" below for why some of the steps in
"Setup, step by step" stay manual.

## What it creates

- A Google Cloud **project** (`google_project`), under **"No
  organization"**: no `org_id`, no `folder_id`, no billing account. Standard
  Gmail API / Calendar API use is free (D-18), so no billing account is
  needed.
- Two **APIs** enabled on that project (`google_project_service`):
  - `gmail.googleapis.com`
  - `calendar-json.googleapis.com` (the Calendar API's actual service
    name)

That's steps 4–5 below (D-18 steps 2–3). Outputs: the resulting
`project_id`, the list of enabled APIs, and ready-made, project-scoped
console URLs for the manual pages (`google_auth_platform_url`,
`oauth_audience_url`, `oauth_scopes_url`, `oauth_client_create_url`); the
wizard in step 6 prints the same pages.

## What can't be automated

Terraform's `google` provider, and the Google APIs it drives, have no
resource for any of the following — this isn't a shortcut we skipped, it
genuinely isn't exposed programmatically for consumer/External OAuth
apps. The wizard `terraform/print_env.sh` guides you through them
(step 6):

- **The OAuth consent screen** (Google Auth Platform branding, audience,
  data access/scopes, publishing) — configured only through the Cloud
  Console UI.
- **The OAuth client** (the Desktop-app client id/secret) — also
  console-only; there is no API to create OAuth 2.0 client credentials
  for this app type, or to read the secret back.
- **The refresh token** — this requires an actual interactive OAuth
  consent flow in a browser, performed once by the account owner.

## Prerequisites

- The **dedicated Gmail account** already exists (D-18 step 1) — this
  configuration creates the *project*, not the account.
- No billing account, no Google Cloud organization or folder.
- Either the CLIs installed locally (step 1 below), or Docker only — the
  app's image already contains the Google Cloud CLI, Terraform and the
  Python the wizard needs (see "Running from Docker" below). The wizard
  (step 6) never opens a browser: it prints every URL for you to open in
  any browser. The gcloud login (step 3) does open one locally; in Docker
  it runs with `--no-launch-browser`.

## Setup, step by step

From a clean system to a filled-in `.env`, in order.

### 1. Install the tools

`gcloud` and `terraform` do the automated part (steps 4–5); `uv` (the
project's own package/task runner, already a prerequisite for the app
itself — see the project root `README.md`) is needed later, by
`terraform/print_env.sh` (step 6), to run `scripts/oauth_consent.py`.

macOS (Homebrew):

```bash
brew update
brew install --cask gcloud-cli
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
brew install uv
```

Debian / Ubuntu:

```bash
sudo apt-get update && sudo apt-get install -y curl ca-certificates gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  | sudo tee /etc/apt/keyrings/google-cloud.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/google-cloud.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
  | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
curl -fsSL https://apt.releases.hashicorp.com/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/hashicorp.gpg
echo "deb [signed-by=/etc/apt/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo "$VERSION_CODENAME") main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt-get update && sudo apt-get install -y google-cloud-cli terraform
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, if not already installed
```

**Check:**

```bash
gcloud --version
terraform version      # must be >= 1.9
uv --version
```

### 2. Create the `dt-agent` gcloud configuration

Never log in inside one of your existing configurations: `gcloud auth
login` changes the account of whatever configuration is active. Create a
new one for this project instead — it shows up as its own line in
`gcloud config configurations list`:

```bash
# Note which configuration is active now, to switch back later (step 7):
gcloud config configurations list

# Create the new configuration (this also activates it):
gcloud config configurations create dt-agent
```

**Check:** `dt-agent` appears in `gcloud config configurations list`.

### 3. Sign in as the dedicated account, in that configuration

In the browser that opens, pick the **dedicated Gmail account**.

```bash
gcloud auth login --no-activate            # adds the account's credentials
gcloud config set account dt.agent.interview@gmail.com   # binds it to the dt-agent configuration
```

Don't run `gcloud auth application-default login`: it would overwrite
your machine-wide Application Default Credentials, which other tools may
use. Terraform gets a short-lived token from the `dt-agent` configuration
instead (step 4).

**Check:**

```bash
gcloud config configurations list   # dt-agent: IS_ACTIVE True, with the dedicated account
gcloud config list
```

### 4. Create the project and enable the APIs

The project id is set in `terraform/gmail/terraform.tfvars`:
`dt-agent-interview-2026` (project ids are globally unique — if `apply`
reports that it already exists, change the suffix there and in the
commands below). With `dt-agent` active:

```bash
cd terraform/gmail
export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token --configuration=dt-agent)"
```

**4a. Initialize, only if needed.** Check whether this directory was
already initialized:

```bash
ls -d .terraform 2>/dev/null && echo "initialized" || echo "not initialized"
```

- **not initialized** (a fresh clone: `.terraform/` is gitignored) → run
  `terraform init`. It downloads the provider pinned in the committed
  `.terraform.lock.hcl`.
- **initialized** → skip `terraform init`. Run it again only if a later
  command asks for it (e.g. "Inconsistent dependency lock file" or
  "Required plugins are not installed" after a provider version change);
  `terraform init` is safe to re-run and changes nothing in Google Cloud.

**4b. Plan and apply:**

```bash
terraform plan
terraform apply
unset GOOGLE_OAUTH_ACCESS_TOKEN
```

- **`plan` shows changes** (first run: 1 project + 2 APIs to add) →
  `apply` and type `yes`.
- **`plan` says "No changes"** (the project already exists: the state is
  committed, see "Decision: state and tfvars are committed") → nothing to
  apply; go on to step 5. `terraform output` prints the outputs.

The token is valid for about an hour; if Terraform later reports an
expired token, re-run the `export` line.

**Check:** `terraform apply` (or `terraform output`) prints `project_id`,
`apis_enabled`, and the console URLs (`google_auth_platform_url`,
`oauth_scopes_url`, `oauth_client_create_url`); `terraform output`
reprints them later.

### 5. Point the `dt-agent` configuration at the new project

```bash
gcloud config set project dt-agent-interview-2026 --configuration=dt-agent
```

**Check:**

```bash
gcloud services list --enabled --configuration=dt-agent | grep -E "gmail|calendar"   # both APIs listed
```

### 6. Run the wizard: `terraform/print_env.sh`

With the `dt-agent` configuration still active (step 2), and `.env` present
at the project root (`cp .env.sample .env` first if it isn't; the wizard
checks). Terminal only: it never opens a browser, so it runs the same
locally or inside Docker.

```bash
terraform/print_env.sh
```

No arguments. The first line it prints says what it does. It then walks
through five steps, each with the URL to open and what to do there, and
waits for Enter before the next one:

1. **Consent screen:** branding (app name, support email) and audience
   **External**.
2. **Data access:** the five scopes (D-18), which it lists for pasting:
   `gmail.modify`, `gmail.send`, `gmail.labels`, `calendar.events`,
   `calendar.readonly`.
3. **Test user:** the app stays in **Testing**; add the dedicated account
   under "Test users" (without it, the consent in step 5 is refused).
   In Testing, the refresh token expires after 7 days (`invalid_grant`):
   then run the wizard again (steps 1–4 are already done, just press
   Enter); it writes the new token into `.env`. Publishing to
   production would need a completed Branding page (home page and privacy
   policy links on a verified domain), which this POC doesn't have.
4. **OAuth client** of type Desktop app. The dialog shown right after
   creation holds the client ID and secret (the only time the secret is
   shown): paste them into the wizard's two prompts (the secret is hidden
   as you type). Nothing is downloaded. If `.env` already has real
   values for both, the wizard uses them and skips the prompts.
5. **Consent:** it prints the consent URL; open it in any browser, sign
   in as the dedicated account, click through "Google hasn't verified this
   app" (Advanced → Go to dt-agent) and allow all permissions. The browser
   then lands on an error page at `http://localhost:<port>/?code=...`
   (nothing listens there; that's expected): copy the full address from
   the address bar and paste it into the wizard, which exchanges it for
   the refresh token.

At the end it prints the three lines and writes them into `.env`: only the
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `GOOGLE_REFRESH_TOKEN`
values change (a missing key is appended); comments, order and every other
line stay as they were. It reads the file back and says whether the
replacement succeeded.

If a check fails, it prints one `print_env.sh: error: ...` line naming the
step to fix, and exits 1.

**Check:** it ends with the three lines and

```
Done. Replaced GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN in .../.env successfully (nothing else in it changed).
```

### 7. Switch back to your previous gcloud configuration

```bash
gcloud config configurations activate <your-previous-configuration>
```

**Check:** `gcloud config configurations list` shows your own
configuration as `IS_ACTIVE True` again.

## Running from Docker

The app's own image already has the Google Cloud CLI and Terraform (see
the root `Dockerfile`), and `docker-compose.yml` bind-mounts `./terraform`
into the `dt-agent` service at `/app/terraform`, so Terraform's state
lands on the host. There is no separate setup image.

**Steps 1–5** (the tools, the gcloud login, and `terraform apply`) can
run inside the container instead of locally — the image already has
`gcloud` and `terraform` (step 1). On a clean system the app can't start
yet (`.env` has no Google credentials), so `docker exec` into a running
container isn't possible; open a one-off shell in the same image
instead, from the project root:

```bash
docker compose build
docker compose run --rm --entrypoint /bin/sh dt-agent
```

Once the app is configured and running, `docker compose exec -it dt-agent
/bin/sh` (or `docker exec -it <container> /bin/sh`) gives the same shell.

Inside the container, gcloud's state belongs only to that container (it
never touches your machine's gcloud setup). Run steps 2–5 as above, with
`--no-launch-browser` on the login (the container has no browser: `gcloud`
prints a URL to open on your machine and asks you to paste the code back):

```bash
gcloud config configurations create dt-agent
gcloud auth login --no-activate --no-launch-browser
gcloud config set account dt.agent.interview@gmail.com

cd /app/terraform/gmail
export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token --configuration=dt-agent)"
terraform init      # only if not initialized yet (step 4a)
terraform plan      # "No changes" → skip apply (step 4b)
terraform apply
unset GOOGLE_OAUTH_ACCESS_TOKEN

gcloud config set project dt-agent-interview-2026 --configuration=dt-agent
```

`./terraform` is bind-mounted from the host, so a `.terraform/` created by
a local `terraform init` is visible here too. It holds provider binaries
for the host's OS, though: on a macOS host, the Linux container's
`terraform plan` reports "Required plugins are not installed", which is
the step 4a case for running `terraform init` again (it adds the Linux
binary next to the macOS one; both keep working). That run may also add
the Linux checksums to `.terraform.lock.hcl`; commit that change.

**The container's `gcloud` login does not persist across container
recreation.** It's written to the container's own filesystem
(`~/.config/gcloud`, not a mounted volume), so recreating the container
(`docker compose up --build`, `docker compose down` + `up`, …) loses it
and steps 2–3 have to be repeated. Terraform state itself is safe either
way — it lives under the bind-mounted `./terraform`, on the host.

**Step 6 (the wizard) runs in the same container shell**, after steps 2–5
there:

```bash
/app/terraform/print_env.sh
```

It is terminal only, so the container needs no browser and no published
port: open the printed URLs on your machine and paste the final address
back. Inside the image it runs the consent helper with the image's own
Python (no `uv` there), from `./scripts`, which `docker-compose.yml`
mounts read-only at `/app/scripts`. `docker-compose.yml` also mounts the
host's `.env` at `/app/.env`, so the values the wizard writes land in the
host's file (no new exposure: `env_file` already puts every value into the
container's environment). The running app picks them up only when the
container is recreated (`docker compose up -d`), because `env_file` is
read at container start.

## Decision: state and tfvars are committed (POC only)

This configuration uses **local Terraform state only** (no `backend`
block, no remote backend), and both `terraform.tfvars` and
`terraform/gmail/terraform.tfstate` (plus its `.backup`) are **committed
to git**.

Normally, neither of these is committed: Terraform state is kept in a
remote backend (for Google Cloud, typically a GCS bucket with versioning
and locking), and `terraform.tfvars` is often kept out of git too when it
carries anything sensitive or environment-specific.

For this POC, for simplicity, both are committed anyway — a deliberate
decision, not an oversight, and **not one to repeat on a production
project**:

- **Accepted risks:** no state locking (two people running `apply` at the
  same time can corrupt the state); no history or backup for the state
  beyond whatever git itself provides; the state can go stale if someone
  runs `apply` locally without committing the result; and, in general,
  Terraform state files can contain sensitive values (resource
  attributes, sometimes secrets). As of now, this configuration's state
  holds none of that: only the project id and name, the enabled APIs, and
  their own resource metadata. No OAuth client, client secret or refresh
  token is ever managed by Terraform (see "What can't be automated"
  above) — so committing these files poses no threat at this point.
- **What to do for production instead:** a `backend "gcs"` block pointing
  at a versioned bucket, so state never lives in git; and keep
  `terraform.tfvars` out of git wherever it might later carry sensitive
  values.

`terraform/gmail/.terraform/` stays gitignored either way (it's
regenerated by `terraform init`); `.terraform.lock.hcl` is committed — it
pins the exact provider version/checksums `terraform init` resolved, the
same role `uv.lock` plays for Python dependencies.

## Destroying

```bash
cd terraform/gmail
export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token --configuration=dt-agent)"
terraform destroy
unset GOOGLE_OAUTH_ACCESS_TOKEN
```

This deletes the Google Cloud project itself (`deletion_policy = "DELETE"`
on the `google_project` resource — see the comment in `main.tf`), which
also takes the enabled APIs, the manually-created OAuth consent
screen/client, and any refresh tokens issued under it with it. There is
no other cleanup to do.

## Notes

- **`terraform/print_env.sh` (step 6) in more detail:**
  - **Checks:** an interactive terminal; `.env` exists at the project root
    and is writable; `gcloud` is installed; `uv`, or
    (inside the Docker image) the image's own Python with the app's
    dependencies; `scripts/oauth_consent.py` is present; the active gcloud
    configuration has an account and a project (the project falls back to
    `terraform/gmail/terraform.tfvars`). gcloud is only read (`gcloud
    config get-value`).
  - **Reads:** `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` from `.env` if
    both are real values (not the `.env.sample` placeholders), otherwise
    from the two prompts. `.env` is parsed, never sourced.
  - **Writes:** the three `GOOGLE_*` values into `.env`: each existing
    `KEY=` line of those three keys gets the new value, a missing key is
    appended, every other line is copied unchanged. It goes through a
    private temp file next to `.env`, then is copied back over `.env` in
    place (same file, same permissions, so the Docker bind mount sees it),
    and is read back to confirm.
  - **Runs:** `scripts/oauth_consent.py` from the project root with that ID
    and secret. It never opens a browser and never listens on a port: it
    prints the consent URL, asks for the `http://localhost:<port>/?code=...`
    address the browser ends on (a random port nothing listens on, so the
    page fails to load and the code stays in the address bar), checks its
    `state`, and exchanges the code (with PKCE) for the refresh token.
  - **stdout vs. stderr:** only the three `GOOGLE_*=` lines go to stdout.
    The first line, the steps, prompts, errors and the consent URL go to
    stderr.
  - **Why `.env` and gcloud alone aren't enough:** the refresh token is
    issued only at the end of an interactive browser consent by the
    account owner, and Google never returns an existing one again. The
    token gcloud already holds (`gcloud auth print-access-token`, used by
    Terraform in steps 4–5) belongs to gcloud's own OAuth client and its
    Cloud Platform scopes, and Google blocks that client for Gmail scopes,
    so it can't stand in for this app's client. Likewise, no `gcloud`
    command or public API reads back an OAuth client's secret for this
    app type (`gcloud iam oauth-clients` is Workforce Identity
    Federation, a different feature), which is why the wizard asks for it
    once, from the dialog the Console shows when the client is created.
- **No billing account is ever needed** for this project (D-18): Gmail
  API usage is free at this app's scale, and Google has stated charges
  for usage above 80,000,000 quota units/day per project would only start
  later in 2026, with at least 90 days' notice.
- **Project ids are globally unique across all of Google Cloud, forever**
  — even a deleted project's id can never be reused. If `terraform
  apply` fails with an "already exists" error, the id in
  `terraform.tfvars` is taken: change it there (and in the `gcloud config
  set project` commands).
- **`terraform.tfvars` is committed:** it holds no secrets, only the
  project id and name.
- **The "unverified app" warning during consent is expected**, not a
  misconfiguration: Google verification is only required for apps used
  at scale, and this is deliberately a single-user setup (D-18).
- **Service accounts are never used** for any of this: they can't access
  a consumer `@gmail.com` mailbox without Workspace domain-wide
  delegation, which this setup (a personal Gmail account, no
  organization) doesn't have (D-18).
- **Quota project for API calls.** The token from `gcloud auth
  print-access-token` already carries gcloud's own quota project, so
  enabling the APIs works on a brand-new project. Don't point the quota
  at the new project (`user_project_override` / `billing_project`, or
  `gcloud auth application-default set-quota-project`): that needs the
  project's own Service Usage API, which a new project doesn't have yet,
  and fails with `403 SERVICE_DISABLED`.
- **Authentication never touches your global gcloud state:** the
  `dt-agent` configuration holds the login, and Terraform only receives
  a short-lived token through `GOOGLE_OAUTH_ACCESS_TOKEN`. Your other
  configurations and your Application Default Credentials stay as they
  were.
- **A brand-new project can briefly 404** on the Service Usage API for a
  few seconds right after creation. If the first `apply` fails enabling
  the APIs, that's the likely cause — simply re-run `terraform apply`.
