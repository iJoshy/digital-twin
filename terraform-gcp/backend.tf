terraform {
  backend "gcs" {
    # Set by scripts/deploy-gcp.sh:
    # bucket = "<project>-twin-terraform-state"
    # prefix = "gcp/<environment>"
  }
}
