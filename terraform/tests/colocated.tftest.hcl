# Credential-free plan-time verification of co-located showcase mode (create_* = false).

mock_provider "google" {}
mock_provider "google-beta" {}

run "colocated_showcase_adopts_existing_resources" {
  command = plan

  variables {
    project_id                              = "demo-colocated-showcase"
    region                                  = "us-central1"
    manage_apis                             = false
    create_shared_bucket                    = false
    create_eval_dataset                     = false
    create_vertex_connection                = false
    create_shared_bucket_lister_role        = false
    shared_bucket_lister_role_name          = "projects/demo-colocated-showcase/roles/shared_bucket_lister"
    vertex_connection_service_account_email = "bqcx-123456-test@gcp-sa-bigquery-condel.iam.gserviceaccount.com"
    power_policy                            = "hibernate"
  }

  assert {
    condition     = length(google_storage_bucket.shared) == 0 && length(data.google_storage_bucket.shared) == 1
    error_message = "Co-located mode must adopt existing shared bucket via data lookup without creating one."
  }

  assert {
    condition     = length(google_bigquery_dataset.agent_eval) == 0 && length(data.google_bigquery_dataset.agent_eval) == 1
    error_message = "Co-located mode must adopt existing agent_eval dataset via data lookup."
  }

  assert {
    condition     = length(google_bigquery_connection.vertex_ai) == 0
    error_message = "Co-located mode must reuse existing Vertex AI connection without creating a duplicate."
  }

  assert {
    condition     = length(google_project_service.apis) == 0
    error_message = "Co-located mode with manage_apis = false must not manage project APIs."
  }

  assert {
    condition     = output.vertex_connection_name == "projects/demo-colocated-showcase/locations/us/connections/vertex_ai_connection"
    error_message = "Co-located mode must build the canonical existing connection resource name."
  }

  assert {
    condition     = google_bigquery_data_transfer_config.daily_quota_guarded_judge[0].disabled == true
    error_message = "Co-located hibernate mode must keep scheduled judge queries paused at rest."
  }
}
