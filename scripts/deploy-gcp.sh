#!/bin/bash
set -e

ENVIRONMENT=${1:-dev}
PROJECT_NAME=${2:-twin}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

# Locally (or in CI when .env is present), auto-load secrets from .env so the
# user doesn't have to keep two copies. In CI the workflow exports TF_VAR_*
# directly, and that already-set value wins via the `${TF_VAR_*:-...}` defaults
# below.
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi

# Map repo-standard env names to the TF_VAR_* names Terraform expects, without
# overriding values already exported by the caller.
export TF_VAR_pushover_user="${TF_VAR_pushover_user:-${PUSHOVER_USER:-}}"
export TF_VAR_pushover_token="${TF_VAR_pushover_token:-${PUSHOVER_TOKEN:-}}"
export TF_VAR_sendgrid_api_key="${TF_VAR_sendgrid_api_key:-${SENDGRID_API_KEY:-}}"
export TF_VAR_sendgrid_sender_email="${TF_VAR_sendgrid_sender_email:-${SENDGRID_SENDER_EMAIL:-}}"
export TF_VAR_sendgrid_recipient_email="${TF_VAR_sendgrid_recipient_email:-${SENDGRID_RECIPIENT_EMAIL:-${RECIPIENT_EMAIL:-}}}"

GCP_PROJECT_ID=${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}
GCP_REGION=${GCP_REGION:-europe-west1}
CONTAINER_PLATFORM=${CONTAINER_PLATFORM:-linux/amd64}
NEXT_PRIVATE_BUILD_WORKER_COUNT=${NEXT_PRIVATE_BUILD_WORKER_COUNT:-1}

if [ -z "$GCP_PROJECT_ID" ]; then
  echo "GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT is required"
  exit 1
fi

echo "Deploying ${PROJECT_NAME} to ${ENVIRONMENT} on GCP..."

cd "$REPO_ROOT"

STATE_BUCKET="${GCP_PROJECT_ID}-${PROJECT_NAME}-terraform-state"
if gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  echo "Terraform state bucket exists: ${STATE_BUCKET}"
else
  echo "Creating Terraform state bucket: ${STATE_BUCKET}"
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="${GCP_PROJECT_ID}" \
    --location="${GCP_REGION}" \
    --uniform-bucket-level-access
fi

cd terraform-gcp
terraform init -input=false \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="prefix=gcp/${ENVIRONMENT}"

TF_ARGS=(
  -var="project_id=${GCP_PROJECT_ID}"
  -var="project_name=${PROJECT_NAME}"
  -var="environment=${ENVIRONMENT}"
  -var="region=${GCP_REGION}"
)

if [ -f "${ENVIRONMENT}.tfvars" ]; then
  TF_ARGS=(-var-file="${ENVIRONMENT}.tfvars" "${TF_ARGS[@]}")
fi

BOOTSTRAP_IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${PROJECT_NAME}-backend/${PROJECT_NAME}-${ENVIRONMENT}-api:bootstrap"
terraform apply \
  -target=google_project_service.required \
  -target=google_artifact_registry_repository.backend \
  "${TF_ARGS[@]}" \
  -var="backend_image=${BOOTSTRAP_IMAGE}" \
  -auto-approve

gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev" --quiet

IMAGE_TAG=${GITHUB_SHA:-$(date +%Y%m%d%H%M%S)}
IMAGE_URI="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${PROJECT_NAME}-backend/${PROJECT_NAME}-${ENVIRONMENT}-api:${IMAGE_TAG}"
SERVICE_NAME="${PROJECT_NAME}-${ENVIRONMENT}-api"

cd ..
docker build --platform "${CONTAINER_PLATFORM}" -t "${IMAGE_URI}" backend
docker push "${IMAGE_URI}"

cd terraform-gcp
# A failed first Cloud Run revision can leave the service tainted in Terraform state.
# Cloud Run image/env updates are in-place, so untaint before applying the fixed image.
terraform untaint google_cloud_run_v2_service.api >/dev/null 2>&1 || true

terraform apply \
  "${TF_ARGS[@]}" \
  -var="backend_image=${IMAGE_URI}" \
  -auto-approve

API_URL=$(terraform output -raw cloud_run_url)
FRONTEND_BUCKET=$(terraform output -raw frontend_bucket 2>/dev/null || echo "")
FRONTEND_URL=$(terraform output -raw frontend_url)
MEMORY_BUCKET=$(terraform output -raw memory_bucket)
FIREBASE_SITE_ID=$(terraform output -raw firebase_site_id 2>/dev/null || echo "")
FIREBASE_URL=$(terraform output -raw firebase_url 2>/dev/null || echo "")
LEGACY_FRONTEND_URL=$(terraform output -raw legacy_frontend_url 2>/dev/null || echo "")

if [ -z "$API_URL" ]; then
  API_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --project="${GCP_PROJECT_ID}" \
    --region="${GCP_REGION}" \
    --format="value(status.url)")
fi

if [ -z "$API_URL" ]; then
  echo "Cloud Run URL is empty; aborting frontend build to avoid deploying a broken API URL."
  exit 1
fi

CLOUD_RUN_SERVICE_ACCOUNT=$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --format="value(spec.template.spec.serviceAccountName)")

if [ -z "$CLOUD_RUN_SERVICE_ACCOUNT" ]; then
  CLOUD_RUN_SERVICE_ACCOUNT="${PROJECT_NAME}-${ENVIRONMENT}-run@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
fi

echo "Ensuring Cloud Run can read and write conversation memory..."
gcloud storage buckets add-iam-policy-binding "gs://${MEMORY_BUCKET}" \
  --member="serviceAccount:${CLOUD_RUN_SERVICE_ACCOUNT}" \
  --role="roles/storage.objectAdmin" \
  >/dev/null

cd ../frontend
echo "NEXT_PUBLIC_API_URL=${API_URL}" > .env.production
npm install --no-audit --fund=false
NEXT_PRIVATE_BUILD_WORKER_COUNT="${NEXT_PRIVATE_BUILD_WORKER_COUNT}" npm run build

if [ -n "$FRONTEND_BUCKET" ]; then
  for attempt in 1 2 3; do
    if gcloud storage rsync --recursive --delete-unmatched-destination-objects ./out "gs://${FRONTEND_BUCKET}"; then
      break
    fi

    if [ "$attempt" = "3" ]; then
      echo "Frontend sync to legacy GCS bucket failed after ${attempt} attempts."
      exit 1
    fi

    echo "Frontend sync failed, retrying in 10 seconds..."
    read -r -t 10 _ || true
  done
else
  echo "Skipping legacy GCS frontend sync (frontend_bucket output is empty)."
fi

if [ -n "$FIREBASE_SITE_ID" ]; then
  echo "Deploying frontend to Firebase Hosting site: ${FIREBASE_SITE_ID}"

  cat > .firebaserc <<EOF
{
  "projects": {
    "default": "${GCP_PROJECT_ID}"
  },
  "targets": {
    "${GCP_PROJECT_ID}": {
      "hosting": {
        "default": ["${FIREBASE_SITE_ID}"]
      }
    }
  }
}
EOF

  npx --yes firebase-tools@latest deploy \
    --project "${GCP_PROJECT_ID}" \
    --only "hosting:default" \
    --non-interactive
else
  echo "Skipping Firebase Hosting deploy (firebase_site_id output is empty)."
fi

echo ""
echo "GCP deployment complete"
if [ -n "$FIREBASE_URL" ]; then
  echo "Frontend URL: ${FIREBASE_URL}"
fi
if [ -n "$LEGACY_FRONTEND_URL" ]; then
  echo "Legacy frontend URL: ${LEGACY_FRONTEND_URL}"
fi
echo "Cloud Run API: ${API_URL}"
if [ -n "$FRONTEND_BUCKET" ]; then
  echo "Frontend bucket (legacy): ${FRONTEND_BUCKET}"
fi
