"""Expose Nautobot Jobs from this repository."""

from nautobot.apps.jobs import register_jobs

from .seed_home_cluster import SeedHomeCluster

name = "Home Inventory"

register_jobs(SeedHomeCluster)

__all__ = ["SeedHomeCluster"]
