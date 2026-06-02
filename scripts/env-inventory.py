#!/usr/bin/env python3
"""Inventory env var consumers across all NoorinALabs repos.

Walks the 7 sibling repos under the parent `noorinalabs-main/` tree and
emits a deterministic CSV listing every env-var reference: Pydantic
BaseSettings fields, `os.environ`/`os.getenv` calls, `process.env.*` /
`import.meta.env.*` references, docker-compose `environment`/`env_file`
blocks, GitHub Actions `secrets.*` / `vars.*` references, `.env.example`
declarations, and CLAUDE.md prose references.

Only env-var NAMES are emitted — never values — per deploy#116 scope.

Outputs (relative to this repo root):
  - docs/env-inventory.csv
  - docs/env-inventory.md  (rendered from the CSV, grouped by var_name)

Re-running on the same repo tree must produce byte-identical output.
"""

from __future__ import annotations

import ast
import csv
import re
import sys
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

# 7 child repos under the org tree
REPOS = [
    "noorinalabs-data-acquisition",
    "noorinalabs-deploy",
    "noorinalabs-design-system",
    "noorinalabs-isnad-graph",
    "noorinalabs-isnad-ingest-platform",
    "noorinalabs-landing-page",
    "noorinalabs-user-service",
]

# Skip these path fragments anywhere in the relative path
SKIP_FRAGMENTS = (
    "/node_modules/",
    "/.venv/",
    "/venv/",
    "/.git/",
    "/.claude/worktrees/",
    "/dist/",
    "/build/",
    "/.next/",
    "/__pycache__/",
    "/.pytest_cache/",
    "/.mypy_cache/",
    "/.ruff_cache/",
    "/coverage/",
    "/htmlcov/",
)

# var_type values (kept in a tuple so they're enumerable and stable)
TYPE_PYDANTIC = "pydantic-setting"
TYPE_OS_ENVIRON = "os-environ"
TYPE_DOCKER_ENV = "docker-env"
TYPE_DOCKER_ENV_FILE = "docker-env-file"
TYPE_GHA_SECRET = "gha-secret"
TYPE_GHA_VAR = "gha-var"
TYPE_DOTENV_EXAMPLE = "dotenv-example"
TYPE_CLAUDE_MD_REF = "claude-md-ref"
TYPE_PROCESS_ENV = "process-env"
TYPE_IMPORT_META_ENV = "import-meta-env"


@dataclass(frozen=True, order=True)
class Row:
    var_name: str
    consumer_repo: str
    consumer_service: str
    source_path: str
    source_line: int
    var_type: str


@dataclass
class Result:
    rows: list[Row] = field(default_factory=list)


# ---------- Path / repo helpers ----------


def org_root(deploy_repo_root: Path) -> Path:
    """Given the deploy repo root, return the parent `noorinalabs-main/` dir."""
    return deploy_repo_root.parent


def is_skipped(path: Path) -> bool:
    s = "/" + str(path).replace("\\", "/")
    return any(frag in s for frag in SKIP_FRAGMENTS)


def iter_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        for p in root.rglob(pat):
            if not p.is_file() or is_skipped(p):
                continue
            out.append(p)
    return sorted(set(out))


def repo_of(path: Path, org_dir: Path) -> str:
    try:
        rel = path.relative_to(org_dir)
    except ValueError:
        return ""
    parts = rel.parts
    return parts[0] if parts else ""


def service_of(path: Path, org_dir: Path) -> str:
    """Best-effort service classification within a repo.

    Returns the most specific subdir name that looks service-like
    (frontend/backend/src), else empty string.
    """
    try:
        rel = path.relative_to(org_dir)
    except ValueError:
        return ""
    parts = rel.parts[1:]  # drop repo
    if not parts:
        return ""
    # Common service-ish top-level dirs
    known = {
        "frontend",
        "backend",
        "src",
        "compose",
        "infra",
        "scripts",
        "integration-tests",
        ".github",
        "terraform",
    }
    head = parts[0]
    if head in known:
        return head
    return ""


def rel_from_org(path: Path, org_dir: Path) -> str:
    return path.relative_to(org_dir).as_posix()


# ---------- Scanners ----------

# Pydantic BaseSettings field detection.
# Matches subclasses of BaseSettings (or pydantic_settings.BaseSettings).
_PYDANTIC_BASES = {"BaseSettings", "Settings"}


def scan_python_file(path: Path, org_dir: Path) -> list[Row]:
    """Scan a Python file for Pydantic BaseSettings fields and os.environ/getenv calls."""
    rows: list[Row] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return rows
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return rows

    repo = repo_of(path, org_dir)
    svc = service_of(path, org_dir)
    src = rel_from_org(path, org_dir)

    # 1) BaseSettings field annotations (class body AnnAssign with simple name target)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        base_names: set[str] = set()
        for b in node.bases:
            if isinstance(b, ast.Name):
                base_names.add(b.id)
            elif isinstance(b, ast.Attribute):
                base_names.add(b.attr)
        if not (base_names & _PYDANTIC_BASES):
            continue
        # Skip the BaseSettings class definition itself if it appears in the codebase
        if node.name == "BaseSettings":
            continue
        for child in node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                name = child.target.id
                if name.isupper() or _looks_like_env_var(name):
                    rows.append(
                        Row(
                            var_name=name,
                            consumer_repo=repo,
                            consumer_service=svc,
                            source_path=src,
                            source_line=child.lineno,
                            var_type=TYPE_PYDANTIC,
                        )
                    )

    # 2) os.environ / os.getenv / environ.get calls
    for node in ast.walk(tree):
        # os.environ["FOO"], os.environ.get("FOO"), environ["FOO"]
        if isinstance(node, ast.Subscript):
            val = node.value
            attr_name = None
            if isinstance(val, ast.Attribute):
                attr_name = val.attr
            elif isinstance(val, ast.Name):
                attr_name = val.id
            if attr_name == "environ":
                key = _const_str_from_index(node.slice)
                if key:
                    rows.append(
                        Row(
                            var_name=key,
                            consumer_repo=repo,
                            consumer_service=svc,
                            source_path=src,
                            source_line=node.lineno,
                            var_type=TYPE_OS_ENVIRON,
                        )
                    )
        if isinstance(node, ast.Call):
            f = node.func
            target = None
            if isinstance(f, ast.Attribute):
                if f.attr in ("getenv", "get_env"):
                    target = "getenv"
                elif f.attr == "get" and isinstance(f.value, (ast.Attribute, ast.Name)):
                    base = f.value.attr if isinstance(f.value, ast.Attribute) else f.value.id
                    if base == "environ":
                        target = "environ.get"
            if target and node.args:
                key = _const_str(node.args[0])
                if key:
                    rows.append(
                        Row(
                            var_name=key,
                            consumer_repo=repo,
                            consumer_service=svc,
                            source_path=src,
                            source_line=node.lineno,
                            var_type=TYPE_OS_ENVIRON,
                        )
                    )
    return rows


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_str_from_index(slice_node: ast.AST) -> str | None:
    # py3.9+: subscript.slice is the index expr directly (not ast.Index)
    return _const_str(slice_node)


_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _looks_like_env_var(name: str) -> bool:
    return bool(_ENV_NAME_RE.match(name))


# ---------- TS/JS scanner (regex) ----------

# process.env.FOO  or process.env["FOO"] or process.env['FOO']
_PROCESS_ENV_RE = re.compile(
    r"process\.env\.([A-Z][A-Z0-9_]*)"
    r"|process\.env\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\]"
)
# import.meta.env.FOO  or import.meta.env["FOO"]
_IMPORT_META_RE = re.compile(
    r"import\.meta\.env\.([A-Z][A-Z0-9_]*)"
    r"|import\.meta\.env\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\]"
)


def scan_ts_js_file(path: Path, org_dir: Path) -> list[Row]:
    rows: list[Row] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return rows
    repo = repo_of(path, org_dir)
    svc = service_of(path, org_dir)
    src = rel_from_org(path, org_dir)

    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _PROCESS_ENV_RE.finditer(line):
            name = m.group(1) or m.group(2)
            if name:
                rows.append(Row(name, repo, svc, src, lineno, TYPE_PROCESS_ENV))
        for m in _IMPORT_META_RE.finditer(line):
            name = m.group(1) or m.group(2)
            if name:
                rows.append(Row(name, repo, svc, src, lineno, TYPE_IMPORT_META_ENV))
    return rows


# ---------- dotenv (.env.example) ----------

_DOTENV_LINE_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")


def scan_dotenv_example(path: Path, org_dir: Path) -> list[Row]:
    rows: list[Row] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return rows
    repo = repo_of(path, org_dir)
    svc = service_of(path, org_dir)
    src = rel_from_org(path, org_dir)
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped:
            continue
        m = _DOTENV_LINE_RE.match(line)
        if m:
            rows.append(Row(m.group(1), repo, svc, src, lineno, TYPE_DOTENV_EXAMPLE))
    return rows


# ---------- docker-compose ----------

# Match lines inside `environment:` blocks. We don't load YAML to avoid a
# runtime dep; the structure is simple enough for a line scanner.

_COMPOSE_ENV_KV_RE = re.compile(r"^\s*-\s*([A-Z][A-Z0-9_]*)\s*[:=]")
_COMPOSE_ENV_MAP_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*:")
_COMPOSE_ENV_FILE_LINE_RE = re.compile(r"^\s*-\s+([^\s#]*\.env[\w.\-]*)\s*$")
_COMPOSE_INTERPOLATION_RE = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)")


def scan_compose_file(path: Path, org_dir: Path) -> list[Row]:
    rows: list[Row] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return rows
    repo = repo_of(path, org_dir)
    svc = service_of(path, org_dir)
    src = rel_from_org(path, org_dir)

    lines = text.splitlines()
    in_env_block = False
    env_block_indent = -1
    in_env_file_block = False
    env_file_indent = -1
    env_block_style: str | None = None  # 'list' or 'map'

    def indent_of(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # Don't exit blocks on blank/comment lines (compose allows them)
            continue
        cur_indent = indent_of(line)

        # Exit env block when indent drops back to or below block header indent
        if in_env_block and cur_indent <= env_block_indent:
            in_env_block = False
            env_block_indent = -1
            env_block_style = None
        if in_env_file_block and cur_indent <= env_file_indent:
            in_env_file_block = False
            env_file_indent = -1

        # Detect block headers
        if re.match(r"^\s*environment\s*:\s*$", line):
            in_env_block = True
            env_block_indent = cur_indent
            env_block_style = None
            continue
        if re.match(r"^\s*env_file\s*:\s*$", line):
            in_env_file_block = True
            env_file_indent = cur_indent
            continue
        # Inline `env_file: path` (single-value form)
        m_inline_envfile = re.match(r"^\s*env_file\s*:\s*([^\s#]+)\s*$", line)
        if m_inline_envfile:
            rows.append(
                Row(
                    var_name=m_inline_envfile.group(1).strip("'\""),
                    consumer_repo=repo,
                    consumer_service=svc,
                    source_path=src,
                    source_line=lineno,
                    var_type=TYPE_DOCKER_ENV_FILE,
                )
            )
            continue

        if in_env_block:
            m = _COMPOSE_ENV_KV_RE.match(line)
            if m:
                env_block_style = env_block_style or "list"
                rows.append(Row(m.group(1), repo, svc, src, lineno, TYPE_DOCKER_ENV))
                # Also catch ${FOO} interpolations in the same line
                for im in _COMPOSE_INTERPOLATION_RE.finditer(line):
                    rows.append(Row(im.group(1), repo, svc, src, lineno, TYPE_DOCKER_ENV))
                continue
            m2 = _COMPOSE_ENV_MAP_RE.match(line)
            if m2:
                env_block_style = env_block_style or "map"
                rows.append(Row(m2.group(1), repo, svc, src, lineno, TYPE_DOCKER_ENV))
                for im in _COMPOSE_INTERPOLATION_RE.finditer(line):
                    rows.append(Row(im.group(1), repo, svc, src, lineno, TYPE_DOCKER_ENV))
                continue

        if in_env_file_block:
            m = _COMPOSE_ENV_FILE_LINE_RE.match(line)
            if m:
                rows.append(
                    Row(
                        var_name=m.group(1).strip("'\""),
                        consumer_repo=repo,
                        consumer_service=svc,
                        source_path=src,
                        source_line=lineno,
                        var_type=TYPE_DOCKER_ENV_FILE,
                    )
                )
    return rows


# ---------- GitHub Actions workflows ----------

_GHA_SECRET_RE = re.compile(r"\$\{\{\s*secrets\.([A-Z][A-Z0-9_]*)\s*\}\}")
_GHA_VAR_RE = re.compile(r"\$\{\{\s*vars\.([A-Z][A-Z0-9_]*)\s*\}\}")


def scan_gha_file(path: Path, org_dir: Path) -> list[Row]:
    rows: list[Row] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return rows
    repo = repo_of(path, org_dir)
    src = rel_from_org(path, org_dir)
    svc = ".github"
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _GHA_SECRET_RE.finditer(line):
            rows.append(Row(m.group(1), repo, svc, src, lineno, TYPE_GHA_SECRET))
        for m in _GHA_VAR_RE.finditer(line):
            rows.append(Row(m.group(1), repo, svc, src, lineno, TYPE_GHA_VAR))
    return rows


# ---------- CLAUDE.md prose refs ----------

# Match backtick-wrapped UPPER_SNAKE tokens that look like env vars.
# Trailing underscore is rejected (e.g. `AUTH_` is a prefix mention, not a var).
_CLAUDE_TOKEN_RE = re.compile(r"`([A-Z][A-Z0-9_]*[A-Z0-9])`")

# Tokens we explicitly ignore — they're not env vars even though they match the
# UPPER_SNAKE shape (file names, command flags, code references, model IDs).
_CLAUDE_DENYLIST = frozenset(
    {
        "TODO",
        "FIXME",
        "NOTE",
        "BUG",
        "XXX",
        "README",
        "LICENSE",
        "CHANGELOG",
        "CLAUDE",
        "TRUE",
        "FALSE",
        "NULL",
        "NONE",
        "HTTP",
        "HTTPS",
        "REST",
        "JSON",
        "YAML",
        "CSV",
        "MD",
        "TXT",
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "HEAD",
        "OPTIONS",
        "API",
        "CLI",
        "URL",
        "URI",
        "ID",
        "UUID",
        "DSN",
        "SQL",
        "DDL",
        "DML",
        "ORM",
        "CPU",
        "RAM",
        "GPU",
        "RPM",
        "TPM",
        "QPS",
        "MVP",
        "PRD",
        "ADR",
        "SOP",
        "RFC",
        "ETL",
        "CDC",
        "OAUTH",
        "SAML",
        "JWT",
        "RSA",
        "ECDSA",
        "TLS",
        "SSL",
        "MTLS",
    }
)


def scan_claude_md(path: Path, org_dir: Path) -> list[Row]:
    """Scan a CLAUDE.md file for backtick-wrapped env-var-shaped tokens.

    Heuristic — false positives expected; we tag them `claude-md-ref` so
    downstream consumers know to validate against the other sources.
    """
    rows: list[Row] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return rows
    repo = repo_of(path, org_dir)
    src = rel_from_org(path, org_dir)
    svc = ""
    # Only emit a (var, file) tuple once per CLAUDE.md to avoid noise; record
    # the first matching line.
    seen: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _CLAUDE_TOKEN_RE.finditer(line):
            tok = m.group(1)
            if tok in _CLAUDE_DENYLIST:
                continue
            if "_" not in tok:
                # Single-word ALL-CAPS tokens are mostly acronyms in prose.
                continue
            if tok not in seen:
                seen[tok] = lineno
    for tok, lineno in seen.items():
        rows.append(Row(tok, repo, svc, src, lineno, TYPE_CLAUDE_MD_REF))
    return rows


# ---------- Driver ----------


def collect_all(org_dir: Path) -> list[Row]:
    rows: list[Row] = []
    for repo in REPOS:
        repo_root = org_dir / repo
        if not repo_root.is_dir():
            continue

        # Python — only scan .py files inside the repo
        for p in iter_files(repo_root, ("*.py",)):
            rows.extend(scan_python_file(p, org_dir))

        # TS/JS — common source extensions; skip node_modules / dist via SKIP_FRAGMENTS
        for p in iter_files(repo_root, ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs")):
            rows.extend(scan_ts_js_file(p, org_dir))

        # .env.example (top-level + any subdir)
        for p in iter_files(repo_root, (".env.example",)):
            rows.extend(scan_dotenv_example(p, org_dir))

        # docker-compose files
        for p in iter_files(repo_root, ("docker-compose*.yml", "docker-compose*.yaml")):
            rows.extend(scan_compose_file(p, org_dir))

        # GitHub Actions workflows (only .github/workflows/, not nested under
        # node_modules — SKIP_FRAGMENTS handles that)
        for p in iter_files(repo_root, ("*.yml", "*.yaml")):
            rel = p.relative_to(repo_root).as_posix()
            if rel.startswith(".github/workflows/"):
                rows.extend(scan_gha_file(p, org_dir))

        # CLAUDE.md references (root + any nested)
        for p in iter_files(repo_root, ("CLAUDE.md",)):
            rows.extend(scan_claude_md(p, org_dir))

    # Deterministic ordering — Row is order=True so this sorts on all fields
    rows.sort()
    # Drop exact duplicates that can arise from compose interpolation overlap
    deduped: list[Row] = []
    last: Row | None = None
    for r in rows:
        if r != last:
            deduped.append(r)
            last = r
    return deduped


_CSV_HEADER = [
    "var_name",
    "consumer_repo",
    "consumer_service",
    "source_path",
    "source_line",
    "var_type",
]


def render_csv(rows: list[Row]) -> str:
    """Render rows to CSV text (shared by write_csv and the --check renderer)."""
    buf = StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    for r in rows:
        writer.writerow(
            [
                r.var_name,
                r.consumer_repo,
                r.consumer_service,
                r.source_path,
                r.source_line,
                r.var_type,
            ]
        )
    return buf.getvalue()


def write_csv(rows: list[Row], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_csv(rows), encoding="utf-8")


def write_markdown(rows: list[Row], out_path: Path) -> None:
    """Render the inventory as a markdown table grouped by var_name."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(rows), encoding="utf-8")


def render_markdown(rows: list[Row]) -> str:
    """Render the inventory markdown to a string (shared by write + --check)."""
    # Group by var_name
    by_var: dict[str, list[Row]] = {}
    for r in rows:
        by_var.setdefault(r.var_name, []).append(r)

    lines: list[str] = []
    lines.append("# Env-var inventory")
    lines.append("")
    lines.append(
        "> Generated by `scripts/env-inventory.py` — re-run after env changes; "
        "do not edit manually."
    )
    lines.append("")
    lines.append(f"**Distinct env vars:** {len(by_var)}  ")
    lines.append(f"**Total references:** {len(rows)}  ")
    multi_source = sum(
        1 for refs in by_var.values() if len({(r.consumer_repo, r.source_path) for r in refs}) > 1
    )
    lines.append(f"**Vars referenced from >1 source file:** {multi_source}  ")
    lines.append("")
    lines.append("## Per-var detail")
    lines.append("")
    for var in sorted(by_var.keys()):
        refs = by_var[var]
        sources = {(r.consumer_repo, r.source_path) for r in refs}
        flag = ""
        if len(sources) > 1:
            flag = "  :warning: **Multiple sources of truth**"
        lines.append(f"### `{var}`{flag}")
        lines.append("")
        lines.append(f"**Distinct sources of truth:** {len(sources)}")
        lines.append("")
        lines.append("| Repo | Service | Source | Line | Type |")
        lines.append("|---|---|---|---|---|")
        for r in refs:
            svc = r.consumer_service or "—"
            lines.append(
                f"| {r.consumer_repo} | {svc} | `{r.source_path}` | "
                f"{r.source_line} | {r.var_type} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def summary(rows: list[Row]) -> str:
    distinct = sorted({r.var_name for r in rows})
    by_repo: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for r in rows:
        by_repo[r.consumer_repo] = by_repo.get(r.consumer_repo, 0) + 1
        by_type[r.var_type] = by_type.get(r.var_type, 0) + 1
    out: list[str] = []
    out.append(f"Distinct env vars: {len(distinct)}")
    out.append(f"Total references:  {len(rows)}")
    out.append("")
    out.append("Per-repo reference counts:")
    for k in sorted(by_repo):
        out.append(f"  {k:40s} {by_repo[k]:5d}")
    out.append("")
    out.append("Per-type reference counts:")
    for k in sorted(by_type):
        out.append(f"  {k:25s} {by_type[k]:5d}")
    return "\n".join(out)


def _find_deploy_repo_root(script_path: Path) -> Path:
    """Walk up from the script to the deploy repo root.

    The script lives at `<deploy-repo>/scripts/env-inventory.py` but may run
    from a git worktree whose path is `<deploy-repo>/.claude/worktrees/<x>/`.
    Identify the deploy repo root as the nearest ancestor named
    `noorinalabs-deploy`.
    """
    for parent in script_path.parents:
        if parent.name == "noorinalabs-deploy":
            return parent
    # Fallback: the conventional layout has the script directly under repo/scripts
    return script_path.parent.parent


def _find_org_dir(deploy_root: Path) -> Path:
    """Find the `noorinalabs-main/` dir that holds all 7 sibling repos.

    When running from a worktree, `deploy_root.parent` may be the worktrees dir
    rather than the org root. Walk up until we find a dir containing both
    `noorinalabs-deploy` and at least one other expected sibling.
    """
    candidate: Path | None = None
    for parent in deploy_root.parents:
        if (parent / "noorinalabs-deploy").is_dir() and (
            parent / "noorinalabs-isnad-graph"
        ).is_dir():
            candidate = parent
            break
    return candidate or deploy_root.parent


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Don't write; regenerate in memory and exit non-zero if "
            "docs/env-inventory.{csv,md} on disk is stale. Used by the "
            "env-validate staleness gate (deploy#363)."
        ),
    )
    args = parser.parse_args()

    script_path = Path(__file__).resolve()
    # `output_root` is the working tree the script was invoked from — usually a
    # git worktree off the deploy repo. We write outputs here so the diff lands
    # on the PR branch, not on the main checkout.
    output_root = script_path.parent.parent
    # `org_dir` is the org-level dir containing all 7 sibling repos. It's
    # discovered relative to the canonical deploy repo (resolved via git
    # worktree path conventions) so we always scan the same sibling tree
    # regardless of which worktree the script is invoked from.
    canonical_deploy_root = _find_deploy_repo_root(script_path)
    org_dir = _find_org_dir(canonical_deploy_root)
    if not org_dir.is_dir():
        print(f"ERROR: org dir not found: {org_dir}", file=sys.stderr)
        return 2

    rows = collect_all(org_dir)
    csv_out = output_root / "docs" / "env-inventory.csv"
    md_out = output_root / "docs" / "env-inventory.md"

    if args.check:
        # Render to strings and compare against on-disk; never write.
        expected_csv = render_csv(rows)
        expected_md = render_markdown(rows)

        stale: list[str] = []
        on_disk_csv = csv_out.read_text(encoding="utf-8") if csv_out.exists() else ""
        if on_disk_csv != expected_csv:
            stale.append(str(csv_out.relative_to(output_root)))
        on_disk_md = md_out.read_text(encoding="utf-8") if md_out.exists() else ""
        if on_disk_md != expected_md:
            stale.append(str(md_out.relative_to(output_root)))

        if stale:
            print(
                "STALE: " + ", ".join(stale) + " — re-run "
                "`python3 scripts/env-inventory.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("env-inventory: docs/env-inventory.{csv,md} are current.")
        return 0

    write_csv(rows, csv_out)
    write_markdown(rows, md_out)

    print(f"Wrote {csv_out.relative_to(output_root)}")
    print(f"Wrote {md_out.relative_to(output_root)}")
    print()
    print(summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
