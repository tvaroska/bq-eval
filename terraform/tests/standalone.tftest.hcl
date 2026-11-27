# Credential-free plan-time verification of standalone cold-project defaults (create_* = true).

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "time" {}

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
    condition     = length(google_storage_bucket.gold_worm_archive) == 1 && google_storage_bucket.gold_worm_archive[0].retention_policy[0].retention_period == 31536000 && google_storage_bucket.gold_worm_archive[0].retention_policy[0].is_locked == false
    error_message = "Standalone mode must provision the PII-free Gold WORM bucket with 365d retention and opt-in lock."
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
    condition     = output.shape3_source_uri == "gs://demo-bq-eval-cold/bq-eval/shape3/*"
    error_message = "Shape-3 BigLake table must point to gs://<project_id>/bq-eval/shape3/*."
  }

  assert {
    condition     = google_storage_bucket_iam_member.biglake_viewer[0].condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/demo-bq-eval-cold/objects/bq-eval/\")"
    error_message = "BigLake viewer IAM binding must be prefix-scoped via CEL condition."
  }

  assert {
    condition     = google_bigquery_data_transfer_config.daily_quota_guarded_judge[0].disabled == true && output.scheduled_judge_disabled == true
    error_message = "Scheduled judge query must pause automatically under power_policy = hibernate."
  }

  assert {
    condition     = google_storage_bucket.shared[0].force_destroy == false && google_bigquery_table.shape3_payloads[0].deletion_protection == true
    error_message = "Standalone defaults must enable deletion_protection = true (S-24)."
  }

  assert {
    condition     = length(google_bigquery_dataset.silver) == 4 && length(google_bigquery_table.shape3_payloads) == 1 && length(time_sleep.biglake_iam_propagation) == 1 && time_sleep.biglake_iam_propagation[0].create_duration == "30s"
    error_message = "Standalone mode must provision 4 Silver datasets, 30s BigLake IAM propagation wait, and Hive BigLake shape3_payloads."
  }

  assert {
    condition     = google_logging_project_bucket_config.default_analytics[0].retention_days == null
    error_message = "_Default log bucket retention_days must stay opt-in (null when manage_default_log_retention = false)."
  }
}

run "standalone_scale_to_zero_disables_scheduled_judge" {
  command = plan

  variables {
    project_id   = "demo-bq-eval-s2z"
    power_policy = "scale-to-zero"
  }

  assert {
    condition     = google_bigquery_data_transfer_config.daily_quota_guarded_judge[0].disabled == true && output.scheduled_judge_disabled == true
    error_message = "Scheduled judge query must pause automatically under power_policy = scale-to-zero (S-24)."
  }
}

run "standalone_active_enables_scheduled_judge_cmek_and_rtbf_kms" {
  command = plan

  variables {
    project_id              = "demo-bq-eval-active"
    power_policy            = "active"
    kms_key_name            = "projects/demo-bq-eval-active/locations/us/keyRings/bq/cryptoKeys/silver"
    create_rtbf_kms_keyring = true
  }

  assert {
    condition     = google_bigquery_data_transfer_config.daily_quota_guarded_judge[0].disabled == false && output.scheduled_judge_disabled == false
    error_message = "Scheduled judge query must activate when power_policy = active."
  }

  assert {
    condition = (
      google_bigquery_dataset.silver["restricted"].default_encryption_configuration[0].kms_key_name == "projects/demo-bq-eval-active/locations/us/keyRings/bq/cryptoKeys/silver"
      && google_storage_bucket.gold_worm_archive[0].encryption[0].default_kms_key_name == "projects/demo-bq-eval-active/locations/us/keyRings/bq/cryptoKeys/silver"
      && length(google_kms_crypto_key.rtbf_subject_dek_template) == 1
    )
    error_message = "Silver/WORM resources must apply CMEK and opt-in RTBF KMS keyring when enabled."
  }
}

run "reject_empty_payload_prefix" {
  command = plan

  variables {
    project_id     = "demo-bq-eval-bad1"
    payload_prefix = ""
  }

  expect_failures = [var.payload_prefix]
}

run "reject_invalid_judge_canary_pct" {
  command = plan

  variables {
    project_id              = "demo-bq-eval-bad2"
    judge_canary_sample_pct = 0
  }

  expect_failures = [var.judge_canary_sample_pct]
}

run "reject_invalid_judge_thinking_level" {
  command = plan

  variables {
    project_id           = "demo-bq-eval-bad3"
    judge_thinking_level = "EXTREME"
  }

  expect_failures = [var.judge_thinking_level]
}

run "reject_invalid_judge_max_output_tokens" {
  command = plan

  variables {
    project_id              = "demo-bq-eval-bad4"
    judge_max_output_tokens = 0
  }

  expect_failures = [var.judge_max_output_tokens]
}

run "standalone_disjoint_shape3_prefix_and_worm_retention" {
  command = plan

  variables {
    project_id                      = "demo-bq-eval-s3"
    silver_shape3_prefix            = "shape3/"
    enable_payload_lifecycle_delete = false
  }

  assert {
    condition = (
      strcontains(google_storage_bucket_iam_member.biglake_viewer[0].condition[0].expression, "objects/bq-eval/")
      && strcontains(google_storage_bucket_iam_member.biglake_viewer[0].condition[0].expression, "objects/shape3/")
    )
    error_message = "BigLake viewer CEL condition must cover both payload_prefix and disjoint silver_shape3_prefix."
  }

  assert {
    condition     = length(google_storage_bucket.shared[0].lifecycle_rule) == 0 && length(google_storage_bucket.gold_worm_archive[0].lifecycle_rule) == 0
    error_message = "WORM and payload buckets must omit Delete lifecycle rules when disabled."
  }
}

run "reject_invalid_iam_propagation_wait_duration" {
  command = plan

  variables {
    project_id                    = "demo-bq-eval-bad5"
    iam_propagation_wait_duration = "forever"
  }

  expect_failures = [var.iam_propagation_wait_duration]
}
