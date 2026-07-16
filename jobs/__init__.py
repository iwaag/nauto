"""Expose Nautobot Jobs from this repository."""

from nautobot.apps.jobs import register_jobs

from .ai_resource_review import AIResourceReview
from .generate_desired_services import GenerateDesiredServices
from .ingest_nodeutils_inventory import IngestNodeutilsInventory
from .seed_home_cluster import SeedHomeCluster

name = "Home Inventory"

register_jobs(SeedHomeCluster, IngestNodeutilsInventory, AIResourceReview, GenerateDesiredServices)

__all__ = [
    "SeedHomeCluster",
    "IngestNodeutilsInventory",
    "AIResourceReview",
    "GenerateDesiredServices",
]
