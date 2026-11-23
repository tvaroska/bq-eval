# Credential-free plan-time verification of standalone cold-project defaults (create_* = true).

mock_provider "google" {}
mock_provider "google-beta" {}

run "standalone_cold_project_defaults" {
  command = plan

  variables {
    project_id = "demo-bq-eval-cold"
    region     = "us-central1"
  }

  assert {
    condition     = length(google_storage_bucket.shared) == 1 && length(data.google_storage_bucket.shared) == 0
    error_message = "Standalone mode must create 1 shared GCS bucket and 0 data lookups."
  }

  assert {
    condition     = length(google_bigquery_dataset.agent_eval) == 1 && length(data.google_bigquery_dataset.agent_eval) == 0
    error_message = "Standalone mode must create 1 agent_eval dataset and 0 data lookups."
  }

  assert {
    condition     = length(google_bigquery_connection.vertex_ai) == 1
    error_message = "Standalone mode must create 1 Vertex AI Cloud Resource connection."
  }

  assert {
    condition = (
      google_storage_bucket.shared[0].name == "demo-bq-eval-cold"
      && google_storage_bucket.shared[0].uniform_bucket_level_access == true
      && google_storage_bucket.shared[0].public_access_prevention == "enforced"
      && google_storage_bucket.shared[0].labels["power_policy"] == "hibernate"
    )
    error_message = "Shared bucket must enforce UBLA, PAP, project-id naming, and power_policy label."
  }

  assert {
    condition     = output.completions_source_uri == "gs://demo-bq-eval-cold/bq-eval/*.jsonl"
    error_message = "BigLake completions table must point to gs://<project_id>/bq-eval/*.jsonl."
  }

  assert {
    condition     = google_storage_bucket_iam_member.biglake_viewer[0].condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/demo-bq-eval-cold/objects/bq-eval/\")"
    error_message = "BigLake viewer IAM binding must be prefix-scoped via CEL condition."
  }

  assert {
    condition     = google_bigquery_data_transfer_config.daily_quota_guarded_judge[0].disabled == true && output.scheduled_judge_disabled == true
    error_message = "Scheduled judge query must pause automatically under power_policy = hibernate."
  }
}

run "standalone_active_enables_scheduled_judge" {
  command = plan

  variables {
    project_id   = "demo-bq-eval-active"
    power_policy = "active"
  }

  assert {
    condition     = google_bigquery_data_transfer_config.daily_quota_guarded_judge[0].disabled == false && output.scheduled_judge_disabled == false
    error_message = "Scheduled judge query must activate when power_policy = active."
  }
}
