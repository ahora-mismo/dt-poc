# Google Cloud project + APIs for the agent's dedicated Gmail account
# (D-18, D-67). What this configuration deliberately does NOT create: the
# OAuth consent screen / Google Auth Platform config, the Desktop OAuth
# client, and the refresh token — none of these are supported by
# Terraform's google provider or by any public Google API; they stay
# manual console steps (see terraform/README.md and D-18 steps 4-6).

# Authentication: a short-lived user access token from the dedicated
# `dt-agent` gcloud configuration, passed as GOOGLE_OAUTH_ACCESS_TOKEN
# (see terraform/README.md, steps 2-4) — never a service account (D-18:
# service accounts can't access a consumer @gmail.com mailbox without
# Workspace domain-wide delegation), and never the machine-wide
# Application Default Credentials, so the operator's other gcloud setups
# are untouched:
#
#   export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token --configuration=dt-agent)"
#
# No credentials are set on the provider block itself; the provider reads
# the token from the environment. A gcloud-issued token already carries
# gcloud's own quota project, so no `user_project_override` /
# `billing_project` is set: pointing the quota at the new project would
# need that project's Service Usage API enabled first, which a brand-new
# project doesn't have (HTTP 403 SERVICE_DISABLED).
provider "google" {}

# The project itself, created under "No organization" (D-18 step 2): no
# org_id, no folder_id, no billing_account argument at all (standard
# Gmail API / Calendar API usage is free, D-18).
resource "google_project" "this" {
  project_id = var.project_id
  name       = var.project_name

  # `deletion_policy` (google provider attribute on google_project): the
  # 6.x provider defaults this to "PREVENT", which blocks `terraform
  # destroy` from actually deleting the project (a safety rail meant for
  # production projects). This project is a disposable POC resource, so
  # it's set to "DELETE" here so `terraform destroy` works as expected.
  # Double-check this attribute name against the `hashicorp/google`
  # provider docs for whatever ~> 6.0 patch version `terraform init`
  # resolves, in case a future 6.x release renames or deprecates it.
  deletion_policy = "DELETE"
}

locals {
  # D-18: Gmail API and Google Calendar API. Note the Calendar API's
  # service name is "calendar-json.googleapis.com", not
  # "calendar.googleapis.com".
  apis = [
    "gmail.googleapis.com",
    "calendar-json.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.apis)

  project = google_project.this.project_id
  service = each.value

  # Don't disable the API if this resource/config is ever destroyed
  # without also destroying the project: disabling an API out from under
  # a manually-configured OAuth client/consent screen (which Terraform
  # doesn't manage, see above) would be surprising and isn't useful for
  # this POC.
  disable_on_destroy = false

  # Implicit via `project = google_project.this.project_id` above, but
  # spelled out because a brand-new project can 404 on the Service Usage
  # API for a few seconds after creation; if `apply` fails here the first
  # time, re-running it is expected to succeed (README "Notes").
  depends_on = [google_project.this]
}
