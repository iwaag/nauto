"""Expose Nautobot Jobs from this repository."""

from nautobot.apps.jobs import register_jobs

from .ingest_agent_registration import IngestAgentRegistration
from .ingest_nodeutils_inventory import IngestNodeutilsInventory
from .seed_home_cluster import SeedHomeCluster

name = "Home Inventory"

register_jobs(SeedHomeCluster, IngestNodeutilsInventory, IngestAgentRegistration)

__all__ = [
    "SeedHomeCluster",
    "IngestNodeutilsInventory",
    "IngestAgentRegistration",
]
