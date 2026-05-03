#!/bin/bash
set -e

ENVIRONMENT=${1:-dev}
PROJECT_NAME=${2:-twin}
GCP_PROJECT_ID=${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}
GCP_REGION=${GCP_REGION:-us-central1}
CONTAINER_PLATFORM=${CONTAINER_PLATFORM:-linux/amd64}
NEXT_PRIVATE_BUILD_WORKER_COUNT=${NEXT_PRIVATE_BUILD_WORKER_COUNT:-1}

if [ -z "$GCP_PROJECT_ID" ]; then
  echo "GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT is required"
  exit 1
fi

echo "Deploying ${PROJECT_NAME} to ${ENVIRONMENT} on GCP..."

cd "$(dirname "$0")/.."

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
FRONTEND_BUCKET=$(terraform output -raw frontend_bucket)
FRONTEND_URL=$(terraform output -raw frontend_url)
MEMORY_BUCKET=$(terraform output -raw memory_bucket)

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

for attempt in 1 2 3; do
  if gcloud storage rsync --recursive --delete-unmatched-destination-objects ./out "gs://${FRONTEND_BUCKET}"; then
    break
  fi

  if [ "$attempt" = "3" ]; then
    echo "Frontend sync failed after ${attempt} attempts."
    exit 1
  fi

  echo "Frontend sync failed, retrying in 10 seconds..."
  read -r -t 10 _ || true
done

echo ""
echo "GCP deployment complete"
echo "Frontend URL: ${FRONTEND_URL}"
echo "Cloud Run API: ${API_URL}"
echo "Frontend bucket: ${FRONTEND_BUCKET}"
