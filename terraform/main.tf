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
  base_apis = [
    "bigquery.googleapis.com",
    "bigqueryconnection.googleapis.com",
    "bigquerydatatransfer.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "logging.googleapis.com",
    "observability.googleapis.com",
    "telemetry.googleapis.com",
  ]
  required_apis = toset(
    var.create_rtbf_kms_keyring ? concat(local.base_apis, ["cloudkms.googleapis.com"]) : local.base_apis
  )

  resolved_bucket_name = var.shared_bucket_name != "" ? var.shared_bucket_name : var.project_id
  resolved_worm_bucket_name = (
    var.worm_archive_bucket_name != ""
    ? var.worm_archive_bucket_name
    : "${local.resolved_bucket_name}-worm"
  )

  trimmed_prefix    = trim(var.payload_prefix, "/")
  normalized_prefix = "${local.trimmed_prefix}/"
  trimmed_shape3    = trim(var.silver_shape3_prefix, "/")
  normalized_shape3 = "${local.trimmed_shape3}/"
  shape3_in_payload = startswith(local.normalized_shape3, local.normalized_prefix)
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

  shape3_hive_prefix = "gs://${local.effective_bucket_name}/${local.normalized_shape3}"
  shape3_source_uri  = "${local.shape3_hive_prefix}*"

  silver_datasets = {
    internal   = { id = var.silver_internal_dataset_id, desc = "Silver internal spans, JCS UDF, unit_entitlements, and workflow_events copies." }
    ref        = { id = var.silver_ref_dataset_id, desc = "Silver SCD-2 reference tables (app_registry, tools, skill_registry, capacity pools, allowed_models)." }
    open       = { id = var.silver_open_dataset_id, desc = "Silver open text-free published TVFs, requests, burst_metrics_1m, and wrapper views." }
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

resource "google_storage_bucket" "gold_worm_archive" {
  count = var.create_worm_archive_bucket ? 1 : 0

  project                     = var.project_id
  name                        = local.resolved_worm_bucket_name
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection
  labels                      = local.effective_labels

  retention_policy {
    retention_period = var.worm_retention_seconds
    is_locked        = var.lock_worm_retention
  }

  dynamic "encryption" {
    for_each = var.kms_key_name != "" ? [1] : []
    content {
      default_kms_key_name = var.kms_key_name
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_kms_key_ring" "rtbf_subject_keys" {
  count = var.create_rtbf_kms_keyring ? 1 : 0

  project  = var.project_id
  name     = "bq-eval-rtbf-subjects"
  location = var.region

  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "rtbf_subject_dek_template" {
  count = var.create_rtbf_kms_keyring ? 1 : 0

  name                       = "subject-dek-template"
  key_ring                   = google_kms_key_ring.rtbf_subject_keys[0].id
  rotation_period            = "7776000s"
  destroy_scheduled_duration = "86400s"
  labels                     = local.effective_labels
}

resource "google_bigquery_dataset" "agent_eval" {
  count = var.create_eval_dataset ? 1 : 0

  project                    = var.project_id
  dataset_id                 = var.eval_dataset_id
  friendly_name              = "BQ Eval & FinOps (Gold)"
  description                = "BigQuery Gold Evaluation, MRM Calibration, WORM Audit, and CPVO FinOps over Silver."
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
  description   = "Cloud Resource connection for Shape-3 BigLake payload tables and AI.GENERATE_TABLE judges."

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
  description         = "Hive-partitioned BigLake external table over Shape-3 content-by-reference payloads."
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
  description = "Cloud Logging _Default analytics linked dataset (_AllLogs) for gateway requests and events."
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
