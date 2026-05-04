locals {
  name_prefix      = "${var.project_name}-${var.environment}"
  frontend_bucket  = "${var.project_id}-${local.name_prefix}-frontend"
  memory_bucket    = "${var.project_id}-${local.name_prefix}-memory"
  service_name     = "${local.name_prefix}-api"
  firebase_site_id = var.firebase_site_id != "" ? var.firebase_site_id : local.name_prefix
  firebase_origins = var.enable_firebase_hosting ? "https://${local.firebase_site_id}.web.app,https://${local.firebase_site_id}.firebaseapp.com" : ""
  legacy_origins   = "https://storage.googleapis.com,https://${local.frontend_bucket}.storage.googleapis.com"
  cors_origins     = trim(join(",", compact([local.legacy_origins, local.firebase_origins])), ",")
  labels = {
    project     = var.project_name
    environment = var.environment
    managed_by  = "terraform"
  }
}

resource "google_project_service" "required" {
  for_each = toset(concat(
    [
      "aiplatform.googleapis.com",
      "artifactregistry.googleapis.com",
      "iam.googleapis.com",
      "run.googleapis.com",
    ],
    var.enable_firebase_hosting ? [
      "firebase.googleapis.com",
      "firebasehosting.googleapis.com",
    ] : []
  ))

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_firebase_project" "this" {
  count    = var.enable_firebase_hosting ? 1 : 0
  provider = google-beta
  project  = var.project_id

  depends_on = [google_project_service.required]
}

resource "google_firebase_hosting_site" "this" {
  count    = var.enable_firebase_hosting ? 1 : 0
  provider = google-beta
  project  = var.project_id
  site_id  = local.firebase_site_id

  depends_on = [google_firebase_project.this]
}

resource "google_artifact_registry_repository" "backend" {
  location      = var.region
  repository_id = "${var.project_name}-backend"
  description   = "Digital Twin backend container images"
  format        = "DOCKER"
  labels        = local.labels

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "frontend" {
  count                       = var.keep_legacy_frontend_bucket ? 1 : 0
  name                        = local.frontend_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels

  website {
    main_page_suffix = "index.html"
    not_found_page   = "404.html"
  }
}

moved {
  from = google_storage_bucket.frontend
  to   = google_storage_bucket.frontend[0]
}

resource "google_storage_bucket" "memory" {
  name                        = local.memory_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels
}

resource "google_storage_bucket_iam_member" "frontend_public_read" {
  count  = var.keep_legacy_frontend_bucket ? 1 : 0
  bucket = google_storage_bucket.frontend[0].name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

moved {
  from = google_storage_bucket_iam_member.frontend_public_read
  to   = google_storage_bucket_iam_member.frontend_public_read[0]
}

resource "google_service_account" "cloud_run" {
  account_id   = "${local.name_prefix}-run"
  display_name = "Digital Twin ${var.environment} Cloud Run"
}

resource "google_storage_bucket_iam_member" "memory_object_admin" {
  bucket = google_storage_bucket.memory.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cloud_run.email}"
}

resource "google_project_iam_member" "vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.cloud_run.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name                = local.service_name
  location            = var.region
  deletion_protection = false
  labels              = local.labels

  template {
    service_account = google_service_account.cloud_run.email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      image = var.backend_image

      resources {
        limits = {
          cpu    = var.cloud_run_cpu
          memory = var.cloud_run_memory
        }
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "AI_PROVIDER"
        value = "gemini"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "GEMINI_MODEL_ID"
        value = var.gemini_model_id
      }
      env {
        name  = "USE_GCS"
        value = "true"
      }
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.memory.name
      }
      env {
        name  = "CORS_ORIGINS"
        value = local.cors_origins
      }
      env {
        name  = "PUSHOVER_USER"
        value = var.pushover_user
      }
      env {
        name  = "PUSHOVER_TOKEN"
        value = var.pushover_token
      }
      env {
        name  = "SENDGRID_API_KEY"
        value = var.sendgrid_api_key
      }
      env {
        name  = "SENDGRID_SENDER_EMAIL"
        value = var.sendgrid_sender_email
      }
      env {
        name  = "SENDGRID_RECIPIENT_EMAIL"
        value = var.sendgrid_recipient_email
      }
    }
  }

  depends_on = [
    google_project_iam_member.vertex_user,
    google_storage_bucket_iam_member.memory_object_admin,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
