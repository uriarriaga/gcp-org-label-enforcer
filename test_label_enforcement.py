#!/usr/bin/env python3
"""Automated unit and integration tests for GCP Custom Org Policy label enforcement.

Tests all compatible resource types:
  1. Pub/Sub Topics (pubsub.googleapis.com/Topic)
  2. Cloud Storage Buckets (storage.googleapis.com/Bucket)
  3. Compute Engine Instances (compute.googleapis.com/Instance)

For each resource type, the test suite verifies:
  - NEGATIVE TEST: Creating an unlabelled resource is BLOCKED by the Custom Org Policy.
  - POSITIVE TEST: Creating a resource with all required labels SUCCEEDS.
  - TEARDOWN: Any test resources created are automatically deleted.

Usage:
  # Run all tests against default/active project:
  python3 test_label_enforcement.py --project uri-test-491314

  # Run only Pub/Sub topic tests:
  python3 test_label_enforcement.py --project uri-test-491314 TestPubSubTopic

  # Run only Storage Bucket tests:
  python3 test_label_enforcement.py --project uri-test-491314 TestStorageBucket

  # Run only Compute VM tests:
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
    topic_name = f"test-unlabelled-{int(time.time())}"
    rc, stdout, stderr = run_cmd([
        "gcloud", "pubsub", "topics", "create", topic_name,
        f"--project={self.project}",
    ])
    # Must fail
    self.assertNotEqual(rc, 0, "Unlabelled Pub/Sub topic creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelspubsubtopic" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial in error output, got: {stderr}",
    )

  def test_pubsub_topic_compliant_succeeds(self):
    """Creating a topic with all required labels MUST succeed."""
    topic_name = f"test-compliant-{int(time.time())}"
    labels_arg = ",".join(f"{k}={v}" for k, v in REQUIRED_LABELS.items())
    rc, stdout, stderr = run_cmd([
        "gcloud", "pubsub", "topics", "create", topic_name,
        f"--project={self.project}",
        f"--labels={labels_arg}",
    ])
    if rc == 0:
      self.created_topics.append(topic_name)
    self.assertEqual(rc, 0, f"Compliant Pub/Sub topic creation failed: {stderr}")


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
    # Must fail
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
    # Fetch access token from gcloud
    rc_t, token, err_t = run_cmd(["gcloud", "auth", "print-access-token"])
    self.assertEqual(rc_t, 0, f"Failed to get auth token: {err_t}")
    token = token.strip().splitlines()[-1]

    payload = json.dumps({
        "name": bucket_name,
        "location": REGION.upper(),
        "labels": REQUIRED_LABELS,
    })

    # Call GCS API directly (gcloud storage buckets create doesn't support --labels at create time)
    rc, stdout, stderr = run_cmd([
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
    instance_name = f"test-unlabelled-{int(time.time())}"
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
    # Must fail
    self.assertNotEqual(rc, 0, "Unlabelled VM instance creation should have failed!")
    combined = (stdout + "\n" + stderr).lower()
    self.assertTrue(
        "customconstraints/custom.reqlabelscomputeinstance" in combined
        or "denied by org policy" in combined,
        f"Expected Org Policy denial in error output, got: {stderr}",
    )

  def test_compute_instance_compliant_succeeds(self):
    """Creating a VM with all required labels MUST succeed."""
    instance_name = f"test-compliant-{int(time.time())}"
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

  print(f"============================================================")
  print(f"Running Custom Org Policy Label Enforcement Test Suite")
  print(f"Target Project  : {get_default_project()}")
  print(f"Required Labels : {REQUIRED_LABELS}")
  print(f"============================================================\n")

  unittest.main(argv=filtered_args)
