"""Minimal .env loader (stdlib only). Values never get printed."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require(name: str) -> str:
    load_env()
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"Missing {name}. Add it to {ROOT / '.env'} (see .env.example).")
    return value


def optional(name: str) -> str:
    load_env()
    return os.environ.get(name, "")


def set_env(name: str, value: str, path: Path = ROOT / ".env") -> None:
    """Write or replace NAME=value in .env (used for tokens obtained by connect scripts)."""
    lines = path.read_text().splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name and not line.lstrip().startswith("#"):
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    os.environ[name] = value
