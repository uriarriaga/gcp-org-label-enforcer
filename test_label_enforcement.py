#!/usr/bin/env python3
"""Automated unit and integration tests for GCP Custom Org Policy label enforcement.

Tests all compatible resource types:
  1. Pub/Sub Topics (pubsub.googleapis.com/Topic)
  2. Pub/Sub Subscriptions (pubsub.googleapis.com/Subscription)
  3. Pub/Sub Snapshots (pubsub.googleapis.com/Snapshot)
  4. Cloud Storage Buckets (storage.googleapis.com/Bucket)
  5. Compute Engine Instances (compute.googleapis.com/Instance)
  6. Dataproc Batches (dataproc.googleapis.com/Batch)
  7. Dataproc Clusters (dataproc.googleapis.com/Cluster)
  8. GKE Clusters (container.googleapis.com/Cluster)

Also includes offline unit tests for policy/constraint configuration generation.

Usage:
  # Run offline unit tests only (instant, no GCP credentials or project needed):
  python3 test_label_enforcement.py TestPolicyGenerationUnitTests

  # Run all tests against target project (offline unit tests + live GCP tests):
  python3 test_label_enforcement.py --project uri-test-491314

  # Run specific live resource tests:
  python3 test_label_enforcement.py --project uri-test-491314 TestPubSubTopic
  python3 test_label_enforcement.py --project uri-test-491314 TestPubSubSubscription
  python3 test_label_enforcement.py --project uri-test-491314 TestPubSubSnapshot
  python3 test_label_enforcement.py --project uri-test-491314 TestStorageBucket
  python3 test_label_enforcement.py --project uri-test-491314 TestComputeInstance
"""

import json
import os
import subprocess
import time
import unittest

PROJECT_ID = os.environ.get("GCP_PROJECT", "")
REQUIRED_LABELS = {
    "environment": "test",
    "cost_center": "engineering",
    "owner": "uarriaga",
}
REGION = "us-central1"
ZONE = "us-central1-a"


def get_default_project():
  """Fetch project ID from gcloud config if not set."""
  global PROJECT_ID
  if PROJECT_ID:
    return PROJECT_ID
  proc = subprocess.run(
      ["gcloud", "config", "get-value", "project"],
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
  )
  proj = proc.stdout.strip()
  if not proj or "(unset)" in proj:
    raise RuntimeError(
        "No GCP project specified. Pass --project <ID> or set GCP_PROJECT env var."
    )
  PROJECT_ID = proj
  return PROJECT_ID


def run_cmd(cmd, check=False):
  """Run a shell command and return (returncode, stdout, stderr)."""
  if cmd and cmd[0] == "gcloud" and "--quiet" not in cmd:
    cmd = [cmd[0], "--quiet"] + cmd[1:]
  proc = subprocess.run(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
  )
  if check and proc.returncode != 0:
    raise RuntimeError(
        f"Command failed ({proc.returncode}): {' '.join(cmd)}\nSTDERR: {proc.stderr}"
    )
  return proc.returncode, proc.stdout, proc.stderr


def is_service_enabled(service_name, project_id):
  """Check if a given Google Cloud service API is enabled in the project."""
  rc, stdout, _ = run_cmd([
      "gcloud", "services", "list", "--enabled",
      f"--project={project_id}",
      f"--filter=name:{service_name}",
      "--format=value(config.name)",
  ])
  return rc == 0 and service_name in stdout


# ============================================================================
# Offline Unit Tests: Policy & Condition Generation
# ============================================================================
class TestPolicyGenerationUnitTests(unittest.TestCase):
  """Unit tests for CEL condition, short name, and constraint YAML generation."""

  def test_condition_with_required_keys_labels(self):
    """build_condition should create AND'd clauses for 'resource.labels'."""
    from enforce_org_label_policy import build_condition
    cond = build_condition(["env", "owner"], field="labels")
    self.assertEqual(cond, "'env' in resource.labels && 'owner' in resource.labels")

  def test_condition_with_required_keys_resource_labels(self):
    """build_condition should create AND'd clauses for 'resource.resourceLabels'."""
    from enforce_org_label_policy import build_condition
    cond = build_condition(["env", "team"], field="resourceLabels")
    self.assertEqual(cond, "'env' in resource.resourceLabels && 'team' in resource.resourceLabels")

  def test_condition_empty_keys_fallback(self):
    """build_condition should fall back to size() > 0 if no specific keys required."""
    from enforce_org_label_policy import build_condition
    cond = build_condition([], field="labels")
    self.assertEqual(cond, "resource.labels.size() > 0")

  def test_make_short_name_formatting_and_length(self):
    """make_short_name should generate valid, truncated constraint short names."""
    from enforce_org_label_policy import make_short_name
    short_topic = make_short_name("pubsub.googleapis.com/Topic")
    self.assertEqual(short_topic, "custom.reqLabelsPubsubTopic")
    self.assertLessEqual(len(short_topic), 64)

    short_vm = make_short_name("compute.googleapis.com/Instance")
    self.assertEqual(short_vm, "custom.reqLabelsComputeInstance")
    self.assertLessEqual(len(short_vm), 64)

    short_batch = make_short_name("dataproc.googleapis.com/Batch")
    self.assertEqual(short_batch, "custom.reqLabelsDataprocBatch")
    self.assertLessEqual(len(short_batch), 64)

  def test_format_condensed_list(self):
    """format_condensed_list should condense long lists and format short ones cleanly."""
    from enforce_org_label_policy import format_condensed_list
    self.assertEqual(format_condensed_list([]), "None")
    self.assertEqual(format_condensed_list(["a", "b"]), "a, b")
    items = ["r1", "r2", "r3", "r4", "r5", "r6", "r7"]
    condensed = format_condensed_list(items, max_items=5)
    self.assertEqual(condensed, "r1, r2, r3, r4, r5, ... (+2 more)")

  def test_supported_resource_registry_integrity(self):
    """Verify SUPPORTED_RESOURCE_TYPES contains all expected services and methods."""
    from enforce_org_label_policy import SUPPORTED_RESOURCE_TYPES
    expected_resources = [
        "pubsub.googleapis.com/Topic",
        "pubsub.googleapis.com/Subscription",
        "pubsub.googleapis.com/Snapshot",
        "storage.googleapis.com/Bucket",
        "compute.googleapis.com/Instance",
        "dataproc.googleapis.com/Cluster",
        "dataproc.googleapis.com/Batch",
        "container.googleapis.com/Cluster",
    ]
    for res in expected_resources:
      self.assertIn(res, SUPPORTED_RESOURCE_TYPES)
      meta = SUPPORTED_RESOURCE_TYPES[res]
      self.assertIn("field", meta)
      self.assertIn("method_types", meta)
      self.assertTrue(len(meta["method_types"]) >= 1)

    # Dataproc Batch should only have CREATE
    self.assertEqual(SUPPORTED_RESOURCE_TYPES["dataproc.googleapis.com/Batch"]["method_types"], ["CREATE"])
    # GKE Cluster should use resourceLabels
    self.assertEqual(SUPPORTED_RESOURCE_TYPES["container.googleapis.com/Cluster"]["field"], "resourceLabels")


# ============================================================================
# Live Integration Tests: Pub/Sub Resources
# ============================================================================
class TestPubSubTopic(unittest.TestCase):
  """Test label enforcement on pubsub.googleapis.com/Topic."""

  def setUp(self):
    self.project = get_default_project()
    self.created_topics = []

  def tearDown(self):
    for topic in self.created_topics:
      run_cmd(["gcloud", "pubsub", "topics", "delete", topic, f"--project={self.project}"])

  def test_pubsub_topic_unlabelled_fails(self):
    """Creating an unlabelled topic MUST be blocked by Org Policy."""
    topic_name = f"test-topic-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "pubsub", "topics", "create", topic_name,
        f"--project={self.project}",
    ])
    self.assertNotEqual(rc, 0, "Unlabelled Pub/Sub topic creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelspubsubtopic" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial in error output, got: {stderr}",
    )

  def test_pubsub_topic_compliant_succeeds(self):
    """Creating a topic with all required labels MUST succeed."""
    topic_name = f"test-topic-compliant-{int(time.time())}"
    labels_arg = ",".join(f"{k}={v}" for k, v in REQUIRED_LABELS.items())
    rc, stdout, stderr = run_cmd([
        "gcloud", "pubsub", "topics", "create", topic_name,
        f"--project={self.project}",
        f"--labels={labels_arg}",
    ])
    if rc == 0:
      self.created_topics.append(topic_name)
    self.assertEqual(rc, 0, f"Compliant Pub/Sub topic creation failed: {stderr}")


class TestPubSubSubscription(unittest.TestCase):
  """Test label enforcement on pubsub.googleapis.com/Subscription."""

  @classmethod
  def setUpClass(cls):
    cls.project = get_default_project()
    cls.shared_topic = f"test-sub-parent-topic-{int(time.time())}"
    labels_arg = ",".join(f"{k}={v}" for k, v in REQUIRED_LABELS.items())
    rc, _, stderr = run_cmd([
        "gcloud", "pubsub", "topics", "create", cls.shared_topic,
        f"--project={cls.project}",
        f"--labels={labels_arg}",
    ])
    if rc != 0:
      raise RuntimeError(f"Failed to create parent topic for subscription tests: {stderr}")

  @classmethod
  def tearDownClass(cls):
    run_cmd(["gcloud", "pubsub", "topics", "delete", cls.shared_topic, f"--project={cls.project}"])

  def setUp(self):
    self.created_subs = []

  def tearDown(self):
    for sub in self.created_subs:
      run_cmd(["gcloud", "pubsub", "subscriptions", "delete", sub, f"--project={self.project}"])

  def test_pubsub_subscription_unlabelled_fails(self):
    """Creating an unlabelled subscription MUST be blocked by custom.reqLabelsPubsubSubscription."""
    sub_name = f"test-sub-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "pubsub", "subscriptions", "create", sub_name,
        f"--topic={self.shared_topic}",
        f"--project={self.project}",
    ])
    self.assertNotEqual(rc, 0, "Unlabelled Pub/Sub subscription creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelspubsubsubscription" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial in error output, got: {stderr}",
    )

  def test_pubsub_subscription_compliant_succeeds(self):
    """Creating a subscription with all required labels MUST succeed."""
    sub_name = f"test-sub-compliant-{int(time.time())}"
    labels_arg = ",".join(f"{k}={v}" for k, v in REQUIRED_LABELS.items())
    rc, stdout, stderr = run_cmd([
        "gcloud", "pubsub", "subscriptions", "create", sub_name,
        f"--topic={self.shared_topic}",
        f"--project={self.project}",
        f"--labels={labels_arg}",
    ])
    if rc == 0:
      self.created_subs.append(sub_name)
    self.assertEqual(rc, 0, f"Compliant Pub/Sub subscription creation failed: {stderr}")


class TestPubSubSnapshot(unittest.TestCase):
  """Test label enforcement on pubsub.googleapis.com/Snapshot."""

  @classmethod
  def setUpClass(cls):
    cls.project = get_default_project()
    cls.shared_topic = f"test-snap-parent-topic-{int(time.time())}"
    cls.shared_sub = f"test-snap-parent-sub-{int(time.time())}"
    labels_arg = ",".join(f"{k}={v}" for k, v in REQUIRED_LABELS.items())

    rc1, _, err1 = run_cmd([
        "gcloud", "pubsub", "topics", "create", cls.shared_topic,
        f"--project={cls.project}",
        f"--labels={labels_arg}",
    ])
    if rc1 != 0:
      raise RuntimeError(f"Failed to create parent topic for snapshot tests: {err1}")

    rc2, _, err2 = run_cmd([
        "gcloud", "pubsub", "subscriptions", "create", cls.shared_sub,
        f"--topic={cls.shared_topic}",
        f"--project={cls.project}",
        f"--labels={labels_arg}",
    ])
    if rc2 != 0:
      run_cmd(["gcloud", "pubsub", "topics", "delete", cls.shared_topic, f"--project={cls.project}"])
      raise RuntimeError(f"Failed to create parent subscription for snapshot tests: {err2}")

  @classmethod
  def tearDownClass(cls):
    run_cmd(["gcloud", "pubsub", "subscriptions", "delete", cls.shared_sub, f"--project={cls.project}"])
    run_cmd(["gcloud", "pubsub", "topics", "delete", cls.shared_topic, f"--project={cls.project}"])

  def setUp(self):
    self.created_snaps = []

  def tearDown(self):
    for snap in self.created_snaps:
      run_cmd(["gcloud", "pubsub", "snapshots", "delete", snap, f"--project={self.project}"])

  def test_pubsub_snapshot_unlabelled_fails(self):
    """Creating an unlabelled snapshot MUST be blocked by custom.reqLabelsPubsubSnapshot."""
    snap_name = f"test-snap-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "pubsub", "snapshots", "create", snap_name,
        f"--subscription={self.shared_sub}",
        f"--project={self.project}",
    ])
    self.assertNotEqual(rc, 0, "Unlabelled Pub/Sub snapshot creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelspubsubsnapshot" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial in error output, got: {stderr}",
    )

  def test_pubsub_snapshot_compliant_succeeds(self):
    """Creating a snapshot with all required labels MUST succeed."""
    snap_name = f"test-snap-compliant-{int(time.time())}"
    labels_arg = ",".join(f"{k}={v}" for k, v in REQUIRED_LABELS.items())
    rc, stdout, stderr = run_cmd([
        "gcloud", "pubsub", "snapshots", "create", snap_name,
        f"--subscription={self.shared_sub}",
        f"--project={self.project}",
        f"--labels={labels_arg}",
    ])
    if rc == 0:
      self.created_snaps.append(snap_name)
    self.assertEqual(rc, 0, f"Compliant Pub/Sub snapshot creation failed: {stderr}")


# ============================================================================
# Live Integration Tests: Storage & Compute Resources
# ============================================================================
class TestStorageBucket(unittest.TestCase):
  """Test label enforcement on storage.googleapis.com/Bucket."""

  def setUp(self):
    self.project = get_default_project()
    self.created_buckets = []

  def tearDown(self):
    for bucket in self.created_buckets:
      run_cmd(["gcloud", "storage", "rm", "--recursive", f"gs://{bucket}"])

  def test_storage_bucket_unlabelled_fails(self):
    """Creating an unlabelled bucket MUST be blocked by Org Policy (HTTP 412)."""
    bucket_name = f"test-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "storage", "buckets", "create", f"gs://{bucket_name}",
        f"--project={self.project}",
        f"--location={REGION}",
    ])
    self.assertNotEqual(rc, 0, "Unlabelled GCS bucket creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelsstoragebucket" in combined
        or "412" in combined,
        f"Expected Org Policy 412 denial, got: {stderr}",
    )

  def test_storage_bucket_compliant_succeeds(self):
    """Creating a bucket with required labels via GCS API MUST succeed."""
    bucket_name = f"test-compliant-{int(time.time())}"
    rc_t, token, err_t = run_cmd(["gcloud", "auth", "print-access-token"])
    self.assertEqual(rc_t, 0, f"Failed to get auth token: {err_t}")
    token = token.strip().splitlines()[-1]

    payload = json.dumps({
        "name": bucket_name,
        "location": REGION.upper(),
        "labels": REQUIRED_LABELS,
    })

    rc, stdout, _ = run_cmd([
        "curl", "-s", "-w", "\n%{http_code}",
        "-X", "POST",
        f"https://storage.googleapis.com/storage/v1/b?project={self.project}",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-d", payload,
    ])

    lines = stdout.strip().splitlines()
    status_code = lines[-1] if lines else "0"
    if status_code == "200":
      self.created_buckets.append(bucket_name)

    self.assertEqual(status_code, "200", f"Compliant bucket creation failed: {stdout}")


class TestComputeInstance(unittest.TestCase):
  """Test label enforcement on compute.googleapis.com/Instance."""

  def setUp(self):
    self.project = get_default_project()
    self.created_instances = []

  def tearDown(self):
    for instance in self.created_instances:
      run_cmd([
          "gcloud", "compute", "instances", "delete", instance,
          f"--zone={ZONE}",
          f"--project={self.project}",
      ])

  def test_compute_instance_unlabelled_fails(self):
    """Creating an unlabelled VM MUST be blocked by custom.reqLabelsComputeInstance."""
    instance_name = f"test-vm-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "compute", "instances", "create", instance_name,
        f"--project={self.project}",
        f"--zone={ZONE}",
        "--machine-type=e2-micro",
        "--network=test",
        "--subnet=test",
        "--no-address",
        "--shielded-secure-boot",
    ])
    self.assertNotEqual(rc, 0, "Unlabelled VM instance creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelscomputeinstance" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial in error output, got: {stderr}",
    )

  def test_compute_instance_compliant_succeeds(self):
    """Creating a VM with all required labels MUST succeed."""
    instance_name = f"test-vm-compliant-{int(time.time())}"
    labels_arg = ",".join(f"{k}={v}" for k, v in REQUIRED_LABELS.items())
    rc, stdout, stderr = run_cmd([
        "gcloud", "compute", "instances", "create", instance_name,
        f"--project={self.project}",
        f"--zone={ZONE}",
        "--machine-type=e2-micro",
        "--network=test",
        "--subnet=test",
        "--no-address",
        "--shielded-secure-boot",
        f"--labels={labels_arg}",
    ])
    if rc == 0:
      self.created_instances.append(instance_name)
    self.assertEqual(rc, 0, f"Compliant VM instance creation failed: {stderr}")


# ============================================================================
# Live Integration Tests: Dataproc & Container Resources (Skipped if API Disabled)
# ============================================================================
class TestDataprocBatch(unittest.TestCase):
  """Test label enforcement on dataproc.googleapis.com/Batch."""

  def setUp(self):
    self.project = get_default_project()
    if not is_service_enabled("dataproc.googleapis.com", self.project):
      self.skipTest("dataproc.googleapis.com API is not enabled in this project.")

  def test_dataproc_batch_unlabelled_fails(self):
    """Submitting an unlabelled batch job MUST be rejected by custom.reqLabelsDataprocBatch."""
    batch_id = f"test-batch-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "dataproc", "batches", "submit", "spark",
        f"--batch={batch_id}",
        f"--region={REGION}",
        f"--project={self.project}",
        "--class=org.apache.spark.examples.SparkPi",
        "--jars=file:///usr/lib/spark/examples/jars/spark-examples.jar",
        "--", "1000",
    ])
    self.assertNotEqual(rc, 0, "Unlabelled Dataproc batch creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelsdataprocbatch" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial, got: {stderr}",
    )


class TestDataprocCluster(unittest.TestCase):
  """Test label enforcement on dataproc.googleapis.com/Cluster."""

  def setUp(self):
    self.project = get_default_project()
    if not is_service_enabled("dataproc.googleapis.com", self.project):
      self.skipTest("dataproc.googleapis.com API is not enabled in this project.")

  def test_dataproc_cluster_unlabelled_fails(self):
    """Creating an unlabelled Dataproc cluster MUST be rejected by custom.reqLabelsDataprocCluster."""
    cluster_name = f"test-cluster-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "dataproc", "clusters", "create", cluster_name,
        f"--region={REGION}",
        f"--project={self.project}",
        "--num-workers=2",
    ])
    self.assertNotEqual(rc, 0, "Unlabelled Dataproc cluster creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelsdataproccluster" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial, got: {stderr}",
    )


class TestContainerCluster(unittest.TestCase):
  """Test label enforcement on container.googleapis.com/Cluster."""

  def setUp(self):
    self.project = get_default_project()
    if not is_service_enabled("container.googleapis.com", self.project):
      self.skipTest("container.googleapis.com API is not enabled in this project.")

  def test_container_cluster_unlabelled_fails(self):
    """Creating an unlabelled GKE cluster MUST be rejected by custom.reqLabelsContainerCluster."""
    cluster_name = f"test-gke-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "container", "clusters", "create", cluster_name,
        f"--zone={ZONE}",
        f"--project={self.project}",
        "--num-nodes=1",
    ])
    self.assertNotEqual(rc, 0, "Unlabelled GKE cluster creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelscontainercluster" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial, got: {stderr}",
    )


if __name__ == "__main__":
  import sys
  # Extract --project flag if supplied before passing args to unittest
  filtered_args = [sys.argv[0]]
  i = 1
  while i < len(sys.argv):
    arg = sys.argv[i]
    if arg == "--project":
      PROJECT_ID = sys.argv[i + 1]
      i += 2
    elif arg.startswith("--project="):
      PROJECT_ID = arg.split("=")[1]
      i += 1
    else:
      filtered_args.append(arg)
      i += 1

  print("============================================================")
  print("Running Custom Org Policy Label Enforcement Test Suite")
  if PROJECT_ID:
    print(f"Target Project  : {PROJECT_ID}")
    print(f"Required Labels : {REQUIRED_LABELS}")
  else:
    print("Mode            : Offline Unit Tests (pass --project <ID> for live tests)")
  print("============================================================\n")

  unittest.main(argv=filtered_args)
