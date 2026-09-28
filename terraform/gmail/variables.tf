# D-18: the project is created under "No organization", with no billing
# account. There is deliberately no `region` variable (project/API
# resources here are global) and no org/folder id variable.

variable "project_id" {
  description = <<-EOT
    Globally-unique Google Cloud project id for the dedicated Gmail
    account (D-18). Project ids are permanent and can never be reused,
    even after the project is deleted, so pick something distinctive
    (set in terraform.tfvars).
  EOT
  type        = string

  validation {
    # GCP project id rules: 6-30 characters, lowercase letters, digits and
    # hyphens only; must start with a lowercase letter; must not end with
    # a hyphen.
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6-30 characters: lowercase letters, digits and hyphens, starting with a lowercase letter and not ending with a hyphen."
  }
}

variable "project_name" {
  description = "Human-readable display name for the project (shown in the Cloud Console; does not need to be unique)."
  type        = string
  default     = "dt-agent"
}
