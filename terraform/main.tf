# Standalone & Co-Located Single-Project Terraform Module for BQ Eval & FinOps.
# Defaults install standalone on a cold GCP project (create_* = true); setting
# create_shared_bucket = false, create_eval_dataset = false, and
# create_vertex_connection = false adopts existing co-located showcase resources.

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  required_apis = toset([
    "bigquery.googleapis.com",
    "bigqueryconnection.googleapis.com",
    "bigquerydatatransfer.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "logging.googleapis.com",
    "observability.googleapis.com",
    "telemetry.googleapis.com",
  ])

  resolved_bucket_name = var.shared_bucket_name != "" ? var.shared_bucket_name : var.project_id

  trimmed_prefix       = trim(var.payload_prefix, "/")
  normalized_prefix    = "${local.trimmed_prefix}/"
  trimmed_shape3       = trim(var.silver_shape3_prefix, "/")
  normalized_shape3    = "${local.trimmed_shape3}/"
  shape3_in_payload    = startswith(local.normalized_shape3, local.normalized_prefix)
  biglake_viewer_expr = (
    var.create_silver_datasets && !local.shape3_in_payload
    ? "resource.name.startsWith(\"projects/_/buckets/${local.effective_bucket_name}/objects/${local.normalized_prefix}\") || resource.name.startsWith(\"projects/_/buckets/${local.effective_bucket_name}/objects/${local.normalized_shape3}\")"
    : "resource.name.startsWith(\"projects/_/buckets/${local.effective_bucket_name}/objects/${local.normalized_prefix}\")"
  )

  effective_bucket_name = (
    var.create_shared_bucket
    ? google_storage_bucket.shared[0].name
    : data.google_storage_bucket.shared[0].name
  )

  effective_eval_dataset_id = (
    var.create_eval_dataset
    ? google_bigquery_dataset.agent_eval[0].dataset_id
    : data.google_bigquery_dataset.agent_eval[0].dataset_id
  )

  effective_connection_name = (
    var.create_vertex_connection
    ? google_bigquery_connection.vertex_ai[0].name
    : "projects/${var.project_id}/locations/${var.vertex_connection_location}/connections/${var.vertex_connection_id}"
  )

  effective_connection_sa = (
    var.create_vertex_connection
    ? google_bigquery_connection.vertex_ai[0].cloud_resource[0].service_account_id
    : var.vertex_connection_service_account_email
  )

  completions_source_uri = "gs://${local.effective_bucket_name}/${local.normalized_prefix}*.jsonl"
  shape3_hive_prefix     = "gs://${local.effective_bucket_name}/${local.normalized_shape3}"
  shape3_source_uri      = "${local.shape3_hive_prefix}*"

  silver_datasets = {
    internal   = { id = var.silver_internal_dataset_id, desc = "Silver internal spans, JCS UDF, unit_entitlements, and workflow_events copies." }
    ref        = { id = var.silver_ref_dataset_id, desc = "Silver SCD-2 reference tables (app_registry, tools, skill_registry)." }
    open       = { id = var.silver_open_dataset_id, desc = "Silver open text-free published TVFs and wrapper views." }
    restricted = { id = var.silver_restricted_dataset_id, desc = "Silver restricted caller-filtered TVFs and Shape-3 BigLake payloads." }
  }

  effective_labels = merge(var.labels, {
    power_policy = var.power_policy
  })

  scheduled_judge_disabled = var.power_policy != "active" || !var.enable_scheduled_judge

  effective_lister_role = (
    var.create_shared_bucket_lister_role
    ? google_project_iam_custom_role.biglake_lister[0].name
    : var.shared_bucket_lister_role_name
  )
}

resource "google_project_service" "apis" {
  for_each = var.manage_apis ? local.required_apis : toset([])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "shared" {
  count = var.create_shared_bucket ? 1 : 0

  project                     = var.project_id
  name                        = local.resolved_bucket_name
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection
  labels                      = local.effective_labels

  versioning {
    enabled = true
  }

  dynamic "encryption" {
    for_each = var.kms_key_name != "" ? [1] : []
    content {
      default_kms_key_name = var.kms_key_name
    }
  }

  dynamic "lifecycle_rule" {
    for_each = var.enable_payload_lifecycle_delete ? [1] : []
    content {
      action {
        type = "Delete"
      }
      condition {
        age            = var.payload_retention_days
        matches_prefix = [local.normalized_prefix]
      }
    }
  }
}

data "google_storage_bucket" "shared" {
  count = var.create_shared_bucket ? 0 : 1

  name = local.resolved_bucket_name
}

resource "google_bigquery_dataset" "agent_eval" {
  count = var.create_eval_dataset ? 1 : 0

  project                    = var.project_id
  dataset_id                 = var.eval_dataset_id
  friendly_name              = "BQ Eval & FinOps"
  description                = "BigQuery Agent Evaluation, Dual-Path Telemetry Views, Calibration, and CPVO FinOps."
  location                   = var.dataset_location
  delete_contents_on_destroy = !var.deletion_protection
  labels                     = local.effective_labels

  depends_on = [google_project_service.apis]
}

data "google_bigquery_dataset" "agent_eval" {
  count = var.create_eval_dataset ? 0 : 1

  project    = var.project_id
  dataset_id = var.eval_dataset_id
}

resource "google_bigquery_connection" "vertex_ai" {
  count = var.create_vertex_connection ? 1 : 0

  project       = var.project_id
  location      = var.vertex_connection_location
  connection_id = var.vertex_connection_id
  friendly_name = "BQ Eval Vertex AI & BigLake Connection"
  description   = "Cloud Resource connection for BigLake completion tables and AI.GENERATE_TABLE judges."

  cloud_resource {}

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "vertex_ai_user" {
  count = var.create_vertex_connection ? 1 : 0

  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_bigquery_connection.vertex_ai[0].cloud_resource[0].service_account_id}"
}

resource "google_project_iam_custom_role" "biglake_lister" {
  count = var.create_shared_bucket_lister_role && (var.create_vertex_connection || var.vertex_connection_service_account_email != "") ? 1 : 0

  project     = var.project_id
  role_id     = "bq_eval_shared_bucket_lister"
  title       = "BQ Eval Shared Bucket Lister"
  description = "Minimal storage.objects.list role for BigLake single-bucket prefix queries."
  permissions = ["storage.objects.list"]
}

resource "google_storage_bucket_iam_member" "biglake_lister" {
  count = (var.create_vertex_connection || var.vertex_connection_service_account_email != "") && (var.create_shared_bucket_lister_role || var.shared_bucket_lister_role_name != "") ? 1 : 0

  bucket = local.effective_bucket_name
  role   = local.effective_lister_role
  member = "serviceAccount:${local.effective_connection_sa}"
}

resource "google_storage_bucket_iam_member" "biglake_viewer" {
  count = var.create_vertex_connection || var.vertex_connection_service_account_email != "" ? 1 : 0

  bucket = local.effective_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${local.effective_connection_sa}"

  dynamic "condition" {
    for_each = local.normalized_prefix != "" ? [1] : []
    content {
      title       = "bq_eval_prefix_biglake_viewer"
      description = "Scope BigLake Object Table viewer reads strictly to the configured payload prefix."
      expression  = local.biglake_viewer_expr
    }
  }
}

resource "google_bigquery_table" "completions" {
  project             = var.project_id
  dataset_id          = local.effective_eval_dataset_id
  table_id            = "completions"
  description         = "Dual-schema BigLake external table over GenAI prompt/context/completion JSONL objects."
  deletion_protection = var.deletion_protection
  labels              = local.effective_labels

  schema = jsonencode([
    { name = "payload", type = "JSON", mode = "NULLABLE", description = "Uploaded OTel GenAI prompt/context/completion JSON payload." },
    { name = "user_prompt", type = "STRING", mode = "NULLABLE", description = "Optional top-level user prompt text." },
    { name = "retrieved_context", type = "STRING", mode = "NULLABLE", description = "Optional top-level retrieved context." },
    { name = "final_response", type = "STRING", mode = "NULLABLE", description = "Optional top-level final response." }
  ])

  external_data_configuration {
    autodetect            = false
    ignore_unknown_values = true
    source_format         = "NEWLINE_DELIMITED_JSON"
    source_uris           = [local.completions_source_uri]
    connection_id         = local.effective_connection_name
  }

  depends_on = [
    google_storage_bucket_iam_member.biglake_viewer,
    google_storage_bucket_iam_member.biglake_lister,
  ]
}

resource "google_bigquery_dataset" "silver" {
  for_each = var.create_silver_datasets ? local.silver_datasets : {}

  project                    = var.project_id
  dataset_id                 = each.value.id
  friendly_name              = "BQ Eval Silver (${each.key})"
  description                = each.value.desc
  location                   = var.dataset_location
  delete_contents_on_destroy = !var.deletion_protection
  labels                     = local.effective_labels

  dynamic "default_encryption_configuration" {
    for_each = var.kms_key_name != "" ? [1] : []
    content {
      kms_key_name = var.kms_key_name
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_bigquery_table" "shape3_payloads" {
  count = var.create_silver_datasets ? 1 : 0

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.silver["restricted"].dataset_id
  table_id            = "shape3_payloads"
  description         = "Hive-partitioned BigLake external table over Shape-3 content-by-reference payloads (alongside legacy completions until M19-2)."
  deletion_protection = var.deletion_protection
  labels              = local.effective_labels

  schema = jsonencode([
    { name = "ref_uri", type = "STRING", mode = "NULLABLE", description = "Canonical storage URI referenced by gen_ai.*.messages_ref." },
    { name = "payload", type = "JSON", mode = "NULLABLE", description = "Stored OTel GenAI message array payload." }
  ])

  external_data_configuration {
    autodetect            = false
    ignore_unknown_values = true
    source_format         = var.silver_shape3_format
    source_uris           = [local.shape3_source_uri]
    connection_id         = local.effective_connection_name

    hive_partitioning_options {
      mode                     = "CUSTOM"
      source_uri_prefix        = "${local.shape3_hive_prefix}{dt:DATE}/{hr:STRING}"
      require_partition_filter = true
    }
  }

  depends_on = [
    google_storage_bucket_iam_member.biglake_viewer,
    google_storage_bucket_iam_member.biglake_lister,
  ]
}

resource "google_logging_project_bucket_config" "default_analytics" {
  count = var.create_logging_link ? 1 : 0

  project          = var.project_id
  location         = "global"
  bucket_id        = "_Default"
  enable_analytics = true
  retention_days   = var.manage_default_log_retention ? var.default_log_retention_days : null

  depends_on = [google_project_service.apis]
}

resource "google_logging_linked_dataset" "obs_logs" {
  count = var.create_logging_link ? 1 : 0

  parent      = "projects/${var.project_id}"
  location    = "global"
  bucket      = google_logging_project_bucket_config.default_analytics[0].bucket_id
  link_id     = "obs_logs"
  description = "Cloud Logging _Default analytics linked dataset (_AllLogs) for GenAI completion events."
}

resource "google_bigquery_data_transfer_config" "daily_quota_guarded_judge" {
  count = var.enable_scheduled_judge ? 1 : 0

  project                = var.project_id
  display_name           = "bq-eval-daily-quota-guarded-judge"
  location               = var.dataset_location
  data_source_id         = "scheduled_query"
  schedule               = var.judge_schedule
  disabled               = local.scheduled_judge_disabled
  destination_dataset_id = local.effective_eval_dataset_id
  service_account_name   = var.judge_transfer_service_account_email != "" ? var.judge_transfer_service_account_email : null

  params = {
    query = "CALL `${var.project_id}.${local.effective_eval_dataset_id}.run_daily_quota_guarded_judge`('${var.judge_rubric_id}', ${var.judge_canary_sample_pct}, ${var.judge_max_output_tokens}, '${var.judge_thinking_level}', 24, '');"
  }
}
