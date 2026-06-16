"""Generate desired service candidates from lightweight repository metadata."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import yaml
from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, StringVar

DEFAULT_CATALOG_PATHS = ("catalog-info.yaml", "backstage/catalog-info.yaml")
DEFAULT_BASIC_FILE_PATHS = (
    "README.md",
    "readme.md",
    "package.json",
    "docker-compose.yml",
    "compose.yaml",
    "Chart.yaml",
)
DEFAULT_REFS = ("HEAD", "main", "master")
MAX_FETCH_BYTES = 512_000


@dataclass(frozen=True)
class RepositorySpec:
    url: str
    enabled: bool
    ref: str | None
    catalog_paths: list[str]
    basic_file_paths: list[str]
    raw_url_template: str | None
    service_hint: str | None
    owner: str | None


@dataclass(frozen=True)
class FetchedFile:
    path: str
    ref: str
    text: str
    source: str


@dataclass(frozen=True)
class CatalogDependency:
    raw_ref: str
    kind: str
    namespace: str
    name: str
    dependency_type: str
    resolution_status: str = "unresolved"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _plain_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_plain_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in sorted(value.items())}
    return str(value)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "service"


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "nauto-generate-desired-services",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    return headers


def _request_text(url: str, timeout: float, headers: dict[str, str] | None = None) -> tuple[str, int]:
    request_headers = dict(_headers())
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_FETCH_BYTES + 1)
    if len(raw) > MAX_FETCH_BYTES:
        raise ValueError(f"response exceeded {MAX_FETCH_BYTES} bytes")
    return raw.decode("utf-8", errors="replace"), len(raw)


def _load_repository_specs(path: Path) -> list[RepositorySpec]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_items = data.get("service_repositories", [])
    if not isinstance(raw_items, list):
        raise ValueError("service_repositories must be a list")

    specs = []
    for item in raw_items:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict) or not item.get("url"):
            continue
        specs.append(
            RepositorySpec(
                url=str(item["url"]),
                enabled=bool(item.get("enabled", True)),
                ref=str(item["ref"]) if item.get("ref") else None,
                catalog_paths=[str(path) for path in item.get("catalog_paths", DEFAULT_CATALOG_PATHS)],
                basic_file_paths=[str(path) for path in item.get("basic_file_paths", DEFAULT_BASIC_FILE_PATHS)],
                raw_url_template=str(item["raw_url_template"]) if item.get("raw_url_template") else None,
                service_hint=str(item["service_hint"]) if item.get("service_hint") else None,
                owner=str(item["owner"]) if item.get("owner") else None,
            )
        )
    return specs


def _github_owner_repo(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    repo = parts[1].removesuffix(".git")
    return parts[0], repo


def _gitlab_project_path(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if "gitlab" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    if parts[-1].endswith(".git"):
        parts[-1] = parts[-1].removesuffix(".git")
    return parsed.netloc, "/".join(parts)


def _candidate_refs(spec: RepositorySpec, default_branch: str | None) -> list[str]:
    refs = []
    if spec.ref:
        refs.append(spec.ref)
    if default_branch:
        refs.append(default_branch)
    refs.extend(DEFAULT_REFS)
    deduped = []
    for ref in refs:
        if ref and ref not in deduped:
            deduped.append(ref)
    return deduped


class RepositoryFileFetcher:
    """Fetch selected files from a repository without cloning the repository."""

    def __init__(self, timeout: float):
        self.timeout = timeout

    def default_branch(self, spec: RepositorySpec) -> str | None:
        github = _github_owner_repo(spec.url)
        if github:
            owner, repo = github
            try:
                text, _ = _request_text(f"https://api.github.com/repos/{owner}/{repo}", self.timeout)
                data = json.loads(text)
                branch = data.get("default_branch")
                return str(branch) if branch else None
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                return None

        gitlab = _gitlab_project_path(spec.url)
        if gitlab:
            host, project_path = gitlab
            project_id = quote(project_path, safe="")
            try:
                text, _ = _request_text(f"https://{host}/api/v4/projects/{project_id}", self.timeout)
                data = json.loads(text)
                branch = data.get("default_branch")
                return str(branch) if branch else None
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                return None

        return None

    def fetch_first(self, spec: RepositorySpec, paths: list[str], refs: list[str]) -> FetchedFile | None:
        for path in paths:
            for ref in refs:
                fetched = self.fetch_file(spec, path, ref)
                if fetched is not None:
                    return fetched
        return None

    def fetch_many(self, spec: RepositorySpec, paths: list[str], refs: list[str]) -> list[FetchedFile]:
        fetched_files = []
        for path in paths:
            for ref in refs:
                fetched = self.fetch_file(spec, path, ref)
                if fetched is not None:
                    fetched_files.append(fetched)
                    break
        return fetched_files

    def fetch_file(self, spec: RepositorySpec, path: str, ref: str) -> FetchedFile | None:
        try:
            if spec.raw_url_template:
                return self._fetch_raw_template(spec, path, ref)

            github = _github_owner_repo(spec.url)
            if github:
                return self._fetch_github(github[0], github[1], path, ref)

            gitlab = _gitlab_project_path(spec.url)
            if gitlab:
                return self._fetch_gitlab(gitlab[0], gitlab[1], path, ref)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError, KeyError):
            return None
        return None

    def _fetch_raw_template(self, spec: RepositorySpec, path: str, ref: str) -> FetchedFile:
        url = spec.raw_url_template.format(ref=quote(ref, safe=""), path=quote(path))
        text, _ = _request_text(url, self.timeout)
        return FetchedFile(path=path, ref=ref, text=text, source=url)

    def _fetch_github(self, owner: str, repo: str, path: str, ref: str) -> FetchedFile:
        api_path = quote(path)
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{api_path}?ref={quote(ref, safe='')}"
        text, _ = _request_text(url, self.timeout, {"Accept": "application/vnd.github+json"})
        data = json.loads(text)
        if isinstance(data, list) or data.get("type") != "file":
            raise ValueError("GitHub contents response did not describe a file")
        if data.get("encoding") == "base64" and isinstance(data.get("content"), str):
            raw = base64.b64decode(data["content"], validate=False)
            if len(raw) > MAX_FETCH_BYTES:
                raise ValueError(f"file exceeded {MAX_FETCH_BYTES} bytes")
            file_text = raw.decode("utf-8", errors="replace")
        elif data.get("download_url"):
            file_text, _ = _request_text(str(data["download_url"]), self.timeout)
        else:
            raise ValueError("GitHub file did not include content")
        return FetchedFile(path=path, ref=ref, text=file_text, source=url)

    def _fetch_gitlab(self, host: str, project_path: str, path: str, ref: str) -> FetchedFile:
        project_id = quote(project_path, safe="")
        file_path = quote(path, safe="")
        url = f"https://{host}/api/v4/projects/{project_id}/repository/files/{file_path}/raw?ref={quote(ref, safe='')}"
        text, _ = _request_text(url, self.timeout)
        return FetchedFile(path=path, ref=ref, text=text, source=url)


def _catalog_entities(catalog_file: FetchedFile) -> list[dict[str, Any]]:
    entities = []
    for doc in yaml.safe_load_all(catalog_file.text):
        if not isinstance(doc, dict):
            continue
        entities.append(_plain_value(doc))
    return entities


def _parse_dependency_ref(raw_ref: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw_ref, str):
        return None, {"raw_ref": str(raw_ref), "reason": "invalid_entity_ref"}

    raw_ref = raw_ref.strip()
    if not raw_ref:
        return None, {"raw_ref": raw_ref, "reason": "invalid_entity_ref"}

    kind = "component"
    entity_ref = raw_ref
    if ":" in raw_ref:
        kind_part, entity_ref = raw_ref.split(":", 1)
        kind_part = kind_part.strip().lower()
        if not kind_part:
            return None, {"raw_ref": raw_ref, "reason": "invalid_entity_ref"}
        kind = kind_part

    entity_ref = entity_ref.strip()
    if not entity_ref or entity_ref.count("/") > 1:
        return None, {"raw_ref": raw_ref, "reason": "invalid_entity_ref"}

    namespace = "default"
    name = entity_ref
    if "/" in entity_ref:
        namespace, name = [part.strip() for part in entity_ref.split("/", 1)]

    kind = kind.strip().lower()
    namespace = namespace.strip().lower()
    name = name.strip()
    if not kind or not namespace or not name:
        return None, {"raw_ref": raw_ref, "reason": "invalid_entity_ref"}

    dependency = CatalogDependency(
        raw_ref=raw_ref,
        kind=kind,
        namespace=namespace,
        name=name,
        dependency_type=kind,
    )
    return asdict(dependency), None


def _entity_dependencies(entity: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entity_spec = entity.get("spec") if isinstance(entity.get("spec"), dict) else {}
    depends_on = entity_spec.get("dependsOn")
    if depends_on is None:
        return [], []
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not isinstance(depends_on, list):
        return [], [{"raw_ref": str(depends_on), "reason": "depends_on_must_be_list"}]

    dependencies = []
    malformed = []
    for raw_ref in depends_on:
        dependency, error = _parse_dependency_ref(raw_ref)
        if dependency is not None:
            dependencies.append(dependency)
        if error is not None:
            malformed.append(error)
    return dependencies, malformed


def _service_dependency_summary(services: list[dict[str, Any]]) -> dict[str, Any]:
    dependencies = [
        dependency
        for service in services
        for dependency in service.get("dependencies", [])
        if isinstance(dependency, dict)
    ]
    malformed = [
        malformed_dependency
        for service in services
        for malformed_dependency in service.get("analysis", {}).get("malformed_dependencies", [])
        if isinstance(malformed_dependency, dict)
    ]
    kinds = sorted({str(dependency.get("kind")) for dependency in dependencies if dependency.get("kind")})
    summary = {
        "dependency_count": len(dependencies),
        "unresolved_dependencies": sorted(
            {
                str(dependency["raw_ref"])
                for dependency in dependencies
                if dependency.get("resolution_status") == "unresolved" and dependency.get("raw_ref")
            }
        ),
        "malformed_dependencies": malformed,
    }
    for kind in kinds:
        summary[f"{kind}_dependency_count"] = sum(
            1 for dependency in dependencies if dependency.get("kind") == kind
        )
    return summary


def _entity_to_desired_service(
    entity: dict[str, Any],
    spec: RepositorySpec,
    catalog_file: FetchedFile,
) -> dict[str, Any] | None:
    kind = str(entity.get("kind") or "")
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    entity_spec = entity.get("spec") if isinstance(entity.get("spec"), dict) else {}
    component_type = str(entity_spec.get("type") or "").lower()
    if kind.lower() != "component" or component_type not in {"service", "website", "worker"}:
        return None

    raw_name = str(metadata.get("name") or spec.service_hint or "")
    if not raw_name:
        return None
    name = _slugify(raw_name)
    display_name = str(metadata.get("title") or raw_name)
    owner = spec.owner or entity_spec.get("owner")
    description = metadata.get("description")
    notes = description if isinstance(description, str) and description else "Generated from Backstage catalog metadata."
    dependencies, malformed_dependencies = _entity_dependencies(entity)
    analysis_reasons = ["backstage_component_catalog_found"]
    if dependencies:
        analysis_reasons.append("backstage_dependencies_found")
    if malformed_dependencies:
        analysis_reasons.append("backstage_dependency_refs_malformed")
    analysis = {
        "status": "catalog_derived",
        "confidence": "medium",
        "reasons": analysis_reasons,
    }
    if malformed_dependencies:
        analysis["malformed_dependencies"] = malformed_dependencies

    service = {
        "name": name,
        "display_name": display_name,
        "role": component_type,
        "required": True,
        "min_instances": 1,
        "max_instances": 1,
        "prefers_gpu": False,
        "protocol": "http",
        "source_repository": {
            "url": spec.url,
            "ref": catalog_file.ref,
            "catalog_path": catalog_file.path,
        },
        "dependencies": dependencies,
        "catalog": {
            "kind": kind,
            "metadata_name": metadata.get("name"),
            "spec_type": entity_spec.get("type"),
            "lifecycle": entity_spec.get("lifecycle"),
            "owner": owner,
            "system": entity_spec.get("system"),
        },
        "analysis": analysis,
        "notes": notes,
    }
    return _plain_value(service)


def _repository_name(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/").removesuffix(".git")
    return path.split("/")[-1] if path else parsed.netloc


class GenerateDesiredServices(Job):
    """Generate desired service candidates from repository catalog files."""

    repository_file = StringVar(
        default="seed/service_repositories.yaml",
        description="Path to repository catalog YAML, relative to the repository root when not absolute.",
    )
    output_file = StringVar(
        default="seed/desired_services.generated.yaml",
        description="Path for generated desired services YAML, relative to the repository root when not absolute.",
    )
    dry_run = BooleanVar(default=True, description="Log generated data without writing output_file.")
    fetch_timeout = IntegerVar(default=10, description="HTTP timeout in seconds for each lightweight file request.")

    class Meta:
        name = "Generate Desired Services"
        description = "Fetch catalog-info.yaml and basic files from service repositories without full clone."
        has_sensitive_variables = False

    def run(self, repository_file: str, output_file: str, dry_run: bool, fetch_timeout: int) -> None:
        repository_path = Path(repository_file)
        if not repository_path.is_absolute():
            repository_path = _repo_root() / repository_path
        output_path = Path(output_file)
        if not output_path.is_absolute():
            output_path = _repo_root() / output_path

        specs = _load_repository_specs(repository_path)
        fetcher = RepositoryFileFetcher(timeout=float(fetch_timeout))

        generated_at = datetime.now(timezone.utc).isoformat()
        analyses = []
        desired_services = []

        for spec in specs:
            analysis, services = self.analyze_repository(fetcher, spec)
            analyses.append(analysis)
            desired_services.extend(services)

        result = {
            "generated_at": generated_at,
            "source": {
                "repository_file": str(repository_path),
                "mode": "lightweight_file_fetch",
            },
            "repository_analysis": analyses,
            "desired_services": desired_services,
        }

        self.logger.info(
            "Generated desired service candidates: repositories=%s services=%s",
            len(analyses),
            len(desired_services),
        )
        self.logger.info("Repository analysis: %s", json.dumps(analyses, sort_keys=True, ensure_ascii=True))

        if dry_run:
            self.logger.warning("Dry run complete; not writing %s.", output_path)
            self.logger.info(
                "Generated desired services preview: %s",
                json.dumps(desired_services, sort_keys=True, ensure_ascii=True),
            )
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml.safe_dump(result, sort_keys=False, allow_unicode=False), encoding="utf-8")
        self.logger.info("Wrote generated desired services to %s.", output_path)

    def analyze_repository(
        self,
        fetcher: RepositoryFileFetcher,
        spec: RepositorySpec,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        repo_name = _repository_name(spec.url)
        if not spec.enabled:
            return (
                {
                    "repository": repo_name,
                    "url": spec.url,
                    "enabled": False,
                    "status": "skipped",
                    "reasons": ["repository_disabled"],
                    "checked_files": [],
                },
                [],
            )

        default_branch = fetcher.default_branch(spec)
        refs = _candidate_refs(spec, default_branch)
        catalog_file = fetcher.fetch_first(spec, spec.catalog_paths, refs)
        basic_files = fetcher.fetch_many(spec, spec.basic_file_paths, refs)
        checked_files = sorted({*spec.catalog_paths, *spec.basic_file_paths})

        if catalog_file is None:
            return (
                {
                    "repository": repo_name,
                    "url": spec.url,
                    "enabled": True,
                    "status": "insufficient",
                    "reasons": ["catalog_info_missing"],
                    "default_branch": default_branch,
                    "refs_tried": refs,
                    "checked_files": checked_files,
                    "fetched_basic_files": [file.path for file in basic_files],
                    "next_action": "manual_review_or_deeper_scan",
                },
                [],
            )

        entities = _catalog_entities(catalog_file)
        services = [
            service
            for entity in entities
            if (service := _entity_to_desired_service(entity, spec, catalog_file)) is not None
        ]
        dependency_summary = _service_dependency_summary(services)
        status = "catalog_parsed" if services else "insufficient"
        reasons = ["desired_services_generated"] if services else ["catalog_info_found_but_no_service_component"]
        return (
            {
                "repository": repo_name,
                "url": spec.url,
                "enabled": True,
                "status": status,
                "reasons": reasons,
                "default_branch": default_branch,
                "ref": catalog_file.ref,
                "catalog_path": catalog_file.path,
                "checked_files": checked_files,
                "fetched_basic_files": [file.path for file in basic_files],
                "catalog_entity_count": len(entities),
                "generated_service_count": len(services),
                **dependency_summary,
            },
            services,
        )
