variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "project_name" {
  description = "Name prefix for resources"
  type        = string
  default     = "twin"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Environment must be one of: dev, test, prod."
  }
}

variable "region" {
  description = "GCP region for Cloud Run and Vertex AI"
  type        = string
  default     = "us-central1"
}

variable "backend_image" {
  description = "Container image URI for the Cloud Run backend"
  type        = string
}

variable "gemini_model_id" {
  description = "Vertex AI Gemini model ID"
  type        = string
  default     = "gemini-2.5-flash-lite"
}

variable "cloud_run_cpu" {
  description = "Cloud Run CPU limit"
  type        = string
  default     = "1"
}

variable "cloud_run_memory" {
  description = "Cloud Run memory limit"
  type        = string
  default     = "512Mi"
}

variable "pushover_user" {
  description = "Pushover user key"
  type        = string
  default     = ""
  sensitive   = true
}

variable "pushover_token" {
  description = "Pushover app token"
  type        = string
  default     = ""
  sensitive   = true
}

variable "sendgrid_api_key" {
  description = "SendGrid API key"
  type        = string
  default     = ""
  sensitive   = true
}

variable "sendgrid_sender_email" {
  description = "SendGrid verified sender email"
  type        = string
  default     = ""
}

variable "sendgrid_recipient_email" {
  description = "Owner recipient email for follow-up notifications"
  type        = string
  default     = ""
}
