#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Quick test of AFP discovery."""

from pathlib import Path
from dataclasses import dataclass, field
import re

@dataclass
class SessionInfo:
    name: str
    path: Path
    parent_session: str
    timeout: int = 300
    theories: list = field(default_factory=list)
    afp_dependencies: list = field(default_factory=list)
    root_content: str = ""

def parse_root_file(root_path: Path):
    try:
        content = root_path.read_text()
    except Exception:
        return None

    session_match = re.search(
        r'session\s+(?:"([^"]+)"|([A-Za-z][A-Za-z0-9_-]*))\s*(?:\([^)]*\))?\s*=\s*(?:"([^"]+)"|([A-Za-z][A-Za-z0-9_-]*))\s*\+',
        content
    )
    if not session_match:
        return None

    session_name = session_match.group(1) or session_match.group(2)
    parent_session = session_match.group(3) or session_match.group(4)

    timeout_match = re.search(r'timeout\s*=\s*(\d+)', content)
    timeout = int(timeout_match.group(1)) if timeout_match else 300

    theories = []
    theory_blocks = re.findall(
        r'theories(?:\s*\[[^\]]*\])?\s+((?:[A-Za-z_][A-Za-z0-9_]*\s*)+)',
        content
    )
    for block in theory_blocks:
        theories.extend(block.split())

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

def main():
    afp_path = Path(__file__).parent / 'lib/afp/thys'
    entries = (afp_path / "ROOTS").read_text().strip().split('\n')
    sessions = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        root_file = afp_path / entry / "ROOT"
        if root_file.exists():
            session = parse_root_file(root_file)
            if session:
                sessions.append(session)

    print(f"Discovered {len(sessions)} sessions")

    # Filter tier1 (no AFP deps)
    tier1 = [s for s in sessions if not s.afp_dependencies]
    print(f"Tier1 (no AFP deps): {len(tier1)}")

    # Find starter sessions
    STARTER = ["Ackermanns_not_PR", "AnselmGod", "Abstract_Soundness", "AVL-Trees",
               "BinarySearchTree", "Boolos_Curious_Inference", "Card_Multisets",
               "List_Inversions", "Fibonacci_Sums", "Derangements"]
    starter = [s for s in sessions if s.name in STARTER]
    print(f"Starter found: {len(starter)}")
    for s in starter:
        print(f"  - {s.name} ({s.parent_session}, {s.timeout}s, theories: {s.theories})")

if __name__ == "__main__":
    main()
