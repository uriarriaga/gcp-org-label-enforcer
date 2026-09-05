#!/usr/bin/env python3
"""Enforce mandatory labels across a GCP Organization or Project via custom Org Policies.

GCP ARCHITECTURAL RULES:
  1. Custom Constraints (the rule definitions) can ONLY be registered at the
     ORGANIZATION level: `organizations/{ORG_ID}/customConstraints/...`.
     GCP does not support defining custom constraints at the project level.
  2. Policy Enforcement (turning the constraint on/off), however, CAN be applied
     at the PROJECT level: `projects/{PROJECT_ID}/policies/...`.
     This allows you to safely test label enforcement in a sandbox project WITHOUT
     affecting the rest of your organization.
  3. Cloud Asset Inventory can be scoped to either the whole organization OR
     specifically to your project (`projects/{PROJECT_ID}`) to find only the
     resource types actually in use.

LIMITATIONS:
  * A custom constraint can target EXACTLY ONE resource type (hard GCP limit).
  * Only resource types onboarded to Custom Org Policy that expose `resource.labels`
    in their CEL schema can be enforced. Unsupported types are gracefully skipped.

Usage examples:
  # 1. Dry-run against a project (writes YAMLs locally, no cloud changes):
  python3 enforce_org_label_policy.py --project my-project-id --dry-run

  # 2. Apply custom constraints to the parent org, but ENFORCE ONLY on the project:
  python3 enforce_org_label_policy.py --project my-project-id --apply --enforce

  # 3. Require specific mandatory label keys:
  python3 enforce_org_label_policy.py --project my-project-id --apply --enforce \
      --required-keys environment,cost_center,owner

  # 4. Org-wide enforcement (applies to all projects in the org):
  python3 enforce_org_label_policy.py --organization 123456789012 --apply --enforce

  # 5. Cleanup policies and constraints created by this script:
  python3 enforce_org_label_policy.py --project my-project-id --cleanup
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

MAX_SHORT_NAME_LEN = 62
CONSTRAINT_PREFIX = "custom.reqLabels"


def run(cmd, check=True, capture=True):
  """Run a shell command, returning (returncode, stdout, stderr)."""
  # Inject --quiet into gcloud commands if not already present
  if cmd and cmd[0] == "gcloud" and "--quiet" not in cmd:
    cmd = [cmd[0], "--quiet"] + cmd[1:]
  print(f"  $ {' '.join(cmd)}")
  proc = subprocess.run(
      cmd,
      stdout=subprocess.PIPE if capture else None,
      stderr=subprocess.PIPE if capture else None,
      text=True,
  )
  if check and proc.returncode != 0:
    raise RuntimeError(
        f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
        f"STDERR: {proc.stderr}"
    )
  return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def resolve_parent_org(project_id, explicit_org=None):
  """Find the parent organization ID for a project."""
  if explicit_org:
    return explicit_org.strip()

  print(f"==> Resolving parent organization for project '{project_id}' ...")
  rc, stdout, stderr = run(
      ["gcloud", "projects", "describe", project_id, "--format=json"],
      check=False,
  )
  if rc != 0:
    raise RuntimeError(
        f"Failed to describe project '{project_id}'. If reauthentication is "
        f"needed, please run 'gcloud auth login' first.\nDetails: {stderr}"
    )

  data = json.loads(stdout)
  parent = data.get("parent") or {}
  p_type = parent.get("type", "")
  p_id = parent.get("id", "")

  if p_type == "organization":
    print(f"    Found parent organization: {p_id}")
    return p_id

  if p_type == "folder":
    print(f"    Project parent is folder {p_id}. Resolving ancestor organization...")
    rc, stdout, _ = run(
        ["gcloud", "resource-manager", "folders", "describe", p_id, "--format=json"],
        check=False,
    )
    if rc == 0:
      folder_data = json.loads(stdout)
      f_parent = folder_data.get("parent", "")
      if f_parent.startswith("organizations/"):
        org_id = f_parent.split("/")[-1]
        print(f"    Found ancestor organization: {org_id}")
        return org_id

  raise RuntimeError(
      f"Could not auto-determine parent organization for project '{project_id}' "
      f"(parent is {p_type}:{p_id}). Please provide --organization <ORG_ID> explicitly."
  )


def discover_asset_types(scope):
  """Return the sorted set of distinct asset types present in the scope.

  scope format: 'projects/{project_id}' or 'organizations/{org_id}'.
  """
  print(f"==> Discovering asset types in scope '{scope}' via Cloud Asset Inventory ...")
  cmd = [
      "gcloud", "asset", "search-all-resources",
      f"--scope={scope}",
      "--format=value(assetType)",
      "--page-size=500",
  ]
  _, stdout, _ = run(cmd)
  types = sorted({line.strip() for line in stdout.splitlines() if line.strip()})
  print(f"    Found {len(types)} distinct asset types in use.")
  return types


def make_short_name(asset_type):
  """Turn 'compute.googleapis.com/Instance' into a valid constraint short name."""
  service = asset_type.split(".")[0]
  resource = asset_type.split("/")[-1]
  service = re.sub(r"[^A-Za-z0-9]", "", service).capitalize()
  resource = re.sub(r"[^A-Za-z0-9]", "", resource)
  short = f"{CONSTRAINT_PREFIX}{service}{resource}"
  return short[:MAX_SHORT_NAME_LEN + len("custom.")]


def build_condition(required_keys):
  """Build the CEL condition enforcing presence of labels.

  actionType is ALLOW, so the resource is allowed ONLY when the condition holds.
  """
  if required_keys:
    clauses = [f"'{k}' in resource.labels" for k in required_keys]
    return " && ".join(clauses)
  return "resource.labels.size() > 0"


def build_constraint_yaml(organization, short_name, asset_type, condition, method_types=None):
  """Return the YAML text for a custom constraint (always at org level)."""
  if method_types is None:
    method_types = ["CREATE", "UPDATE"]
  methods_yaml = "\n".join(f"- {m}" for m in method_types)
  return (
      f"name: organizations/{organization}/customConstraints/{short_name}\n"
      f"resourceTypes:\n"
      f"- {asset_type}\n"
      f"methodTypes:\n"
      f"{methods_yaml}\n"
      f"condition: \"{condition}\"\n"
      f"actionType: ALLOW\n"
      f"displayName: Require labels on {asset_type}\n"
      f"description: Resources of type {asset_type} must have the required "
      f"labels set.\n"
  )


def build_policy_yaml(policy_target, short_name):
  """Return the YAML text for the enforcing org policy.

  policy_target: e.g. 'projects/my-project' or 'organizations/123456789012'
  """
  return (
      f"name: {policy_target}/policies/{short_name}\n"
      f"spec:\n"
      f"  rules:\n"
      f"  - enforce: true\n"
  )


def write_file(directory, filename, content):
  path = os.path.join(directory, filename)
  with open(path, "w") as f:
    f.write(content)
  return path


def parse_error_reason(stderr):
  """Extract a clean, human-readable reason from gcloud's error output."""
  if "undefined field 'labels'" in stderr:
    return "CEL schema does not have a 'labels' field"
  if "INVALID_CUSTOM_CONSTRAINT_RESOURCE_TYPE" in stderr or "Invalid custom constraint resource type" in stderr:
    return "Resource type not onboarded to Custom Org Policy"
  m = re.search(r"description:\s*(.+)", stderr)
  if m:
    desc = m.group(1).strip().strip("'\"")
    return desc
  lines = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
  return lines[-1] if lines else "rejected by API"


def set_custom_constraint(path, organization, short_name, asset_type, condition, out_dir):
  """Attempt to create/update a custom constraint with smart retries."""
  rc, _, stderr = run(
      ["gcloud", "org-policies", "set-custom-constraint", path],
      check=False,
  )
  if rc == 0:
    return True, ""

  # If UPDATE is not supported, retry with CREATE only
  if "Method type `UPDATE` is not supported" in stderr:
    print("    Retrying with methodTypes=[CREATE] only ...")
    create_only_yaml = build_constraint_yaml(
        organization, short_name, asset_type, condition, method_types=["CREATE"]
    )
    write_file(out_dir, f"{short_name}.constraint.yaml", create_only_yaml)
    rc2, _, stderr2 = run(
        ["gcloud", "org-policies", "set-custom-constraint", path],
        check=False,
    )
    if rc2 == 0:
      return True, ""
    stderr = stderr2

  reason = parse_error_reason(stderr)
  return False, reason


def set_policy(path, project_id=None):
  cmd = ["gcloud", "org-policies", "set-policy", path]
  if project_id:
    cmd.extend([f"--project={project_id}"])
  run(cmd)


def cleanup(organization, project_id, out_dir):
  """Delete constraints/policies created by this script."""
  target_desc = f"project '{project_id}'" if project_id else f"org '{organization}'"
  print(f"==> Cleaning up policies and constraints for {target_desc} ...")

  _, stdout, _ = run([
      "gcloud", "org-policies", "list-custom-constraints",
      f"--organization={organization}",
      "--format=value(name)",
  ], check=False)
  names = [n.strip().split("/")[-1] for n in stdout.splitlines()
           if CONSTRAINT_PREFIX in n]

  for short_name in names:
    if project_id:
      print(f"  Removing policy {short_name} from project {project_id} ...")
      run(["gcloud", "org-policies", "delete", short_name,
           f"--project={project_id}"], check=False)
    else:
      print(f"  Removing policy {short_name} from org {organization} ...")
      run(["gcloud", "org-policies", "delete", short_name,
           f"--organization={organization}"], check=False)

    print(f"  Removing custom constraint {short_name} from org {organization} ...")
    run(["gcloud", "org-policies", "delete-custom-constraint", short_name,
         f"--organization={organization}"], check=False)

  print(f"    Cleanup completed for {len(names)} item(s).")


def main():
  parser = argparse.ArgumentParser(
      description="Create label-enforcing custom Org Policies for GCP resources.")
  parser.add_argument("--project", default=None,
                      help="Target a specific GCP Project ID. Scopes asset search "
                           "and policy enforcement to this project.")
  parser.add_argument("--organization", default=None,
                      help="Numeric GCP organization ID (e.g. 123456789012). "
                           "Required for constraint registration. Auto-detected if "
                           "--project is supplied.")
  parser.add_argument("--required-keys", default="",
                      help="Comma-separated label keys that must be present. "
                           "If omitted, requires at least one label of any kind.")
  parser.add_argument("--output-dir", default=None,
                      help="Directory to write generated YAML files. Defaults to a temp dir.")
  parser.add_argument("--apply", action="store_true",
                      help="Actually create the custom constraints in the org via gcloud.")
  parser.add_argument("--enforce", action="store_true",
                      help="Also set an enforcing policy (on project if --project is set, "
                           "or on org if only --organization is set). Implies --apply.")
  parser.add_argument("--dry-run", action="store_true",
                      help="Only discover types and generate YAML locally; no cloud changes.")
  parser.add_argument("--cleanup", action="store_true",
                      help="Delete policies/constraints created by this script.")
  args = parser.parse_args()

  if not args.project and not args.organization:
    parser.error("At least one of --project or --organization must be specified.")

  org_id = None
  if args.dry_run and not args.organization and not args.apply:
    try:
      org_id = resolve_parent_org(args.project, args.organization)
    except Exception:
      org_id = "YOUR_ORG_ID"
      print(f"    [dry-run] Using placeholder org ID: {org_id}")
  else:
    if args.project:
      org_id = resolve_parent_org(args.project, args.organization)
    else:
      org_id = args.organization.strip()

  if args.cleanup:
    cleanup(org_id, args.project, args.output_dir or ".")
    return

  if args.enforce:
    args.apply = True
  if not args.apply and not args.dry_run:
    print("Neither --apply nor --dry-run specified; defaulting to --dry-run.")
    args.dry_run = True

  if args.project:
    cai_scope = f"projects/{args.project}"
    policy_target = f"projects/{args.project}"
    print(f"==> Target Project      : {args.project}")
    print(f"==> Parent Organization : {org_id}")
    print(f"==> CAI Scan Scope      : {cai_scope}")
    print(f"==> Policy Target       : {policy_target} (isolated to {args.project}!)")
  else:
    cai_scope = f"organizations/{org_id}"
    policy_target = f"organizations/{org_id}"
    print(f"==> Target Organization : {org_id}")
    print(f"==> CAI Scan Scope      : {cai_scope}")
    print(f"==> Policy Target       : {policy_target} (org-wide)")

  out_dir = args.output_dir or tempfile.mkdtemp(prefix="label_policies_")
  os.makedirs(out_dir, exist_ok=True)
  print(f"==> Output directory    : {out_dir}")

  required_keys = [k.strip() for k in args.required_keys.split(",") if k.strip()]
  condition = build_condition(required_keys)
  print(f"==> CEL Condition       : {condition}")

  asset_types = discover_asset_types(cai_scope)

  created, skipped = [], []
  for asset_type in asset_types:
    short_name = make_short_name(asset_type)
    constraint_yaml = build_constraint_yaml(
        org_id, short_name, asset_type, condition)
    c_path = write_file(out_dir, f"{short_name}.constraint.yaml", constraint_yaml)

    if args.dry_run:
      policy_yaml = build_policy_yaml(policy_target, short_name)
      write_file(out_dir, f"{short_name}.policy.yaml", policy_yaml)
      print(f"  [dry-run] Generated constraint + policy for {asset_type}")
      created.append((asset_type, short_name))
      continue

    print(f"==> Registering custom constraint for {asset_type} (at org {org_id}) ...")
    ok, reason = set_custom_constraint(
        c_path, org_id, short_name, asset_type, condition, out_dir
    )
    if not ok:
      print(f"    SKIP {asset_type}: {reason}")
      skipped.append((asset_type, reason))
      continue

    if args.enforce:
      policy_yaml = build_policy_yaml(policy_target, short_name)
      p_path = write_file(out_dir, f"{short_name}.policy.yaml", policy_yaml)
      set_policy(p_path, project_id=args.project)
      scope_desc = f"project {args.project}" if args.project else f"org {org_id}"
      print(f"    ENFORCED on {scope_desc}: {asset_type}")
    else:
      print(f"    CREATED constraint (not enforced): {asset_type}")
    created.append((asset_type, short_name))

  # Summary
  print("\n================ SUMMARY ================")
  print(f"Total asset types in scope   : {len(asset_types)}")
  print(f"Constraints created/generated: {len(created)}")
  print(f"Skipped (unsupported)        : {len(skipped)}")
  if skipped:
    print("\nSkipped resource types:")
    for t, r in skipped:
      print(f"  - {t}  ({r})")
  print(f"\nGenerated YAMLs are in: {out_dir}")
  if args.dry_run:
    target_flag = f"--project {args.project}" if args.project else f"--organization {org_id}"
    print(f"\nThis was a DRY RUN. To apply and enforce, re-run with:")
    print(f"  python3 enforce_org_label_policy.py {target_flag} --apply --enforce")


if __name__ == "__main__":
  try:
    main()
  except RuntimeError as e:
    print(f"\nERROR: {e}", file=sys.stderr)
    sys.exit(1)
