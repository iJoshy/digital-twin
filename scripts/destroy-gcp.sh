#!/bin/bash
# Destroy the GCP environment (Cloud Run, Artifact Registry, GCS buckets,
# Firebase Hosting site, IAM bindings, and required APIs - the latter only
# from Terraform state, since `disable_on_destroy = false` keeps them enabled
# on the project itself).
#
# Usage:
#   ./scripts/destroy-gcp.sh [dev|test|prod] [project_name]

set -e

ENVIRONMENT=${1:-dev}
PROJECT_NAME=${2:-twin}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

# Locally (or in CI when .env is present), auto-load secrets from .env so the
# user doesn't have to keep two copies. In CI the workflow exports TF_VAR_*
# directly; existing values win via the `${TF_VAR_*:-...}` defaults below.
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi

export TF_VAR_pushover_user="${TF_VAR_pushover_user:-${PUSHOVER_USER:-}}"
export TF_VAR_pushover_token="${TF_VAR_pushover_token:-${PUSHOVER_TOKEN:-}}"
export TF_VAR_sendgrid_api_key="${TF_VAR_sendgrid_api_key:-${SENDGRID_API_KEY:-}}"
export TF_VAR_sendgrid_sender_email="${TF_VAR_sendgrid_sender_email:-${SENDGRID_SENDER_EMAIL:-}}"
export TF_VAR_sendgrid_recipient_email="${TF_VAR_sendgrid_recipient_email:-${SENDGRID_RECIPIENT_EMAIL:-${RECIPIENT_EMAIL:-}}}"

GCP_PROJECT_ID=${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}
GCP_REGION=${GCP_REGION:-europe-west1}

if [ -z "$GCP_PROJECT_ID" ]; then
  echo "GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT is required"
  exit 1
fi

echo "Destroying ${PROJECT_NAME}-${ENVIRONMENT} on GCP (${GCP_PROJECT_ID} / ${GCP_REGION})..."

cd "$REPO_ROOT"

STATE_BUCKET="${GCP_PROJECT_ID}-${PROJECT_NAME}-terraform-state"
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  echo "Terraform state bucket not found (${STATE_BUCKET}); nothing to destroy."
  exit 0
fi

cd terraform-gcp
terraform init -input=false -reconfigure \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="prefix=gcp/${ENVIRONMENT}"

# Empty force_destroy buckets first to give Terraform a clean shot at removing
# them in a single pass (it can also do this on its own, but this is faster
# and surfaces permission issues earlier).
for output_name in frontend_bucket memory_bucket; do
  bucket=$(terraform output -raw "$output_name" 2>/dev/null || echo "")
  if [ -n "$bucket" ] && gcloud storage buckets describe "gs://${bucket}" >/dev/null 2>&1; then
    echo "Emptying gs://${bucket} ..."
    gcloud storage rm --recursive "gs://${bucket}/**" >/dev/null 2>&1 || true
  fi
done

# Cloud Run images can be pushed outside Terraform during deploy; satisfy the
# `backend_image` variable so destroy can run even if the image tag has rotated.
BACKEND_IMAGE_PLACEHOLDER="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${PROJECT_NAME}-backend/${PROJECT_NAME}-${ENVIRONMENT}-api:destroy"

TF_ARGS=(
  -var="project_id=${GCP_PROJECT_ID}"
  -var="project_name=${PROJECT_NAME}"
  -var="environment=${ENVIRONMENT}"
  -var="region=${GCP_REGION}"
  -var="backend_image=${BACKEND_IMAGE_PLACEHOLDER}"
)

if [ -f "${ENVIRONMENT}.tfvars" ]; then
  TF_ARGS=(-var-file="${ENVIRONMENT}.tfvars" "${TF_ARGS[@]}")
fi

terraform destroy "${TF_ARGS[@]}" -auto-approve

echo ""
echo "GCP environment destroyed."
echo "Notes:"
echo "  - Required APIs (run, artifactregistry, aiplatform, firebase, firebasehosting) remain enabled on the project."
echo "  - Firebase Hosting site IDs may stay reserved by Firebase for a short window after deletion."
echo "  - Terraform state bucket gs://${STATE_BUCKET} is preserved; delete manually if you also want to remove state."
