variable "project_id" {
  description = "Target GCP project ID for BQ Eval & FinOps."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid 6-30 character lowercase GCP project ID."
  }
}

variable "region" {
  description = "Primary GCP region for single-bucket storage and regional resources."
  type        = string
  default     = "us-central1"
}

variable "dataset_location" {
  description = "BigQuery location for the evaluation dataset and scheduled queries (e.g., US or us-central1)."
  type        = string
  default     = "US"
}

variable "eval_dataset_id" {
  description = "BigQuery dataset ID hosting agent_eval tables and views."
  type        = string
  default     = "agent_eval"
}

variable "manage_apis" {
  description = "Whether this module enables required Google Cloud APIs (set false when APIs are managed externally)."
  type        = bool
  default     = true
}

variable "create_shared_bucket" {
  description = "Create the single project-scoped GCS bucket (gs://<project_id>) when true; adopt existing via data lookup when false."
  type        = bool
  default     = true
}

variable "shared_bucket_name" {
  description = "Override GCS bucket name (defaults to var.project_id per single-bucket-per-project convention)."
  type        = string
  default     = ""
}

variable "payload_prefix" {
  description = "Object prefix inside the shared GCS bucket for GenAI completion payloads."
  type        = string
  default     = "bq-eval/"
}

variable "payload_retention_days" {
  description = "Lifecycle retention age in days applied strictly to payload_prefix objects."
  type        = number
  default     = 90
}

variable "create_eval_dataset" {
  description = "Create the agent_eval BigQuery dataset when true; adopt existing via data lookup when false."
  type        = bool
  default     = true
}

variable "create_vertex_connection" {
  description = "Create the BigQuery Cloud Resource connection for Vertex AI / BigLake when true; reuse existing when false."
  type        = bool
  default     = true
}

variable "vertex_connection_id" {
  description = "BigQuery Cloud Resource connection ID."
  type        = string
  default     = "vertex_ai_connection"
}

variable "vertex_connection_location" {
  description = "BigQuery location of the Cloud Resource connection."
  type        = string
  default     = "us"
}

variable "vertex_connection_service_account_email" {
  description = "Existing BigQuery connection service account email when create_vertex_connection = false."
  type        = string
  default     = ""
}

variable "create_shared_bucket_lister_role" {
  description = "Create minimal custom storage.objects.list role when true; reuse shared_bucket_lister_role_name when false."
  type        = bool
  default     = true
}

variable "create_logging_link" {
  description = "Enable _Default log analytics and create the obs_logs linked BigQuery dataset when true (set false when co-located stack already manages obs_logs)."
  type        = bool
  default     = true
}

variable "judge_transfer_service_account_email" {
  description = "Optional service account email used by BigQuery Data Transfer scheduled judge runs in non-interactive environments."
  type        = string
  default     = ""
}

variable "shared_bucket_lister_role_name" {
  description = "Existing custom storage.objects.list role resource name when create_shared_bucket_lister_role = false."
  type        = string
  default     = ""
}

variable "power_policy" {
  description = "Lifecycle hibernation policy (hibernate pauses scheduled judge queries at rest)."
  type        = string
  default     = "hibernate"
  validation {
    condition     = contains(["hibernate", "active", "scale-to-zero"], var.power_policy)
    error_message = "power_policy must be one of: hibernate, active, scale-to-zero."
  }
}

variable "enable_scheduled_judge" {
  description = "Provision the daily quota-guarded BigQuery scheduled query."
  type        = bool
  default     = true
}

variable "judge_schedule" {
  description = "BigQuery Data Transfer schedule expression for quota-guarded daily judging."
  type        = string
  default     = "every 24 hours"
}

variable "judge_rubric_id" {
  description = "Default 4-point ordinal rubric ID executed by the daily quota-guarded procedure."
  type        = string
  default     = "groundedness_4pt_v1"
}

variable "judge_canary_sample_pct" {
  description = "Canary sampling percentage passed to run_daily_quota_guarded_judge."
  type        = number
  default     = 5
}

variable "judge_max_output_tokens" {
  description = "Max output token headroom passed to run_daily_quota_guarded_judge."
  type        = number
  default     = 2048
}

variable "judge_thinking_level" {
  description = "Thinking level headroom (LOW, MEDIUM, HIGH) passed to run_daily_quota_guarded_judge."
  type        = string
  default     = "LOW"
}

variable "deletion_protection" {
  description = "Protect dataset and bucket contents from accidental destruction."
  type        = bool
  default     = false
}

variable "labels" {
  description = "Labels applied to provisioned GCP resources."
  type        = map(string)
  default = {
    managed_by = "terraform"
    workload   = "bq-eval"
  }
}
