"""Expose Nautobot Jobs from this repository."""

from nautobot.apps.jobs import register_jobs

from .ai_resource_review import AIResourceReview
from .generate_desired_services import GenerateDesiredServices
from .seed_home_cluster import SeedHomeCluster
from .service_placement_review import ServicePlacementReview

name = "Home Inventory"

register_jobs(SeedHomeCluster, AIResourceReview, ServicePlacementReview, GenerateDesiredServices)

__all__ = ["SeedHomeCluster", "AIResourceReview", "ServicePlacementReview", "GenerateDesiredServices"]
