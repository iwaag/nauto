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
│   └── seed_home_cluster.py
└── seed
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
[jobs/ai_resource_review.py](jobs/ai_resource_review.py) contains a Job Hook Receiver that can call an Ollama-compatible LLM endpoint after Device self-registration updates.

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
- `inventory_raw_json`

If the required Custom Fields do not exist in Nautobot, Device create/update calls can fail.

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

## Current Scope

This repository creates or updates prerequisite Nautobot objects for home inventory self-registration.

## Notes

This repository uses the `YAML + Nautobot Job` approach for repeatable home inventory setup.

Nautobot 2.0 or later is assumed. The data model uses Location / Location Type, not the older Site / Region model, so both the seed data and the self-registration script use `location`.
