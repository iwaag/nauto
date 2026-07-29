# Nauto

This repository is a Nautobot Git Repository that provides Jobs.

## Nautobot Setup

The source of truth for prerequisite Nautobot objects is [seed/home_cluster.yaml](seed/home_cluster.yaml). The Nautobot Job reads this YAML file and creates or updates the required objects.

This repository is structured so it can be used as a Nautobot Git Repository that provides Jobs.

```text
.
├── __init__.py
├── jobs
│   ├── __init__.py
│   ├── ai_resource_review.py
│   ├── ingest_nodeutils_inventory.py
│   └── seed_home_cluster.py
└── seed
    ├── nodeutils_ingest.yaml
    └── home_cluster.yaml
```

When adding this repository to Nautobot, include `Jobs` in the repository `provides` setting. The Git repository `slug` becomes part of the Job class path, so do not change it after you start using the repository. The target branch must not be empty; it needs at least one commit.

Nautobot Git Repository Jobs requirements:

- The repository root contains `__init__.py`
- The repository root contains `jobs/`
- `jobs/` contains `jobs/__init__.py`
- [jobs/__init__.py](jobs/__init__.py) imports the Job class and explicitly registers it with `register_jobs()`
- The seed data used by the Job is stored at `seed/home_cluster.yaml`, relative to the repository root

In this repository, [jobs/seed_home_cluster.py](jobs/seed_home_cluster.py) contains the Job logic and [jobs/__init__.py](jobs/__init__.py) is the registration point. `Seed Home Cluster` seeds native Nautobot prerequisites only (Location/Role/Status/Device Type/Tag/Custom Field data below); it does not import or write any nintent `IntentSource` or `DesiredService` row.
[jobs/ingest_nodeutils_inventory.py](jobs/ingest_nodeutils_inventory.py) reads a batch of `nodeutils collect` reports from API input, validates them, applies [seed/nodeutils_ingest.yaml](seed/nodeutils_ingest.yaml), and creates or updates Devices with Nautobot-side credentials only.
[jobs/ai_resource_review.py](jobs/ai_resource_review.py) contains a Job Hook Receiver that can call an Ollama-compatible LLM endpoint after Device inventory updates. The review includes service placement and Docker snapshot fields when they are present, but it should not be treated as a live capacity signal.
`seed/intent_sources.yaml` is retained only until Phase 4 removes private desired state from Git.
It is no longer read by Nautobot. Source-derived service/dependency discovery is owned entirely by
nintent's `Analyze Intent Sources` Job; this repository has no candidate-generation Job or output file.

Nautobot-side workflow:

1. Add this repository under Nautobot Git Repositories.
2. Include `Jobs` in `provides`.
3. Sync the repository.
4. Enable `Home Inventory` / `Seed Home Cluster` from Jobs.
5. Run `Seed Home Cluster` with `dry_run=true` first, then apply with `dry_run=false`.
6. Run `Ingest Nodeutils Inventory` with `dry_run=true` against one report, inspect logs, then apply with `dry_run=false`.

If Job record updates do not appear in your environment, run `nautobot-server post_upgrade` on the Nautobot server and restart the web / worker processes as needed.

The seed Job creates the main objects required by nodeutils inventory ingest:

- Location Type: `Home`
- Location: `Home`
- Role: `linux-workstation`, `macos-workstation`, `workstation`
- Status: `Active`
- Manufacturer: `Apple`, `Generic`, and others
- Device Type: `Mac`, `Ubuntu PC`
- Tag: `self-registered`, `home`
- Device Custom Fields

The Device Custom Fields include:

- `owner`
- `purpose`
- `last_seen`
- `os_name`
- `os_version`
- `kernel_version`
- `architecture`
- `cpu_model`
- `cpu_cores`
- `memory_gb`
- `gpu_count`
- `gpu_models`
- `gpu_memory_gb`
- `gpu_accelerator_summary`
- `disk_total_gb`
- `serial_number`
- `primary_mac_address`
- `primary_ip_address`
- `network_interface`
- `host_system`
- `inventory_source`
- `ai_resource_summary`
- `agent_task_state`
- `ai_resource_review`
- `ai_resource_review_updated_at`
- `ai_resource_review_model`
- `ai_resource_review_source_hash`
- `observed_services`
- `docker_engine_state`
- `docker_container_running_count`
- `docker_container_total_count`
- `docker_compose_projects`
- `docker_published_ports`
- `docker_service_summary`
- `service_inventory_updated_at`
- `inventory_raw_json`

If the required Custom Fields do not exist in Nautobot, Device create/update calls can fail.

Observed service fields on a Device are host-local facts, not the cluster-wide desired service catalog. nodeutils reports `observed_services.ollama` when it sees a running Docker container or systemd unit, but that observation never decides desired service-group membership; desired placement lives in nintent `DesiredServicePlacement` records. Live capacity checks such as GPU utilization, VRAM pressure, CPU load, and request latency should come from a monitoring system before an automation agent sends work to that endpoint.

Cluster-level desired services and their placements are persisted in nintent (`DesiredService` and `DesiredServicePlacement`). They answer "what should run where?" rather than "what does this Device currently provide?" Service-placement drift is computed only by `nctl drift`; nauto persists observations but does not maintain a second drift engine.

Repository-driven service discovery (catalog-info.yaml fetching, dependency extraction) is
nintent's `Analyze Intent Sources` Job, run against `IntentSource` rows imported from
`seed/intent_sources.yaml`. It defaults to a zero-write preview (`apply=false`) and requires
`apply=true` to persist `DesiredService`/`DesiredDependency` rows. This repository no longer has
its own candidate-generation Job, input file, or generated-output file.

## Configuration

To adjust the prerequisite Nautobot objects:

```bash
editor seed/home_cluster.yaml
```

To adjust central policy for nodeutils report ingest:

```bash
editor seed/nodeutils_ingest.yaml
```

This policy controls supported report schema versions, default Nautobot objects,
whether reports may create or update Devices, system-to-role/device-type maps,
and which `self_reported` fields may be copied into custom fields.

To adjust name-reserved bootstrap hosts:

```bash
editor seed/intent_sources.yaml
```

This file declares desired nodes, endpoints, services, service placements, and
optional node operational overrides. Ordinary observed Linux/macOS hosts do not
need an override row: nctl derives policy and OS from fresh nodeutils facts and
selects a unique usable local endpoint (or unique primary). Keep override rows
only for genuine exceptions such as declared HAOS, non-default power/laptop
behavior, a non-default Ansible port, or a forced endpoint/path.

Bootstrap inventory generation uses only the eligible desired nodes and their
mDNS endpoints. Production service groups come exclusively from active
placements and the Ansible-owned deployment-profile map; observed facts supply
only the production exporter's audited actual-state fields. The removed
`desired_node_operational_configs` YAML root is invalid; use
`desired_node_operational_overrides`.

Host-side scripts and their local configuration examples live in the separate `nodeutils` repository.

## Nodeutils Ingest

Generate reports on hosts with:

```bash
uv run nodeutils collect --output /var/lib/nodeutils/inventory.json
```

Submit reports to `Home Inventory` / `Ingest Nodeutils Inventory` as one batch
payload. The Job does not read host or container filesystem paths for
nodeutils reports.

- `report_batch`: JSON/YAML text with a top-level `reports` list
- `policy_file`: defaults to `seed/nodeutils_ingest.yaml`
- `dry_run`: keep `true` first to log matched Device, action, report hash, and changed fields

Example `report_batch`:

```yaml
reports:
  - source: agpc
    text: |
      {"schema_version": "nodeutils.inventory.v2", "...": "..."}
  - source: agstudio
    text: |
      {"schema_version": "nodeutils.inventory.v2", "...": "..."}
```

The ingestor rejects malformed, stale, oversized, or unsupported-schema reports.
Location, role, status, device type, manufacturer, and tags come from
server-side policy, not from host authority.

For `nodeutils.inventory.v2`, nested
`facts.services.observed_services.*.managed_files` metadata is retained
unchanged in the existing `observed_services` custom field. This is digest/path/status/size/time
metadata only; nauto does not store managed-file contents or create a separate applied-digest model.

The AI resource review Job Hook uses these Nautobot server environment variables:

```bash
AI_RESOURCE_REVIEW_URL=http://localhost:11434/api/generate
AI_RESOURCE_REVIEW_MODEL=llama3.1:8b
AI_RESOURCE_REVIEW_TIMEOUT=30
# Optional, for debugging prompt/model behavior. Logs a bounded prompt preview.
AI_RESOURCE_REVIEW_LOG_PROMPT=false
```

The Job sends `think=false` to Ollama so thinking-capable models return the final review in `response` instead of spending the request on a separate `thinking` trace.

After syncing this repository and running `Seed Home Cluster` with `dry_run=false`, create a Nautobot Job Hook for `dcim.device` create and update events and select the `AI Resource Review` job. The job stores the LLM output in `ai_resource_review` and skips regeneration when the selected source facts have not changed.

## Current Scope

This repository creates prerequisite Nautobot objects and ingests nodeutils inventory reports.

## Notes

This repository uses the `YAML + Nautobot Job` approach for repeatable home inventory setup.

Nautobot 2.0 or later is assumed. The data model uses Location / Location Type, not the older Site / Region model, so both the seed data and ingest policy use `location`.
