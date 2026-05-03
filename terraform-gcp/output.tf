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
  description = "Cloud Storage bucket for static frontend"
  value       = google_storage_bucket.frontend.name
}

output "frontend_url" {
  description = "Public static frontend URL"
  value       = "https://${google_storage_bucket.frontend.name}.storage.googleapis.com/index.html"
}

output "memory_bucket" {
  description = "Cloud Storage bucket for conversation memory"
  value       = google_storage_bucket.memory.name
}
