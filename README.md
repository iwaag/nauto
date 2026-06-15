# Nautobot Self Register

This repository contains a host self-registration tool for Ubuntu/Linux and macOS systems. It collects local inventory data and creates or updates the current machine as a Nautobot Device.

It also includes a Nautobot Job that seeds the prerequisite Nautobot objects from YAML, so the self-registration script can run against a clean Nautobot instance.

## Supported Hosts

- Ubuntu / Linux
- macOS
- Windows is not supported

## Dependencies

Install dependencies with `uv`:

```bash
uv sync
```

If you install dependencies directly with `pip`, install `psutil` and `PyYAML`.

## Nautobot Setup

The source of truth for prerequisite Nautobot objects is [seed/home_cluster.yaml](seed/home_cluster.yaml). The Nautobot Job reads this YAML file and creates or updates the required objects.

This repository is structured so it can be used as a Nautobot Git Repository that provides Jobs.

```text
.
├── __init__.py
├── jobs
│   ├── __init__.py
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
- `disk_total_gb`
- `serial_number`
- `primary_mac_address`
- `primary_ip_address`
- `inventory_source`
- `inventory_raw_json`

If the required Custom Fields do not exist in Nautobot, Device create/update calls can fail.

## Configuration

To adjust the prerequisite Nautobot objects:

```bash
editor seed/home_cluster.yaml
```

For host self-registration:

```bash
cp .env.example .env
editor .env
```

`self_inventory.yaml` is optional. If no config file exists, the script registers the host with default values and locally detected inventory data.

Default values:

- Location: `Home`
- Status: `Active`
- Role: `linux-workstation` on Linux, `macos-workstation` on macOS
- Tags: `self-registered`, `home`

Create `self_inventory.yaml` only when you need local overrides:

```bash
cp example.self_inventory.yaml self_inventory.yaml
editor self_inventory.yaml
```

Provide `NAUTOBOT_URL` and `NAUTOBOT_TOKEN` via `.env` or shell environment variables. When using `.env`, load it with `uv run --env-file .env ...`. Do not store API tokens directly in `self_inventory.yaml`.

## Usage

Print collected inventory:

```bash
uv run --env-file .env nautobot-self-register --json
```

Print the planned Nautobot Device payload:

```bash
uv run --env-file .env nautobot-self-register --dry-run
```

Create or update the Nautobot Device:

```bash
uv run --env-file .env nautobot-self-register --verbose
```

Existing Devices are matched by serial number first. If no serial number is available, the script falls back to the Device name, which defaults to the local hostname.

## Scheduled Run Example

Ubuntu cron example:

```cron
0 3 * * * cd /path/to/nautobot-home-inventory && uv run --env-file .env nautobot-self-register
```

Use an equivalent `launchd` schedule on macOS.

## Current Scope

This is a Phase 1 implementation. It creates or updates the Nautobot Device and stores the main collected data in Device Custom Fields.

Interface and IP Address creation in Nautobot IPAM is intentionally left for a later phase.

## Notes

This repository uses the `YAML + Nautobot Job` approach for repeatable home inventory setup.

Nautobot 2.0 or later is assumed. The data model uses Location / Location Type, not the older Site / Region model, so both the seed data and the self-registration script use `location`.
