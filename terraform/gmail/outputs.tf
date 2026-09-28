output "project_id" {
  description = "The Google Cloud project id created by this configuration."
  value       = google_project.this.project_id
}

output "apis_enabled" {
  description = "APIs enabled by this configuration."
  value       = [for s in google_project_service.apis : s.service]
}

# The following are plain console URLs, not resources: Terraform cannot
# create the OAuth consent screen or the OAuth client (D-67), but it can
# at least hand you the right, project-scoped page to do it by hand
# (D-18 steps 4-5).

output "google_auth_platform_url" {
  description = <<-EOT
    Google Auth Platform overview for this project (D-18 step 4):
    configure branding, audience (External) and data access (the five
    scopes below), then Publish -> In production.

      gmail.modify, gmail.send, gmail.labels,
      calendar.events, calendar.readonly

    Terraform cannot create or configure this (not supported by the
    google provider or by any public Google API).
  EOT
  value       = "https://console.cloud.google.com/auth/overview?project=${google_project.this.project_id}"
}

output "oauth_audience_url" {
  description = "Google Auth Platform audience page (External user type) for this project."
  value       = "https://console.cloud.google.com/auth/audience?project=${google_project.this.project_id}"
}

output "oauth_scopes_url" {
  description = "Google Auth Platform data access / scopes page for this project."
  value       = "https://console.cloud.google.com/auth/scopes?project=${google_project.this.project_id}"
}

output "oauth_client_create_url" {
  description = <<-EOT
    Console page to create the Desktop-app OAuth client (D-18 step 5),
    scoped to this project. Terraform cannot create OAuth clients (not
    supported by the google provider or by any public Google API).
  EOT
  value       = "https://console.cloud.google.com/auth/clients/create?project=${google_project.this.project_id}"
}
