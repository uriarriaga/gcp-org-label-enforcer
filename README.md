# GCP Custom Org Policy Label Enforcer

An automated tool that inspects **Google Cloud Asset Inventory** (at the Project or Organization level) to discover every resource type currently in use and generates + enforces **Custom Organization Policies** requiring resources to have labels.

---

## Key Google Cloud Architectural Rules

Before running this tool, it is important to understand how Google Cloud Organization Policies work under the hood:

1. **One Resource Type per Custom Constraint:**
   Google Cloud backend validation enforces a strict **1-to-1 relationship** between a custom constraint and a resource type (e.g., `storage.googleapis.com/Bucket`). Wildcards (`*`) or multi-service lists are rejected by the API. Therefore, this tool automatically iterates over all discovered resource types and creates one custom constraint per supported type.
2. **Custom Constraints Live at the Organization Level:**
   In Google Cloud, custom constraint definitions (`CustomConstraint`) can **only** be registered at the Organization resource level (`organizations/{ORG_ID}/customConstraints/...`). They cannot be defined at the project level.
3. **Policy Enforcement Can Be Scoped to a Single Project:**
   While the constraint *definition* lives in the Organization, the *policy enforcement* (`Policy`) can be applied **exclusively to a single sandbox Project** (`projects/{PROJECT_ID}/policies/...`). This allows you to test label enforcement safely in a test project without impacting any other projects or production workloads.
4. **CEL Schema Support:**
   Only Google Cloud services that are onboarded to Custom Organization Policies **and** expose `resource.labels` in their CEL schema can be enforced this way (e.g., Cloud Storage Buckets, Pub/Sub Topics, Compute Engine Instances). For resource types that do not expose `labels` or are not onboarded, this tool automatically detects the API rejection, logs the reason, and gracefully skips them.

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
   - **`roles/cloudasset.viewer`** on the target Project or Organization (required to discover resource types).

---

## Quickstart: Running in GCP Cloud Shell

Cloud Shell has direct internet access to GitHub and comes with `python3` and `gcloud` pre-installed and authenticated:

```bash
# 1. Clone the repository
git clone https://github.com/uriarriaga/gcp-org-label-enforcer.git
cd gcp-org-label-enforcer

# 2. Set your active project and enable required APIs
gcloud config set project uri-test-491314
gcloud services enable cloudasset.googleapis.com orgpolicy.googleapis.com

# 3. Dry-Run (safe, generates YAMLs locally without cloud changes)
python3 enforce_org_label_policy.py --project uri-test-491314 --dry-run

# 4. Apply & Enforce on uri-test-491314
python3 enforce_org_label_policy.py --project uri-test-491314 --apply --enforce

# 5. Cleanup when finished testing
python3 enforce_org_label_policy.py --project uri-test-491314 --cleanup
```

---

## Detailed Usage Options

### 1. Dry-Run (Safe Discovery & Local YAML Generation)
Discover all resource types in your project and generate the constraint/policy YAML files locally without making any cloud changes:
```bash
python3 enforce_org_label_policy.py \
    --project uri-test-491314 \
    --dry-run
```

### 2. Apply & Enforce on a Single Test Project (Recommended First Step)
Register custom constraints in your parent organization, but **enforce them only on `<YOUR_PROJECT_ID>`** (requires resources to have at least 1 label):
```bash
python3 enforce_org_label_policy.py \
    --project <YOUR_PROJECT_ID> \
    --apply --enforce
```

### 3. Enforce Specific Mandatory Label Keys
Require that specific label keys (e.g., `environment`, `cost_center`, `owner`) must be present on resources:
```bash
python3 enforce_org_label_policy.py \
    --project <YOUR_PROJECT_ID> \
    --required-keys environment,cost_center,owner \
    --apply --enforce
```

### 4. Enforce Organization-Wide
Discover all resource types across the entire organization and enforce mandatory labels across **all projects** in the organization:
```bash
python3 enforce_org_label_policy.py \
    --organization <YOUR_NUMERIC_ORG_ID> \
    --required-keys environment,cost_center,owner \
    --apply --enforce
```

### 5. Cleanup / Rollback
Remove all policies applied to your project and delete the custom constraints created by this script from your organization:
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
| `--required-keys <KEYS>` | Comma-separated list of mandatory label keys (e.g., `env,owner`). If omitted, enforces `resource.labels.size() > 0` (at least one label). |
| `--output-dir <DIR>` | Directory where generated `.constraint.yaml` and `.policy.yaml` files are written. Defaults to a system temporary directory. |
| `--dry-run` | Default mode if `--apply` is not specified. Discovers resources and writes YAML files locally without calling org policy APIs. |
| `--apply` | Creates/updates the custom constraints in the parent GCP Organization. |
| `--enforce` | Applies the enforcing policy (`enforce: true`) to the target project (or organization). Implies `--apply`. |
| `--cleanup` | Deletes all policies and custom constraints created by this script (matching the prefix `custom.reqLabels*`). |

---

## How Enforcement Works in Practice

Once enforced (note that GCP Org Policies can take 2–15 minutes to propagate), attempts to create non-compliant resources are blocked at admission time:

```bash
# Creating an unlabelled bucket -> BLOCKED
$ gcloud storage buckets create gs://my-test-bucket --project=<YOUR_PROJECT_ID>
ERROR: HTTPError 412: orgpolicy:projects/_/buckets/my-test-bucket violates customConstraints/custom.reqLabelsStorageBucket.
Details: Resources of type storage.googleapis.com/Bucket must have the required labels set.

# Creating a labelled Pub/Sub topic -> ALLOWED
$ gcloud pubsub topics create my-topic --project=<YOUR_PROJECT_ID> --labels=environment=dev
Created topic [projects/<YOUR_PROJECT_ID>/topics/my-topic].
```

---

## Automated Unit & Integration Tests

The repository includes an automated test suite ([`test_label_enforcement.py`](./test_label_enforcement.py)) using Python's built-in `unittest` framework to automatically verify enforcement on all compatible resource types:

- **`TestPubSubTopic`**: Verifies unlabelled topic creation is rejected, and compliant topic creation succeeds and is deleted.
- **`TestStorageBucket`**: Verifies unlabelled bucket creation is rejected (HTTP 412), and compliant bucket creation succeeds and is deleted.
- **`TestComputeInstance`**: Verifies unlabelled VM instance creation is rejected, and compliant VM creation succeeds and is deleted.

### Running the Tests:
```bash
# Run all tests against your target project:
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID>

# Or run tests for a specific resource type:
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID> TestPubSubTopic
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID> TestStorageBucket
python3 test_label_enforcement.py --project <YOUR_PROJECT_ID> TestComputeInstance
```

