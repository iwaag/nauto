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
│   ├── service_placement_review.py
│   └── seed_home_cluster.py
└── seed
    ├── desired_services.yaml
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
[jobs/ai_resource_review.py](jobs/ai_resource_review.py) contains a Job Hook Receiver that can call an Ollama-compatible LLM endpoint after Device self-registration updates. The review includes service placement and Docker snapshot fields when they are present, but it should not be treated as a live capacity signal.
[jobs/service_placement_review.py](jobs/service_placement_review.py) reviews the cluster-level desired service catalog in [seed/desired_services.yaml](seed/desired_services.yaml) against self-reported Device facts and logs a JSON placement review.
[jobs/generate_desired_services.py](jobs/generate_desired_services.py) reads [seed/service_repositories.yaml](seed/service_repositories.yaml), fetches selected repository files without a full clone, and can write `seed/desired_services.generated.yaml`.

Nautobot-side workflow:

1. Add this repository under Nautobot Git Repositories.
2. Include `Jobs` in `provides`.
3. Sync the repository.
4. Enable `Home Inventory` / `Seed Home Cluster` from Jobs.
5. Run with `dry_run=true` first, then apply with `dry_run=false`.

If Job record updates do not appear in your environment, run `nautobot-server post_upgrade` on the Nautobot server and restart the web / worker processes as needed.

The seed Job creates the main objects required by self-registration:

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
- `inventory_source`
- `ai_resource_summary`
- `agent_task_state`
- `ai_resource_review`
- `ai_resource_review_updated_at`
- `ai_resource_review_model`
- `ai_resource_review_source_hash`
- `service_roles`
- `preferred_services`
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

Service placement fields on a Device are host-local facts or preferences, not the cluster-wide desired service catalog. For example, a Device can declare that `ollama` is normally available at `http://pc1:11434` with a `use_existing_first` startup policy, and self-registration can report `observed_services.ollama` when it sees a running Docker container or systemd unit. Live capacity checks such as GPU utilization, VRAM pressure, CPU load, and request latency should come from a monitoring system before an automation agent sends work to that endpoint.

Cluster-level desired services live independently in [seed/desired_services.yaml](seed/desired_services.yaml). This file answers "what should exist somewhere?" rather than "what does this Device currently provide?"

Repository-driven service discovery starts from [seed/service_repositories.yaml](seed/service_repositories.yaml). Only `url` is required:

```yaml
service_repositories:
  - url: "https://github.com/example/hatchet-stack"
  - url: "https://github.com/example/ollama-service"
```

The `Generate Desired Services` Job resolves default branches where possible, fetches only `catalog-info.yaml` and a short list of basic files such as `README.md`, and marks repositories without catalog metadata as `insufficient` for later review. Run it with `dry_run=true` first. With `dry_run=false`, it writes `seed/desired_services.generated.yaml`; keep [seed/desired_services.yaml](seed/desired_services.yaml) as the approved catalog until generated output has been reviewed or a merge workflow is added.

Example `preferred_services` value:

```json
{
  "ollama": {
    "service_role": "ai-inference",
    "preferred": true,
    "endpoint": "http://pc1:11434",
    "startup_policy": "use_existing_first",
    "fallback_policy": "start_new_if_capacity_available",
    "managed_by": "systemd"
  }
}
```

## Configuration

To adjust the prerequisite Nautobot objects:

```bash
editor seed/home_cluster.yaml
```

Host-side scripts and their local configuration examples live in the separate `nodeutils` repository.

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

Run `Service Placement Review` manually at first. With `dry_run=true`, it loads the desired service catalog and Device facts and logs deterministic status without calling the LLM. With `dry_run=false`, it requests a JSON placement review from the configured LLM endpoint.

## Current Scope

This repository creates or updates prerequisite Nautobot objects for home inventory self-registration.

## Notes

This repository uses the `YAML + Nautobot Job` approach for repeatable home inventory setup.

Nautobot 2.0 or later is assumed. The data model uses Location / Location Type, not the older Site / Region model, so both the seed data and the self-registration script use `location`.
