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
  description = "Object prefix inside the shared GCS bucket for GenAI completion payloads (must be non-empty)."
  type        = string
  default     = "bq-eval/"
  validation {
    condition     = trim(var.payload_prefix, "/") != "" && !strcontains(var.payload_prefix, "..")
    error_message = "payload_prefix must be non-empty so GCS lifecycle matches_prefix is never unscoped."
  }
}

variable "payload_retention_days" {
  description = "Lifecycle retention age in days applied strictly to payload_prefix objects."
  type        = number
  default     = 90
}

variable "enable_payload_lifecycle_delete" {
  description = "Enable the Delete lifecycle rule on payload_prefix (set false or use a disjoint silver_shape3_prefix when storing WORM/regulated Shape-3 objects)."
  type        = bool
  default     = true
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

variable "manage_default_log_retention" {
  description = "Opt-in flag to override _Default Cloud Logging bucket retention_days (false preserves existing project retention)."
  type        = bool
  default     = false
}

variable "default_log_retention_days" {
  description = "Retention days applied to _Default Cloud Logging bucket only when manage_default_log_retention = true."
  type        = number
  default     = 30
  validation {
    condition     = var.default_log_retention_days >= 1 && var.default_log_retention_days <= 3650
    error_message = "default_log_retention_days must be between 1 and 3650."
  }
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
  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{2,63}$", var.judge_rubric_id))
    error_message = "judge_rubric_id must be a lowercase snake_case identifier (3-64 chars)."
  }
}

variable "judge_canary_sample_pct" {
  description = "Canary sampling percentage passed to run_daily_quota_guarded_judge."
  type        = number
  default     = 5
  validation {
    condition     = var.judge_canary_sample_pct >= 1 && var.judge_canary_sample_pct <= 100 && floor(var.judge_canary_sample_pct) == var.judge_canary_sample_pct
    error_message = "judge_canary_sample_pct must be an integer between 1 and 100."
  }
}

variable "judge_max_output_tokens" {
  description = "Max output token headroom passed to run_daily_quota_guarded_judge."
  type        = number
  default     = 2048
  validation {
    condition     = var.judge_max_output_tokens >= 64 && var.judge_max_output_tokens <= 65536 && floor(var.judge_max_output_tokens) == var.judge_max_output_tokens
    error_message = "judge_max_output_tokens must be an integer between 64 and 65536."
  }
}

variable "judge_thinking_level" {
  description = "Thinking level headroom (LOW, MEDIUM, HIGH) passed to run_daily_quota_guarded_judge."
  type        = string
  default     = "LOW"
  validation {
    condition     = contains(["LOW", "MEDIUM", "HIGH"], var.judge_thinking_level)
    error_message = "judge_thinking_level must be one of: LOW, MEDIUM, HIGH."
  }
}

variable "create_silver_datasets" {
  description = "Provision the four ADR-048 Silver datasets (silver_internal, silver_ref, silver_open, silver_restricted) and Hive BigLake shape3_payloads table."
  type        = bool
  default     = true
}

variable "silver_internal_dataset_id" {
  description = "BigQuery dataset ID for internal Silver spans, JCS UDF, unit_entitlements, and workflow_events copies."
  type        = string
  default     = "silver_internal"
}

variable "silver_ref_dataset_id" {
  description = "BigQuery dataset ID for SCD-2 Silver reference tables (app_registry, tools, skill_registry)."
  type        = string
  default     = "silver_ref"
}

variable "silver_open_dataset_id" {
  description = "BigQuery dataset ID for open text-free Silver views."
  type        = string
  default     = "silver_open"
}

variable "silver_restricted_dataset_id" {
  description = "BigQuery dataset ID for restricted Silver views and Shape-3 BigLake payloads."
  type        = string
  default     = "silver_restricted"
}

variable "silver_shape3_prefix" {
  description = "Hive-partitioned GCS prefix for Shape-3 BigLake payload files (must be non-empty)."
  type        = string
  default     = "bq-eval/shape3/"
  validation {
    condition     = trim(var.silver_shape3_prefix, "/") != "" && !strcontains(var.silver_shape3_prefix, "..")
    error_message = "silver_shape3_prefix must be non-empty and free of '..'."
  }
}

variable "silver_shape3_format" {
  description = "BigLake source format for Shape-3 payloads (PARQUET or NEWLINE_DELIMITED_JSON)."
  type        = string
  default     = "NEWLINE_DELIMITED_JSON"
  validation {
    condition     = contains(["PARQUET", "NEWLINE_DELIMITED_JSON"], var.silver_shape3_format)
    error_message = "silver_shape3_format must be PARQUET or NEWLINE_DELIMITED_JSON."
  }
}

variable "kms_key_name" {
  description = "Optional Cloud KMS key resource name for CMEK encryption on Silver datasets and GCS bucket."
  type        = string
  default     = ""
}

variable "deletion_protection" {
  description = "Protect dataset, table, and bucket contents from accidental destruction (defaults to true)."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Labels applied to provisioned GCP resources."
  type        = map(string)
  default = {
    managed_by = "terraform"
    workload   = "bq-eval"
  }
}
