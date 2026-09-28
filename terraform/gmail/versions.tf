# Terraform for the Google Cloud setup (D-18, D-67).
#
# Local state only (no remote backend): this is a single-operator POC, run
# by hand from a laptop or from a shell in the app's Docker image. State
# lives at terraform/gmail/terraform.tfstate and is committed on purpose for
# this POC; production would use a `backend "gcs"`. See terraform/README.md,
# "Decision: state and tfvars are committed (POC only)".

terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}
