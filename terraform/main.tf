# Standalone & Co-Located Single-Project Terraform Module for BQ Eval & FinOps (M10-2).
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
    "observability.googleapis.com",
  ])

  resolved_bucket_name = var.shared_bucket_name != "" ? var.shared_bucket_name : var.project_id

  trimmed_prefix    = trim(var.payload_prefix, "/")
  normalized_prefix = local.trimmed_prefix != "" ? "${local.trimmed_prefix}/" : ""

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

  effective_labels = merge(var.labels, {
    power_policy = var.power_policy
  })

  scheduled_judge_disabled = var.power_policy == "hibernate" || !var.enable_scheduled_judge

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

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age            = var.payload_retention_days
      matches_prefix = local.normalized_prefix != "" ? [local.normalized_prefix] : []
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
      expression  = "resource.name.startsWith(\"projects/_/buckets/${local.effective_bucket_name}/objects/${local.normalized_prefix}\")"
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

resource "google_bigquery_data_transfer_config" "daily_quota_guarded_judge" {
  count = var.enable_scheduled_judge ? 1 : 0

  project                = var.project_id
  display_name           = "bq-eval-daily-quota-guarded-judge"
  location               = var.dataset_location
  data_source_id         = "scheduled_query"
  schedule               = var.judge_schedule
  disabled               = local.scheduled_judge_disabled
  destination_dataset_id = local.effective_eval_dataset_id

  params = {
    query = "CALL `${var.project_id}.${local.effective_eval_dataset_id}.run_daily_quota_guarded_judge`('${var.judge_rubric_id}', ${var.judge_canary_sample_pct}, ${var.judge_max_output_tokens}, '${var.judge_thinking_level}', 24, '');"
  }
}
