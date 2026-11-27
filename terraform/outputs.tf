output "shared_bucket_name" {
  description = "Effective single project-scoped GCS bucket name."
  value       = local.effective_bucket_name
}

output "worm_archive_bucket_name" {
  description = "Effective PII-free Gold WORM snapshot archive bucket name (null when disabled)."
  value       = var.create_worm_archive_bucket ? google_storage_bucket.gold_worm_archive[0].name : null
}

output "rtbf_kms_keyring_id" {
  description = "Optional Cloud KMS key ring ID for per-subject RTBF crypto-shredding."
  value       = var.create_rtbf_kms_keyring ? google_kms_key_ring.rtbf_subject_keys[0].id : null
}

output "eval_dataset_id" {
  description = "Effective BigQuery dataset ID for BQ Eval & FinOps."
  value       = local.effective_eval_dataset_id
}

output "eval_internal_dataset_id" {
  description = "Effective hidden BigQuery dataset ID for Gold restricted staging tables."
  value       = local.effective_eval_internal_dataset_id
}

output "vertex_connection_name" {
  description = "Effective BigQuery Cloud Resource connection name."
  value       = local.effective_connection_name
}

output "scheduled_judge_disabled" {
  description = "Whether the daily quota-guarded judge scheduled query is paused (true when power_policy != active)."
  value       = local.scheduled_judge_disabled
}

output "silver_dataset_ids" {
  description = "Provisioned Silver dataset IDs keyed by tier (internal, ref, open, restricted)."
  value       = { for k, ds in google_bigquery_dataset.silver : k => ds.dataset_id }
}

output "shape3_source_uri" {
  description = "Hive-partitioned BigLake Shape-3 payload URI pattern."
  value       = local.shape3_source_uri
}
