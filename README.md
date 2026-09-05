# GCP Custom Org Policy Label Enforcer

An automated tool that inspects **Google Cloud Asset Inventory** (at the Project or Organization level) to discover every resource type currently in use (or targets all known compatible services) and generates + enforces **Custom Organization Policies** requiring resources to have labels.

---

## Verified Label-Compatible GCP Resources

Google Cloud Custom Organization Policies (CuOP) validate resource creation and updates using Common Expression Language (CEL). A custom constraint can **only** enforce labels if the underlying service has onboarded to CuOP and defines a label field in its CEL schema proto.

The following is the authoritative list of all verified compatible resource types across Google Cloud:

| Resource Type | Service | CEL Label Field | Supported Methods | Status |
| :--- | :--- | :--- | :--- | :--- |
| `pubsub.googleapis.com/Topic` | Cloud Pub/Sub | `resource.labels` | `CREATE`, `UPDATE` | **GA** |
| `pubsub.googleapis.com/Subscription` | Cloud Pub/Sub | `resource.labels` | `CREATE`, `UPDATE` | **GA** |
| `pubsub.googleapis.com/Snapshot` | Cloud Pub/Sub | `resource.labels` | `CREATE`, `UPDATE` | **GA** |
| `storage.googleapis.com/Bucket` | Cloud Storage | `resource.labels` | `CREATE`, `UPDATE` | **GA** |
| `compute.googleapis.com/Instance` | Compute Engine | `resource.labels` | `CREATE`, `UPDATE` | **GA** |
| `dataproc.googleapis.com/Cluster` | Cloud Dataproc | `resource.labels` | `CREATE`, `UPDATE` | **GA** |
| `dataproc.googleapis.com/Batch` | Dataproc Serverless | `resource.labels` | `CREATE` | **GA** |
| `container.googleapis.com/Cluster` | Google Kubernetes Engine | `resource.resourceLabels` | `CREATE`, `UPDATE` | **GA** |

> **Note on other resource types:** Resources such as Compute Persistent Disks (`compute.googleapis.com/Disk`), Cloud Functions (`cloudfunctions.googleapis.com/Function`), and Cloud SQL do not currently expose `resource.labels` in their Custom Org Policy CEL schemas and are therefore gracefully skipped.

---

## Key Google Cloud Architectural Rules

Before running this tool, it is important to understand how Google Cloud Organization Policies work under the hood:

1. **One Resource Type per Custom Constraint:**
   Google Cloud backend validation enforces a strict **1-to-1 relationship** between a custom constraint and a resource type (e.g., `storage.googleapis.com/Bucket`). Wildcards (`*`) or multi-service lists are rejected by the API. Therefore, this tool automatically generates one custom constraint per supported resource type.
2. **Custom Constraints Live at the Organization Level:**
   In Google Cloud, custom constraint definitions (`CustomConstraint`) can **only** be registered at the Organization resource level (`organizations/{ORG_ID}/customConstraints/...`). They cannot be defined at the project level.
3. **Policy Enforcement Can Be Scoped to a Single Project:**
   While the constraint *definition* lives in the Organization, the *policy enforcement* (`Policy`) can be applied **exclusively to a single sandbox Project** (`projects/{PROJECT_ID}/policies/...`). This allows you to test label enforcement safely in a test project without impacting any other projects or production workloads.
4. **Automatic Field & Method Matching:**
   Different services use different label attributes (e.g. GKE uses `resource.resourceLabels` whereas GCE, GCS, Pub/Sub, and Dataproc use `resource.labels`) and supported method types (e.g. Dataproc Batch supports only `CREATE`). This tool automatically selects the correct field and method types for each service.

---

## Prerequisites

1. **Python 3.7+**
   The script uses **only the Python standard library** (zero external `pip` dependencies required).
2. **Google Cloud SDK (`gcloud` CLI)**
   Must be installed and authenticated:
   ```bash
   gcloud auth login
   ```
3. **Required GCP APIs**
   Enable the following APIs on your target project:
   ```bash
   gcloud services enable cloudasset.googleapis.com orgpolicy.googleapis.com --project=<YOUR_PROJECT_ID>
   ```
4. **Required IAM Permissions**
   The authenticated identity running the script must have:
   - **`roles/orgpolicy.policyAdmin`** on the **Organization** (required to register custom constraints).
   - **`roles/cloudasset.viewer`** on the target Project or Organization (required if using automatic CAI discovery).

---

## Quickstart: Running in GCP Cloud Shell

Cloud Shell comes with `python3` and `gcloud` pre-installed and authenticated:

```bash
# 1. Clone the repository
git clone https://github.com/uriarriaga/gcp-org-label-enforcer.git
cd gcp-org-label-enforcer

# 2. Set your active project and enable required APIs
gcloud config set project uri-test-491314
gcloud services enable cloudasset.googleapis.com orgpolicy.googleapis.com

# 3. Dry-Run across all verified compatible resource types (safe, generates YAMLs locally)
python3 enforce_org_label_policy.py --project uri-test-491314 --all-supported --dry-run

# 4. Apply & Enforce on uri-test-491314 with mandatory labels
python3 enforce_org_label_policy.py --project uri-test-491314 --all-supported --apply --enforce \
    --required-keys environment,cost_center,owner

# 5. Run the automated test suite
python3 test_label_enforcement.py --project uri-test-491314

# 6. Cleanup when finished testing
python3 enforce_org_label_policy.py --project uri-test-491314 --cleanup
```

---

## Detailed Usage Options

### 1. Enforce Across All Supported Resource Types (Recommended)
Registers constraints at the organization level and enforces them on your project for all 8 compatible resource types without needing them to pre-exist in Asset Inventory:
```bash
python3 enforce_org_label_policy.py \
    --project <YOUR_PROJECT_ID> \
    --all-supported \
    --required-keys environment,cost_center,owner \
    --apply --enforce
```

### 2. Enforce Only for Resources Discovered via Cloud Asset Inventory
Scans what resource types currently exist in your project or organization, filters for compatible ones, and enforces labels:
```bash
python3 enforce_org_label_policy.py \
    --project <YOUR_PROJECT_ID> \
    --required-keys environment,cost_center,owner \
    --apply --enforce
```

### 3. Dry-Run (Safe Discovery & Local YAML Generation)
Discovers resource types and generates the constraint/policy YAML files locally in a temporary directory without calling GCP APIs:
```bash
python3 enforce_org_label_policy.py \
    --project <YOUR_PROJECT_ID> \
    --all-supported \
    --dry-run
```

### 4. Enforce on Specific Resources
Target specific resource types explicitly:
```bash
python3 enforce_org_label_policy.py \
    --project <YOUR_PROJECT_ID> \
    --resources pubsub.googleapis.com/Topic,compute.googleapis.com/Instance,storage.googleapis.com/Bucket \
    --required-keys environment,cost_center,owner \
    --apply --enforce
```

### 5. Enforce Organization-Wide
Enforce mandatory labels across **all projects** in the organization:
```bash
python3 enforce_org_label_policy.py \
    --organization <YOUR_NUMERIC_ORG_ID> \
    --all-supported \
    --required-keys environment,cost_center,owner \
    --apply --enforce
```

### 6. Cleanup / Rollback
Deletes all custom constraints and project policies created by this script (matching prefix `custom.reqLabels*`):
```bash
python3 enforce_org_label_policy.py \
    --project <YOUR_PROJECT_ID> \
    --cleanup
```

---

## CLI Options Reference

| Flag | Description |
| :--- | :--- |
| `--project <PROJECT_ID>` | Target a specific GCP project. Scopes Cloud Asset Inventory discovery and policy enforcement exclusively to this project. Parent Organization ID is auto-discovered. |
| `--organization <ORG_ID>` | Numeric GCP Organization ID (e.g., `123456789012`). Required if running org-wide or if parent auto-discovery cannot traverse folders. |
| `--all-supported` | Enforce policies across all 8 verified label-compatible resource types without needing prior discovery via CAI. |
| `--resources <LIST>` | Comma-separated list of explicit resource types to target (e.g. `pubsub.googleapis.com/Topic,compute.googleapis.com/Instance`). |
| `--required-keys <KEYS>` | Comma-separated list of mandatory label keys (e.g., `environment,cost_center,owner`). If omitted, enforces `size() > 0` (at least one label). |
| `--output-dir <DIR>` | Directory where generated `.constraint.yaml` and `.policy.yaml` files are written. Defaults to a system temporary directory. |
| `--dry-run` | Default mode if `--apply` is not specified. Discovers resources and writes YAML files locally without calling org policy APIs. |
| `--apply` | Creates/updates the custom constraints in the parent GCP Organization. |
| `--enforce` | Applies the enforcing policy (`enforce: true`) to the target project (or organization). Implies `--apply`. |
| `--cleanup` | Deletes all policies and custom constraints created by this script. |
| `-v, --verbose` | Print all incompatible/unsupported resource types in full (default is condensed). |

---

## Automated Unit & Integration Tests

The repository includes a comprehensive test suite ([`test_label_enforcement.py`](./test_label_enforcement.py)) using Python's built-in `unittest` framework:

### 1. Offline Unit Tests (Instant, Zero GCP Cost/Calls)
Tests CEL expression syntax, constraint name limits (<= 64 chars), schema metadata integrity, and YAML generation:
```bash
python3 test_label_enforcement.py TestPolicyGenerationUnitTests
```

### 2. Live Integration Tests
Tests real GCP API admission control against your project:
- **`TestPubSubTopic`**: Verifies unlabelled topic creation is rejected by `custom.reqLabelsPubsubTopic`, and compliant topic creation succeeds and is deleted.
- **`TestPubSubSubscription`**: Verifies unlabelled subscription creation is rejected by `custom.reqLabelsPubsubSubscription`, and compliant subscription creation succeeds and is deleted.
- **`TestPubSubSnapshot`**: Verifies unlabelled snapshot creation is rejected by `custom.reqLabelsPubsubSnapshot`, and compliant snapshot creation succeeds and is deleted.
- **`TestStorageBucket`**: Verifies unlabelled bucket creation is rejected (HTTP 412) by `custom.reqLabelsStorageBucket`, and compliant bucket creation succeeds and is deleted.
- **`TestComputeInstance`**: Verifies unlabelled VM instance creation is rejected by `custom.reqLabelsComputeInstance`, and compliant VM creation succeeds and is deleted.
- **`TestDataprocBatch`**: Verifies batch rejection without labels (auto-skipped if Dataproc API is disabled).
- **`TestDataprocCluster`**: Verifies cluster rejection without labels (auto-skipped if Dataproc API is disabled).
- **`TestContainerCluster`**: Verifies GKE cluster rejection without labels (auto-skipped if Container API is disabled).

```bash
# Run all tests against your target project:
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID>

# Or run tests for a specific resource:
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID> TestPubSubTopic
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID> TestPubSubSubscription
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID> TestPubSubSnapshot
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID> TestStorageBucket
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID> TestComputeInstance
```
