#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.27.0",
#     "rich>=13.0.0",
# ]
# ///
"""
Batch test the /build endpoint using AFP (Archive of Formal Proofs) sessions.

This script discovers AFP sessions, sends build requests to the API,
and generates success/failure reports.

Usage:
    # Test starter set (10 simple sessions)
    uv run batch_test_build.py --starter

    # Test all Tier 1 sessions (no AFP dependencies)
    uv run batch_test_build.py --tier1

    # Test specific sessions
    uv run batch_test_build.py --sessions Ackermanns_not_PR,AnselmGod

    # Test first N tier1 sessions
    uv run batch_test_build.py --tier1 --limit 20
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

# Default paths
DEFAULT_AFP_PATH = Path(__file__).parent / "lib" / "afp" / "thys"
DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "test_results"

# Standard Isabelle heaps that don't require AFP
STANDARD_HEAPS = {
    "HOL", "HOL-Library", "HOL-Analysis", "HOL-Algebra", "HOL-Computational_Algebra",
    "HOL-Number_Theory", "HOL-Probability", "HOL-Data_Structures", "HOL-Eisbach",
    "HOL-ex", "HOL-Combinatorics", "HOL-Real_Asymp", "HOL-Decision_Procs",
    "HOLCF", "HOLCF-Library", "Pure",
}

# Starter set: 10 simple HOL sessions for smoke testing
STARTER_SESSIONS = [
    "Ackermanns_not_PR",
    "AnselmGod",
    "Abstract_Soundness",
    "AVL-Trees",
    "BinarySearchTree",
    "Boolos_Curious_Inference",
    "Card_Multisets",
    "List_Inversions",
    "Fibonacci_Sums",
    "Derangements",
]

console = Console()


@dataclass
class SessionInfo:
    """Parsed information from an AFP ROOT file."""
    name: str
    path: Path
    parent_session: str
    timeout: int = 300
    theories: list[str] = field(default_factory=list)
    afp_dependencies: list[str] = field(default_factory=list)
    root_content: str = ""


@dataclass
class BuildResult:
    """Result of building a single session."""
    session_name: str
    status: str  # success, failure, timeout, error
    built: bool
    build_time_seconds: float
    error_count: int
    errors: list[dict] = field(default_factory=list)
    message: str = ""


def parse_root_file(root_path: Path) -> SessionInfo | None:
    """
    Parse an AFP ROOT file to extract session metadata.

    ROOT files follow this pattern:
        chapter AFP
        session Name = Parent +
          options [timeout = 300]
          sessions
            Dep1
            Dep2
          theories
            Theory1
            Theory2
          document_files
            "root.tex"

    Some sessions have attributes like:
        session "Abel_Limit_Theorem" (AFP) = Catalan_Numbers +
        session AODV (slow) = AWN +
    """
    try:
        content = root_path.read_text()
    except Exception as e:
        console.print(f"[red]Error reading {root_path}: {e}[/red]")
        return None

    # Extract session name and parent
    # Handle quoted session names like "AVL-Trees" and optional attributes like (AFP)
    session_match = re.search(
        r'session\s+(?:"([^"]+)"|([A-Za-z][A-Za-z0-9_-]*))\s*(?:\([^)]*\))?\s*=\s*(?:"([^"]+)"|([A-Za-z][A-Za-z0-9_-]*))\s*\+',
        content
    )
    if not session_match:
        return None

    session_name = session_match.group(1) or session_match.group(2)
    parent_session = session_match.group(3) or session_match.group(4)

    # Extract timeout
    timeout_match = re.search(r'timeout\s*=\s*(\d+)', content)
    timeout = int(timeout_match.group(1)) if timeout_match else 300

    # Extract theories (can appear multiple times with different attributes)
    theories = []
    # Find all theory blocks
    theory_blocks = re.findall(
        r'theories(?:\s*\[[^\]]*\])?\s+((?:[A-Za-z_][A-Za-z0-9_]*\s*)+)',
        content
    )
    for block in theory_blocks:
        theories.extend(block.split())

    # Extract AFP session dependencies
    afp_deps = []
    sessions_match = re.search(
        r'sessions\s+((?:[A-Za-z_][A-Za-z0-9_-]*\s*)+?)(?=theories|document_files|$)',
        content,
        re.DOTALL
    )
    if sessions_match:
        deps_text = sessions_match.group(1)
        afp_deps = deps_text.split()

    return SessionInfo(
        name=session_name,
        path=root_path.parent,
        parent_session=parent_session,
        timeout=timeout,
        theories=theories,
        afp_dependencies=afp_deps,
        root_content=content,
    )


def discover_sessions(afp_path: Path, verbose: bool = False) -> list[SessionInfo]:
    """Find all AFP sessions by parsing ROOT files."""
    sessions = []

    # Read ROOTS file to get list of entries
    roots_file = afp_path / "ROOTS"
    if roots_file.exists():
        entries = roots_file.read_text().strip().split('\n')
    else:
        # Fallback: scan directories
        entries = [d.name for d in afp_path.iterdir() if d.is_dir()]

    total = len(entries)
    for i, entry in enumerate(entries):
        entry = entry.strip()
        if not entry:
            continue

        entry_path = afp_path / entry
        root_file = entry_path / "ROOT"

        if root_file.exists():
            session = parse_root_file(root_file)
            if session:
                sessions.append(session)

    return sessions


def filter_tier1_sessions(sessions: list[SessionInfo]) -> list[SessionInfo]:
    """
    Filter to Tier 1 sessions: those with no AFP dependencies.

    These sessions depend only on standard Isabelle heaps (HOL, HOL-Library, etc.)
    and can be built in isolation.
    """
    return [s for s in sessions if not s.afp_dependencies]


def load_theory_files(session: SessionInfo) -> dict[str, str]:
    """Load all .thy files for a session."""
    theory_files = {}

    for theory_name in session.theories:
        thy_path = session.path / f"{theory_name}.thy"
        if thy_path.exists():
            try:
                theory_files[f"{theory_name}.thy"] = thy_path.read_text()
            except Exception as e:
                console.print(f"[yellow]Warning: Could not read {thy_path}: {e}[/yellow]")

    # If no theories were specified or found, try to find .thy files directly
    if not theory_files:
        for thy_path in session.path.glob("*.thy"):
            try:
                theory_files[thy_path.name] = thy_path.read_text()
            except Exception as e:
                console.print(f"[yellow]Warning: Could not read {thy_path}: {e}[/yellow]")

    return theory_files


def build_session(
    session: SessionInfo,
    api_url: str,
    timeout_seconds: int | None = None,
) -> BuildResult:
    """Send a build request to the API for a single session."""
    # Load theory files
    theory_files = load_theory_files(session)

    if not theory_files:
        return BuildResult(
            session_name=session.name,
            status="error",
            built=False,
            build_time_seconds=0,
            error_count=1,
            errors=[{"message": "No theory files found"}],
            message="No theory files found for session",
        )

    # Prepare request
    effective_timeout = timeout_seconds or session.timeout
    payload = {
        "session_name": session.name,
        "root_content": session.root_content,
        "theory_files": theory_files,
        "timeout_seconds": effective_timeout,
        "options": ["-v"],
    }

    url = f"{api_url.rstrip('/')}/build"

    try:
        # Add extra time for network overhead
        client_timeout = effective_timeout + 60

        with httpx.Client(timeout=client_timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            result = response.json()

        return BuildResult(
            session_name=session.name,
            status="success" if result["built"] else "failure",
            built=result["built"],
            build_time_seconds=result.get("build_time_seconds", 0),
            error_count=len(result.get("errors", [])),
            errors=result.get("errors", []),
            message=result.get("message", ""),
        )

    except httpx.TimeoutException:
        return BuildResult(
            session_name=session.name,
            status="timeout",
            built=False,
            build_time_seconds=effective_timeout,
            error_count=1,
            errors=[{"message": f"Request timed out after {effective_timeout}s"}],
            message=f"Build timed out after {effective_timeout} seconds",
        )

    except httpx.HTTPStatusError as e:
        return BuildResult(
            session_name=session.name,
            status="error",
            built=False,
            build_time_seconds=0,
            error_count=1,
            errors=[{"message": f"HTTP {e.response.status_code}: {e.response.text[:500]}"}],
            message=f"HTTP error: {e.response.status_code}",
        )

    except Exception as e:
        return BuildResult(
            session_name=session.name,
            status="error",
            built=False,
            build_time_seconds=0,
            error_count=1,
            errors=[{"message": str(e)}],
            message=f"Error: {str(e)}",
        )


def run_batch(
    sessions: list[SessionInfo],
    api_url: str,
    timeout_seconds: int | None = None,
) -> list[BuildResult]:
    """Execute batch builds with progress tracking."""
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Building sessions...", total=len(sessions))

        for session in sessions:
            progress.update(task, description=f"Building {session.name}...")

            result = build_session(session, api_url, timeout_seconds)
            results.append(result)

            # Update with result indicator
            status_icon = "✓" if result.built else "✗"
            progress.update(task, advance=1)
            progress.console.print(
                f"  {status_icon} {session.name}: {result.status} "
                f"({result.build_time_seconds:.1f}s)"
            )

    return results


def generate_json_report(
    results: list[BuildResult],
    output_dir: Path,
    timestamp: str,
) -> Path:
    """Generate JSON report of build results."""
    output_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "timestamp": timestamp,
        "summary": {
            "total": len(results),
            "success": sum(1 for r in results if r.built),
            "failure": sum(1 for r in results if r.status == "failure"),
            "timeout": sum(1 for r in results if r.status == "timeout"),
            "error": sum(1 for r in results if r.status == "error"),
        },
        "results": [asdict(r) for r in results],
    }

    output_path = output_dir / f"results_{timestamp}.json"
    output_path.write_text(json.dumps(report, indent=2))

    return output_path


def generate_markdown_report(
    results: list[BuildResult],
    output_dir: Path,
    timestamp: str,
) -> Path:
    """Generate human-readable Markdown report."""
    output_dir.mkdir(parents=True, exist_ok=True)

    total = len(results)
    success = sum(1 for r in results if r.built)
    failure = sum(1 for r in results if r.status == "failure")
    timeout = sum(1 for r in results if r.status == "timeout")
    error = sum(1 for r in results if r.status == "error")

    lines = [
        f"# AFP Build Test Results",
        f"",
        f"**Date:** {timestamp}",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total | {total} |",
        f"| Success | {success} ({100*success/total:.1f}%) |" if total > 0 else f"| Success | 0 |",
        f"| Failure | {failure} |",
        f"| Timeout | {timeout} |",
        f"| Error | {error} |",
        f"",
        f"## Results",
        f"",
        f"| Session | Status | Time (s) | Errors |",
        f"|---------|--------|----------|--------|",
    ]

    for r in sorted(results, key=lambda x: (not x.built, x.session_name)):
        status_emoji = "✅" if r.built else "❌"
        error_summary = r.errors[0]["message"][:50] + "..." if r.errors else "-"
        lines.append(
            f"| {r.session_name} | {status_emoji} {r.status} | "
            f"{r.build_time_seconds:.1f} | {error_summary} |"
        )

    # Add failure details section
    failed = [r for r in results if not r.built]
    if failed:
        lines.extend([
            f"",
            f"## Failure Details",
            f"",
        ])
        for r in failed:
            lines.extend([
                f"### {r.session_name}",
                f"",
                f"**Status:** {r.status}",
                f"**Message:** {r.message}",
                f"",
                f"**Errors:**",
            ])
            for err in r.errors[:5]:  # Limit to 5 errors per session
                theory = err.get("theory", "")
                line = err.get("line", "")
                msg = err.get("message", "")
                location = f"{theory}:{line}" if theory and line else (theory or "")
                lines.append(f"- {location}: {msg}" if location else f"- {msg}")
            if len(r.errors) > 5:
                lines.append(f"- ... and {len(r.errors) - 5} more errors")
            lines.append("")

    output_path = output_dir / f"summary_{timestamp}.md"
    output_path.write_text("\n".join(lines))

    return output_path


def print_summary_table(results: list[BuildResult]):
    """Print a summary table to the console."""
    table = Table(title="Build Results Summary")

    table.add_column("Session", style="cyan")
    table.add_column("Status", justify="center")
    table.add_column("Time (s)", justify="right")
    table.add_column("Errors", justify="right")

    for r in sorted(results, key=lambda x: (not x.built, x.session_name)):
        status_style = "green" if r.built else "red"
        table.add_row(
            r.session_name,
            f"[{status_style}]{r.status}[/{status_style}]",
            f"{r.build_time_seconds:.1f}",
            str(r.error_count),
        )

    console.print(table)

    # Print summary stats
    total = len(results)
    success = sum(1 for r in results if r.built)
    console.print(f"\n[bold]Total:[/bold] {total} sessions")
    console.print(f"[bold green]Success:[/bold green] {success} ({100*success/total:.1f}%)" if total > 0 else "[bold]Success:[/bold] 0")
    console.print(f"[bold red]Failed:[/bold red] {total - success}")


def check_health(api_url: str) -> bool:
    """Check if the API is healthy."""
    url = f"{api_url.rstrip('/')}/health"
    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(url)
            response.raise_for_status()
            return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Batch test the /build endpoint using AFP sessions."
    )
    parser.add_argument(
        "--starter",
        action="store_true",
        help="Test the starter set of 10 simple sessions",
    )
    parser.add_argument(
        "--tier1",
        action="store_true",
        help="Test all Tier 1 sessions (no AFP dependencies)",
    )
    parser.add_argument(
        "--sessions",
        type=str,
        help="Comma-separated list of specific session names to test",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit the number of sessions to test",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"API URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--afp-path",
        type=Path,
        default=DEFAULT_AFP_PATH,
        help=f"Path to AFP thys directory (default: {DEFAULT_AFP_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for reports (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Override timeout for all sessions (seconds)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list sessions that would be tested, don't run builds",
    )

    args = parser.parse_args()

    # Check API health first
    if not args.list_only:
        console.print(f"Checking API health at {args.api_url}...")
        if not check_health(args.api_url):
            console.print(
                f"[red]Error: Could not connect to API at {args.api_url}[/red]"
            )
            console.print("Make sure the Docker container is running: docker-compose up")
            sys.exit(1)
        console.print("[green]API is healthy[/green]\n")

    # Discover sessions
    console.print(f"Discovering AFP sessions in {args.afp_path}...")
    all_sessions = discover_sessions(args.afp_path)
    console.print(f"Found {len(all_sessions)} sessions\n")

    # Filter sessions based on arguments
    if args.starter:
        sessions = [s for s in all_sessions if s.name in STARTER_SESSIONS]
        # Sort by starter order
        session_order = {name: i for i, name in enumerate(STARTER_SESSIONS)}
        sessions.sort(key=lambda s: session_order.get(s.name, 999))
        console.print(f"Using starter set: {len(sessions)} sessions")
    elif args.sessions:
        session_names = set(args.sessions.split(","))
        sessions = [s for s in all_sessions if s.name in session_names]
        console.print(f"Using specified sessions: {len(sessions)} sessions")
    elif args.tier1:
        sessions = filter_tier1_sessions(all_sessions)
        console.print(f"Using Tier 1 sessions (no AFP deps): {len(sessions)} sessions")
    else:
        # Default to starter set
        sessions = [s for s in all_sessions if s.name in STARTER_SESSIONS]
        session_order = {name: i for i, name in enumerate(STARTER_SESSIONS)}
        sessions.sort(key=lambda s: session_order.get(s.name, 999))
        console.print(f"Using starter set (default): {len(sessions)} sessions")

    # Apply limit
    if args.limit:
        sessions = sessions[:args.limit]
        console.print(f"Limited to {len(sessions)} sessions")

    if not sessions:
        console.print("[red]No sessions found to test[/red]")
        sys.exit(1)

    # List only mode
    if args.list_only:
        console.print("\n[bold]Sessions to test:[/bold]")
        for s in sessions:
            deps = f" (deps: {', '.join(s.afp_dependencies)})" if s.afp_dependencies else ""
            console.print(f"  - {s.name} ({s.parent_session}, {s.timeout}s){deps}")
        sys.exit(0)

    # Run batch builds
    console.print(f"\n[bold]Starting batch build of {len(sessions)} sessions...[/bold]\n")
    results = run_batch(sessions, args.api_url, args.timeout)

    # Generate reports
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = generate_json_report(results, args.output_dir, timestamp)
    md_path = generate_markdown_report(results, args.output_dir, timestamp)

    console.print(f"\n[bold]Reports generated:[/bold]")
    console.print(f"  JSON: {json_path}")
    console.print(f"  Markdown: {md_path}")

    # Print summary
    console.print("\n")
    print_summary_table(results)

    # Exit with appropriate code
    success_count = sum(1 for r in results if r.built)
    if success_count == len(results):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
