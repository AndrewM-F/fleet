"""Fleet root, configuration and file locations. Everything resolves from the fleet root."""
import json
import os
from pathlib import Path
import shutil

ROOT = Path(os.environ.get("FLEET_ROOT") or Path(__file__).resolve().parents[2])
CONFIG = Path(os.environ.get("FLEET_CONFIG") or ROOT / "config" / "fleet.local.json")
DATA = ROOT / "data"
LOGS = ROOT / "logs"
BIN = ROOT / "bin" / "fleet"

DB = DATA / "fleet.sqlite"
QUEUE = DATA / "queue.json"
STATUS = DATA / "status.json"
PAUSE = DATA / "dispatch.paused"
MANAGER_LOCK = DATA / "manager.lock"
QUEUE_LOCK = DATA / "queue.lock"
RECEIPTS = DATA / "receipts"
CLAUDE = DATA / "claude"
OPENCODE = DATA / "opencode"


def config(path=None):
    return json.loads(Path(path or CONFIG).read_text())


def _expand(value):
    return os.path.expanduser(value) if isinstance(value, str) else value


def slots(cfg=None):
    """Return (pi_slots, claude_slots) with paths expanded and provider tagged."""
    cfg = config() if cfg is None else cfg
    pi = []
    for raw in cfg.get("pi", []):
        slot = dict(raw, provider="pi", piProvider=raw.get("provider"))
        for key in ("cwd", "sessionFile", "sessionCwd"):
            if key in slot:
                slot[key] = _expand(slot[key])
        pi.append(slot)
    claude = []
    for raw in cfg.get("claude", []):
        slot = dict(raw, provider="claude")
        for key in ("cwd", "sessionCwd"):
            if key in slot:
                slot[key] = _expand(slot[key])
        claude.append(slot)
    return pi, claude


def opencode_slots(cfg=None):
    """Return configured OpenCode sessions with their native provider tagged."""
    cfg = config() if cfg is None else cfg
    result = []
    for raw in cfg.get("opencode", []):
        slot = dict(raw, provider="opencode")
        for key in ("cwd", "sessionCwd"):
            if key in slot:
                slot[key] = _expand(slot[key])
        result.append(slot)
    return result


def all_slots(cfg=None):
    """Return every configured worker while keeping the legacy provider pair stable."""
    pi, claude = slots(cfg)
    # Legacy unit fixtures often replace the provider pair without creating a config file.
    if cfg is None and not CONFIG.exists():
        return pi + claude
    return pi + claude + opencode_slots(cfg)


def known_cwds(slot):
    """The configured cwd plus the cwd recorded when the native session was created."""
    return {c for c in (slot.get("cwd"), slot.get("sessionCwd")) if c}


def executable(name, cfg=None):
    cfg = config() if cfg is None else cfg
    value = _expand((cfg.get("executables") or {}).get(name) or name)
    found = value if os.path.isabs(value) and os.access(value, os.X_OK) else shutil.which(value)
    if not found:
        raise RuntimeError(f"{name} executable not found (config executables.{name} or PATH)")
    return found


def lane_dir(lane):
    return DATA / "lanes" / lane


def lane_logs(lane):
    return LOGS / "lanes" / lane


def socket_path(lane):
    path = DATA / "run" / (lane + ".sock")
    if len(str(path)) > 100:  # AF_UNIX path limit
        import hashlib
        tag = hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]
        path = Path("/tmp") / f"fleet-{tag}-{lane}.sock"
    return path


def log_limits(cfg=None):
    try:
        limits = (config() if cfg is None else cfg).get("logs") or {}
    except FileNotFoundError:
        limits = {}
    return (int(limits.get("maxBytes", 20 * 1024 * 1024)), int(limits.get("keep", 5)),
            int(limits.get("claudeTurnsPerSession", 20)))


def rotate(path, max_bytes=None, keep=None):
    """Size-bounded rotation; call only before a writer opens the file."""
    default_max, default_keep, _ = log_limits()
    max_bytes = default_max if max_bytes is None else max_bytes
    keep = default_keep if keep is None else keep
    path = Path(path)
    if not path.exists() or path.stat().st_size <= max_bytes:
        return False
    for n in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{n}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{n + 1}"))
    os.replace(path, path.with_name(path.name + ".1"))
    path.with_name(f"{path.name}.{keep + 1}").unlink(missing_ok=True)
    return True
