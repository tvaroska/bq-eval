output "shared_bucket_name" {
  description = "Effective single project-scoped GCS bucket name."
  value       = local.effective_bucket_name
}

output "eval_dataset_id" {
  description = "Effective BigQuery dataset ID for BQ Eval & FinOps."
  value       = local.effective_eval_dataset_id
}

output "vertex_connection_name" {
  description = "Effective BigQuery Cloud Resource connection name."
  value       = local.effective_connection_name
}

output "completions_source_uri" {
  description = "BigLake external table source URI pattern under the shared bucket prefix."
  value       = local.completions_source_uri
}

output "completions_table_fqn" {
  description = "Fully qualified BigLake completions table name."
  value       = "${var.project_id}.${local.effective_eval_dataset_id}.${google_bigquery_table.completions.table_id}"
}

output "scheduled_judge_disabled" {
  description = "Whether the daily quota-guarded judge scheduled query is paused (true when power_policy = hibernate)."
  value       = local.scheduled_judge_disabled
}
