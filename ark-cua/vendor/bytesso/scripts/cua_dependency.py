"""Resolve the standalone al-credential-sync runtime from its official GitHub repo."""

import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from cua_util import SkillError

PROTOCOL = "cua-target/v1"
OFFICIAL_REPOSITORY = "https://github.com/2B-AL/credential-skill"
OFFICIAL_ARCHIVE_BASE = "https://codeload.github.com/2B-AL/credential-skill/zip"
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED_ARCHIVE = 64 * 1024 * 1024
RUNTIME_ROOT = (
    Path(
        os.environ.get("ARK_CUA_BYTESSO_RUNTIME_ROOT")
        or Path.home() / ".ark-agentplan" / "ark-cua" / "runtime" / "bytesso"
    )
    / "dependencies"
    / "al-credential-sync"
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _safe_runtime(path):
    try:
        root_info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        return False
    required = [
        path / "SKILL.md",
        path / "scripts" / "bootstrap-agent.py",
        path / "scripts" / "prepare-source.py",
        path / "scripts" / "sync-cua.py",
        path / "scripts" / "sync-cua-resource.py",
    ]
    for item in required:
        try:
            info = item.lstat()
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
            return False
    try:
        contract = (path / "references" / "cua-target-adapter-v1.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return PROTOCOL in contract


def discover(config):
    candidates = []
    explicit = os.environ.get("CUA_CREDENTIAL_SKILL_DIR") or config.get("credential_skill_dir") or config.get("dir")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([
        RUNTIME_ROOT / "current",
        Path.home() / ".codex" / "skills" / "al-credential-sync",
        Path.home() / ".ark-agentplan" / "skills" / "al-credential-sync",
    ])
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if _safe_runtime(resolved):
            return resolved
    return None


def _source_config(config):
    repository = (
        os.environ.get("CUA_CREDENTIAL_SKILL_REPOSITORY")
        or config.get("credential_skill_repository")
        or config.get("repository")
        or OFFICIAL_REPOSITORY
    )
    normalized = str(repository).strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    if normalized != OFFICIAL_REPOSITORY:
        raise SkillError(
            "DEPENDENCY_INVALID",
            "Credential Skill dependency must use the official 2B-AL/credential-skill repository.",
        )
    commit = str(
        os.environ.get("CUA_CREDENTIAL_SKILL_COMMIT")
        or config.get("credential_skill_commit")
        or config.get("commit")
        or ""
    ).strip().lower()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise SkillError(
            "DEPENDENCY_INVALID",
            "Credential Skill dependency requires a pinned full Git commit.",
        )
    return {
        "repository": OFFICIAL_REPOSITORY,
        "commit": commit,
        "archive_url": f"{OFFICIAL_ARCHIVE_BASE}/{commit}",
    }


def status(config):
    path = discover(config)
    source = None
    try:
        source = _source_config(config)
    except SkillError:
        if path is None:
            raise
    return {
        "installed": path is not None,
        "compatible": path is not None,
        "adapter_protocol": PROTOCOL,
        "runtime_path": str(path) if path else None,
        "source_repository": source.get("repository") if source else None,
        "source_commit": source.get("commit") if source else None,
    }


def configuration(config):
    source = _source_config(config)
    return {
        "adapter_protocol": PROTOCOL,
        "repository": source["repository"],
        "commit": source["commit"],
    }


def _download_github_archive(url):
    parsed = urllib.parse.urlsplit(str(url))
    if (
        parsed.scheme != "https"
        or parsed.hostname != "codeload.github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SkillError("DEPENDENCY_INVALID", "Credential Skill source URL is not the official GitHub archive endpoint.")
    request = urllib.request.Request(url, headers={"User-Agent": "cua-skill-bytesso/credential-dependency-v2"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "codeload.github.com":
                raise SkillError("DEPENDENCY_INVALID", "Credential Skill source redirected outside GitHub codeload.")
            raw = response.read(MAX_ARCHIVE + 1)
    except SkillError:
        raise
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SkillError("DEPENDENCY_UNAVAILABLE", "Official Credential Skill repository download failed.") from exc
    if len(raw) > MAX_ARCHIVE:
        raise SkillError("DEPENDENCY_INVALID", "Credential Skill repository archive exceeds its size limit.")
    return raw


def _safe_extract(raw, destination):
    archive_path = destination.parent / (destination.name + ".zip")
    archive_path.write_bytes(raw)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 512:
                raise SkillError("DEPENDENCY_INVALID", "Credential Skill repository archive has an invalid file count.")
            expanded_size = sum(info.file_size for info in infos)
            if expanded_size > MAX_EXPANDED_ARCHIVE:
                raise SkillError("DEPENDENCY_INVALID", "Credential Skill repository archive expands beyond its size limit.")
            for info in infos:
                path = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (
                    info.flag_bits & 0x1
                    or path.is_absolute()
                    or "\\" in info.filename
                    or not path.parts
                    or any(part in ("", ".", "..") or ":" in part for part in path.parts)
                ):
                    raise SkillError("DEPENDENCY_INVALID", "Credential Skill repository archive contains an unsafe path.")
                if stat.S_ISLNK(mode):
                    raise SkillError("DEPENDENCY_INVALID", "Credential Skill repository archive contains a symlink.")
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.chmod(0o700)
                else:
                    with archive.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o700 if target.suffix in (".py", ".sh") else 0o600)
    except zipfile.BadZipFile as exc:
        raise SkillError("DEPENDENCY_INVALID", "Credential Skill repository archive is not a valid ZIP file.") from exc
    finally:
        archive_path.unlink(missing_ok=True)


def ensure(config):
    existing = discover(config)
    if existing:
        return existing
    source = _source_config(config)
    commit = source["commit"]
    target = RUNTIME_ROOT / commit
    if target.exists() or target.is_symlink():
        if not _safe_runtime(target):
            raise SkillError("DEPENDENCY_INVALID", "Cached Credential Skill runtime is incomplete or unsafe.")
    else:
        archive = _download_github_archive(source["archive_url"])
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        RUNTIME_ROOT.chmod(0o700)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=RUNTIME_ROOT) as tmp:
            stage = Path(tmp) / "archive"
            stage.mkdir(mode=0o700)
            _safe_extract(archive, stage)
            children = list(stage.iterdir())
            expected_name = f"credential-skill-{commit}"
            if len(children) != 1 or children[0].name != expected_name or not _safe_runtime(children[0]):
                raise SkillError(
                    "DEPENDENCY_INVALID",
                    "Official Credential Skill repository archive does not match the pinned commit contract.",
                )
            root = children[0]
            metadata = root / ".cua-dependency.json"
            metadata.write_text(json.dumps({
                "schema_version": 1,
                "repository": source["repository"],
                "commit": commit,
                "adapter_protocol": PROTOCOL,
            }, sort_keys=True) + "\n", encoding="utf-8")
            metadata.chmod(0o600)
            os.replace(root, target)
    current = RUNTIME_ROOT / "current"
    if current.exists() and not current.is_symlink():
        raise SkillError("DEPENDENCY_INVALID", "Credential Skill runtime current path is not a managed symlink.")
    link = RUNTIME_ROOT / (".current-" + os.urandom(6).hex())
    link.symlink_to(target.name)
    os.replace(link, current)
    return target
