output "artifact_registry_repository" {
  description = "Artifact Registry repository name"
  value       = google_artifact_registry_repository.backend.repository_id
}

output "artifact_registry_location" {
  description = "Artifact Registry location"
  value       = google_artifact_registry_repository.backend.location
}

output "cloud_run_url" {
  description = "Cloud Run backend URL"
  value       = google_cloud_run_v2_service.api.uri
}

output "frontend_bucket" {
  description = "Cloud Storage bucket for static frontend (legacy, may be empty when disabled)"
  value       = var.keep_legacy_frontend_bucket ? google_storage_bucket.frontend[0].name : ""
}

output "frontend_url" {
  description = "Public frontend URL"
  value = (
    var.enable_firebase_hosting
    ? "https://${local.firebase_site_id}.web.app"
    : (var.keep_legacy_frontend_bucket ? "https://${google_storage_bucket.frontend[0].name}.storage.googleapis.com/index.html" : "")
  )
}

output "legacy_frontend_url" {
  description = "Legacy GCS-hosted frontend URL (kept during migration)"
  value       = var.keep_legacy_frontend_bucket ? "https://${google_storage_bucket.frontend[0].name}.storage.googleapis.com/index.html" : ""
}

output "firebase_site_id" {
  description = "Firebase Hosting site ID"
  value       = var.enable_firebase_hosting ? local.firebase_site_id : ""
}

output "firebase_url" {
  description = "Firebase Hosting URL"
  value       = var.enable_firebase_hosting ? "https://${local.firebase_site_id}.web.app" : ""
}

output "memory_bucket" {
  description = "Cloud Storage bucket for conversation memory"
  value       = google_storage_bucket.memory.name
}
