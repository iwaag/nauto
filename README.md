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
│   ├── generate_desired_services.py
│   ├── ingest_nodeutils_inventory.py
│   ├── service_placement_review.py
│   └── seed_home_cluster.py
└── seed
    ├── intent_sources.yaml
    ├── nodeutils_ingest.yaml
    ├── service_repositories.yaml
    └── home_cluster.yaml
```

When adding this repository to Nautobot, include `Jobs` in the repository `provides` setting. The Git repository `slug` becomes part of the Job class path, so do not change it after you start using the repository. The target branch must not be empty; it needs at least one commit.

Nautobot Git Repository Jobs requirements:

- The repository root contains `__init__.py`
- The repository root contains `jobs/`
- `jobs/` contains `jobs/__init__.py`
- [jobs/__init__.py](jobs/__init__.py) imports the Job class and explicitly registers it with `register_jobs()`
- The seed data used by the Job is stored at `seed/home_cluster.yaml`, relative to the repository root

In this repository, [jobs/seed_home_cluster.py](jobs/seed_home_cluster.py) contains the Job logic and [jobs/__init__.py](jobs/__init__.py) is the registration point.
[jobs/ingest_nodeutils_inventory.py](jobs/ingest_nodeutils_inventory.py) reads a batch of `nodeutils collect` reports from API input, validates them, applies [seed/nodeutils_ingest.yaml](seed/nodeutils_ingest.yaml), and creates or updates Devices with Nautobot-side credentials only.
[jobs/ai_resource_review.py](jobs/ai_resource_review.py) contains a Job Hook Receiver that can call an Ollama-compatible LLM endpoint after Device inventory updates. The review includes service placement and Docker snapshot fields when they are present, but it should not be treated as a live capacity signal.
[jobs/service_placement_review.py](jobs/service_placement_review.py) reads the persisted nintent `DesiredService` and active `DesiredServicePlacement` records, compares them against observed Device facts, and logs a deterministic per-service drift report (and an optional JSON placement review). It is advisory only and never mutates placements.
[jobs/generate_desired_services.py](jobs/generate_desired_services.py) reads [seed/service_repositories.yaml](seed/service_repositories.yaml), fetches selected repository files without a full clone, and can write `seed/desired_services.generated.yaml`.
[seed/intent_sources.yaml](seed/intent_sources.yaml) is the nintent input for name-reserved DesiredNodes and primary mDNS endpoints. It is used before nodeutils collection to generate the minimal Ansible bootstrap inventory.

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

Cluster-level desired services and their placements are persisted in nintent (`DesiredService` and `DesiredServicePlacement`). They answer "what should run where?" rather than "what does this Device currently provide?" The Service Placement Review reads those models directly; there is no file catalog acting as a second source of truth.

Repository-driven service discovery starts from [seed/service_repositories.yaml](seed/service_repositories.yaml). Only `url` is required:

```yaml
service_repositories:
  - url: "https://github.com/example/hatchet-stack"
  - url: "https://github.com/example/ollama-service"
```

The `Generate Desired Services` Job resolves default branches where possible, fetches only `catalog-info.yaml` and a short list of basic files such as `README.md`, and marks repositories without catalog metadata as `insufficient` for later review. Run it with `dry_run=true` first. With `dry_run=false`, it writes `seed/desired_services.generated.yaml` as a candidate proposal for operator review; it does not become authoritative until reviewed and persisted into nintent.

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
typed node operational configuration. Bootstrap inventory generation uses only
the eligible desired nodes and their mDNS endpoints. Production service groups
come exclusively from active placements and the Ansible-owned deployment-profile
map; observed facts supply only the production exporter's audited actual-state
fields.

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
      {"schema_version": "nodeutils.inventory.v1", "...": "..."}
  - source: agstudio
    text: |
      {"schema_version": "nodeutils.inventory.v1", "...": "..."}
```

The ingestor rejects malformed, stale, oversized, or unsupported-schema reports.
Location, role, status, device type, manufacturer, and tags come from
server-side policy, not from host authority.

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

The Service Placement Review Job reuses the AI resource review LLM settings by default when `dry_run=false`:

```bash
AI_RESOURCE_REVIEW_URL=http://localhost:11434/api/generate
AI_RESOURCE_REVIEW_MODEL=llama3.1:8b
AI_RESOURCE_REVIEW_TIMEOUT=30
```

Use these optional variables only when service placement should call a different endpoint, model, or timeout:

```bash
SERVICE_PLACEMENT_REVIEW_URL=http://localhost:11434/api/generate
SERVICE_PLACEMENT_REVIEW_MODEL=llama3.1:8b
SERVICE_PLACEMENT_REVIEW_TIMEOUT=45
# Optional, for debugging prompt/model behavior. Logs a bounded prompt preview.
SERVICE_PLACEMENT_REVIEW_LOG_PROMPT=false
```

Run `Service Placement Review` manually at first. With `dry_run=true`, it reads the persisted desired services and active placements plus observed Device facts and logs the deterministic drift report without calling the LLM. With `dry_run=false`, it additionally requests a JSON placement review from the configured LLM endpoint. The report separates `missing_service`, `wrong_node`, `stale_observation`, `insufficient_actual_facts`, and `os_mismatch` drift, and a missing or stopped observation is reported as drift rather than removing the placement from the desired convergence target.

## Current Scope

This repository creates prerequisite Nautobot objects and ingests nodeutils inventory reports.

## Notes

This repository uses the `YAML + Nautobot Job` approach for repeatable home inventory setup.

Nautobot 2.0 or later is assumed. The data model uses Location / Location Type, not the older Site / Region model, so both the seed data and ingest policy use `location`.
