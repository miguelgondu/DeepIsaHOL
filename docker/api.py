"""
FastAPI web server for DeepIsaHOL proof verification.

This module provides a REST API for verifying Isabelle/HOL proofs.
"""

import os
import sys
import asyncio
import logging
import tempfile
import subprocess
import shutil
import signal
import time
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Add the project's Python source to the path
sys.path.insert(0, "/app/src/main/python")

from repl import REPL

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Thread pool for blocking Isabelle operations. Each `isabelle build` call
# maps the full prebuilt Benchmark heap (HOL-Analysis, HOL-Algebra, etc.) into
# its own process — docker-compose.yml's own sizing note says a single build
# wants 8-16GB against this container's 16GB hard cap, so running several of
# these at once risks the OOM killer (see the signal-kill handling in
# `_build_session_sync`). Default kept low for that reason; override via
# BUILD_MAX_WORKERS if the container is sized for more.
BUILD_MAX_WORKERS = int(os.environ.get("BUILD_MAX_WORKERS", "2"))
executor = ThreadPoolExecutor(max_workers=BUILD_MAX_WORKERS)


class VerifyRequest(BaseModel):
    """Request model for proof verification."""
    thy_content: str = Field(
        ...,
        description="The complete content of the .thy file to verify"
    )
    logic: str = Field(
        default="HOL",
        description="The Isabelle logic to use (e.g., HOL, HOL-Analysis)"
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Timeout in seconds for the verification"
    )


class VerifyResponse(BaseModel):
    """Response model for proof verification."""
    success: bool = Field(
        ...,
        description="Whether the API call completed successfully"
    )
    verified: bool = Field(
        ...,
        description="Whether the proof was successfully verified"
    )
    errors: list[str] = Field(
        default_factory=list,
        description="List of error messages if verification failed"
    )
    state: Optional[str] = Field(
        default=None,
        description="The final Isabelle state after verification"
    )
    message: str = Field(
        ...,
        description="Human-readable message about the result"
    )


class HealthResponse(BaseModel):
    """Response model for health check."""
    status: str
    gateway_available: bool
    message: str


class SessionsResponse(BaseModel):
    """Which Isabelle sessions the running image's `Benchmark` heap was
    actually built from (DEEPISAHOL_PARENT_SESSION / DEEPISAHOL_EXTRA_SESSIONS
    at Docker build time)."""
    parent_session: Optional[str] = Field(
        default=None,
        description="Session `Benchmark` extends, e.g. HOL or HOL-Probability"
    )
    extra_sessions: list[str] = Field(
        default_factory=list,
        description="Sibling sessions built and loaded alongside the parent"
    )
    benchmark_theories: list[str] = Field(
        default_factory=list,
        description="Umbrella theories loaded into the Benchmark heap, one per extra session"
    )


class BuildRequest(BaseModel):
    """Request model for building an Isabelle session."""
    session_name: str = Field(
        ...,
        description="Name matching the ROOT file session"
    )
    root_content: str = Field(
        ...,
        description="Content of the ROOT file"
    )
    theory_files: dict[str, str] = Field(
        ...,
        description="Map of filename to content for theory files"
    )
    timeout_seconds: int = Field(
        default=600,
        ge=1,
        le=7200,
        description="Timeout in seconds for the build"
    )
    options: Optional[list[str]] = Field(
        default=None,
        description="Build options like -v, -j2"
    )


class BuildError(BaseModel):
    """Structured error from build output."""
    theory: Optional[str] = Field(
        default=None,
        description="Theory file with error"
    )
    line: Optional[int] = Field(
        default=None,
        description="Line number of error"
    )
    message: str = Field(
        ...,
        description="Error message"
    )


class BuildResponse(BaseModel):
    """Response model for session build."""
    success: bool = Field(
        ...,
        description="Whether the API call completed successfully"
    )
    built: bool = Field(
        ...,
        description="Whether the session built successfully"
    )
    session_name: str = Field(
        ...,
        description="Session that was built"
    )
    build_log: str = Field(
        ...,
        description="Complete build output"
    )
    errors: list[BuildError] = Field(
        default_factory=list,
        description="Parsed errors from build"
    )
    build_time_seconds: Optional[float] = Field(
        default=None,
        description="Build duration in seconds"
    )
    message: str = Field(
        ...,
        description="Human-readable result message"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    logger.info("DeepIsaHOL API starting up...")
    yield
    logger.info("DeepIsaHOL API shutting down...")
    executor.shutdown(wait=True)


app = FastAPI(
    title="DeepIsaHOL API",
    description="REST API for verifying Isabelle/HOL proofs",
    version="1.0.0",
    lifespan=lifespan
)


def _check_gateway_available() -> bool:
    """Check if the Py4J gateway is available."""
    ports_file = "/app/ports.json"
    if not os.path.exists(ports_file):
        return False
    try:
        import json
        with open(ports_file, "r") as f:
            ports = json.load(f)
        return len(ports) > 0
    except Exception:
        return False


_BENCHMARK_ROOT_PATH = "/app/benchmark/ROOT"


def _read_benchmark_info() -> dict:
    """Parse the running image's generated `benchmark/ROOT` (see
    `benchmark/gen_root.py`) to report which sessions/theories the
    `Benchmark` heap was actually built with. Live-parsed rather than
    cached from a build-time env var so this can never drift from the
    heap that's actually on disk."""
    try:
        with open(_BENCHMARK_ROOT_PATH) as f:
            text = f.read()
    except OSError:
        return {"parent_session": None, "extra_sessions": [], "benchmark_theories": []}

    parent_match = re.search(r'session\s+Benchmark\s*=\s*"?([\w.-]+)"?\s*\+', text)
    parent = parent_match.group(1) if parent_match else None

    sessions_match = re.search(r'\n[ \t]*sessions[ \t]*\n((?:[ \t]*"[^"]+"[ \t]*\n)+)', text)
    extra_sessions = re.findall(r'"([^"]+)"', sessions_match.group(1)) if sessions_match else []

    theories_match = re.search(r'\n[ \t]*theories[ \t]*\n((?:[ \t]*"[^"]+"[ \t]*\n?)+)', text)
    theories = re.findall(r'"([^"]+)"', theories_match.group(1)) if theories_match else []

    return {
        "parent_session": parent,
        "extra_sessions": extra_sessions,
        "benchmark_theories": theories,
    }


# Isabelle Unicode to ASCII mappings
# Isabelle accepts both Unicode symbols and ASCII equivalents
ISABELLE_UNICODE_TO_ASCII = {
    "⇒": "=>",      # function arrow
    "⟹": "==>",     # meta implication
    "⟸": "<==",     # reverse meta implication
    "⇔": "<=>",     # equivalence
    "∧": "&",       # conjunction (also /\)
    "∨": "|",       # disjunction (also \/)
    "¬": "~",       # negation
    "≠": "~=",      # not equal
    "≤": "<=",      # less or equal
    "≥": ">=",      # greater or equal
    "∈": ":",       # element of (in set context)
    "∉": "~:",      # not element of
    "⊆": "<=",      # subset (in set context)
    "⊂": "<",       # proper subset
    "∩": "Int",     # intersection
    "∪": "Un",      # union
    "λ": "%",       # lambda
    "∀": "!",       # forall
    "∃": "?",       # exists
    "′": "'",       # prime (for derivatives, etc.)
    "×": "*",       # cartesian product
    "→": "->",      # simple arrow
    "←": "<-",      # reverse arrow
    "↔": "<->",     # bi-directional arrow
    "⦃": "{:",      # term antiquotation open
    "⦄": ":}",      # term antiquotation close
    "⟦": "[[",      # double bracket open
    "⟧": "]]",      # double bracket close
    "⊢": "|-",      # turnstile
    "⊥": "False",   # bottom/false
    "⊤": "True",    # top/true
}


def _normalize_isabelle_unicode(text: str) -> str:
    """Convert Isabelle Unicode symbols to ASCII equivalents."""
    for unicode_char, ascii_equiv in ISABELLE_UNICODE_TO_ASCII.items():
        text = text.replace(unicode_char, ascii_equiv)
    return text


# Isabelle command keywords that start new top-level commands
COMMAND_KEYWORDS = {
    # Document structure
    "section", "subsection", "subsubsection", "paragraph", "text", "txt",
    # Type definitions
    "datatype", "codatatype", "type_synonym", "typedecl",
    # Function definitions
    "fun", "function", "primrec", "definition", "abbreviation", "value",
    # Inductive definitions
    "inductive", "inductive_set", "coinductive", "inductive_cases",
    # Other definitions
    "record", "typedef", "consts", "defs",
    # Type classes
    "class", "instance", "instantiation", "interpretation", "subclass",
    # Locales and contexts
    "locale", "context", "sublocale",
    # Theorems and proofs
    "lemma", "theorem", "corollary", "proposition", "schematic_goal",
    # Proof commands (these are also command starters)
    # Note: "by" and "proof" are NOT included - they belong with the preceding lemma/theorem
    # e.g., "lemma P by auto" or "lemma P proof ... qed" should keep lemma+by/proof together
    "qed", "done", "sorry", "oops",
    "apply", "apply_end", "defer", "prefer",
    "have", "show", "thus", "hence", "obtain", "fix", "assume",
    "let", "note", "from", "with", "using", "unfolding",
    "next", "case", "subgoal", "moreover", "ultimately",
    # Other
    "declare", "setup", "ML", "ML_file", "ML_val",
    "notepad", "experiment", "named_theorems",
    "method_setup", "attribute_setup",
    "no_notation", "notation", "syntax", "translations",
    "hide_fact", "hide_const", "hide_type",
}


def _parse_isabelle_commands(thy_content: str) -> list[str]:
    """
    Parse Isabelle theory content into individual commands.

    Isabelle commands span multiple lines and are delimited by command keywords,
    not by newlines. This function accumulates lines until a new command keyword
    is encountered.

    Lines are joined with newlines (not spaces) to preserve Unicode characters
    and original formatting that Isabelle expects.
    """
    lines = thy_content.split('\n')
    commands = []
    current_command_lines = []
    in_theory_body = False
    in_comment = False

    for line in lines:
        # Track multi-line comments
        if '(*' in line and '*)' not in line:
            in_comment = True
        if '*)' in line:
            in_comment = False
            continue
        if in_comment:
            continue

        line_stripped = line.strip()

        # Skip single-line comments
        if line_stripped.startswith('(*') and line_stripped.endswith('*)'):
            continue

        # Skip empty lines when not accumulating
        if not line_stripped and not current_command_lines:
            continue

        # Handle theory header
        if line_stripped.startswith('theory '):
            continue
        if line_stripped.startswith('imports '):
            continue
        if line_stripped == 'begin':
            in_theory_body = True
            continue
        if line_stripped == 'end':
            # Flush any remaining command
            if current_command_lines:
                commands.append('\n'.join(current_command_lines))
                current_command_lines = []  # Clear to prevent duplicate at end of loop
            break

        if not in_theory_body:
            continue

        # Check if this line starts a new command
        first_word = line_stripped.split()[0] if line_stripped.split() else ""
        # Remove any trailing colon or similar
        first_word_clean = first_word.rstrip(':')

        if first_word_clean in COMMAND_KEYWORDS:
            # Flush the previous command if any
            if current_command_lines:
                commands.append('\n'.join(current_command_lines))
                current_command_lines = []

        # Add this line to the current command (preserve original line, not stripped)
        if line_stripped:
            current_command_lines.append(line)

    # Don't forget the last command
    if current_command_lines:
        commands.append('\n'.join(current_command_lines))

    return commands


def _verify_theory_sync(thy_content: str, logic: str) -> dict:
    """
    Synchronously verify a theory file content.

    This function creates a fresh REPL instance, loads the theory content,
    and attempts to verify it step by step.
    """
    repl = None
    errors = []
    state = None
    verified = False

    try:
        # Create a fresh REPL instance
        logger.info(f"Creating REPL with logic: {logic}")
        repl = REPL(logic=logic, thy_name="Scratch.thy")

        # Parse the theory content into complete commands
        commands = _parse_isabelle_commands(thy_content)
        logger.info(f"Parsed {len(commands)} commands from theory")

        for cmd in commands:
            # Normalize Unicode to ASCII for compatibility with Py4J/JVM
            cmd_normalized = _normalize_isabelle_unicode(cmd)

            # Apply each command
            logger.debug(f"Applying: {cmd_normalized[:80]}{'...' if len(cmd_normalized) > 80 else ''}")
            _result = repl.apply(cmd_normalized)  # noqa: F841

            # Check for errors
            error = repl.last_error()
            if error:
                # Truncate command for error message if too long
                cmd_display = cmd_normalized[:60] + '...' if len(cmd_normalized) > 60 else cmd_normalized
                errors.append(f"Error at '{cmd_display}': {error}")
                logger.warning(f"Verification error: {error}")

        # Get final state
        state = repl.state_string()

        # Check if we ended without subgoals (successful proof)
        # If there are no errors, we consider it verified
        verified = len(errors) == 0

        logger.info(f"Verification complete. Verified: {verified}, Errors: {len(errors)}")

    except Exception as e:
        logger.error(f"Exception during verification: {e}")
        errors.append(str(e))
        verified = False

    finally:
        # Clean up the REPL
        if repl is not None:
            try:
                repl.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting REPL: {e}")

    return {
        "verified": verified,
        "errors": errors,
        "state": state
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint.

    Returns the status of the API and whether the Py4J gateway is available.
    """
    gateway_available = _check_gateway_available()

    if gateway_available:
        return HealthResponse(
            status="healthy",
            gateway_available=True,
            message="DeepIsaHOL API is running and gateway is available"
        )
    else:
        return HealthResponse(
            status="degraded",
            gateway_available=False,
            message="DeepIsaHOL API is running but gateway is not available"
        )


@app.get("/sessions", response_model=SessionsResponse)
async def sessions_info():
    """Report which Isabelle sessions/theories this image's `Benchmark` heap
    was actually built from. This is the source of truth for callers
    deciding what a submitted theory can `imports` without triggering an
    on-the-fly build — see `DEEPISAHOL_PARENT_SESSION` /
    `DEEPISAHOL_EXTRA_SESSIONS` in the Dockerfile.
    """
    return SessionsResponse(**_read_benchmark_info())


@app.post("/verify", response_model=VerifyResponse)
async def verify_proof(request: VerifyRequest):
    """
    Verify an Isabelle/HOL theory file.

    This endpoint takes the content of a .thy file and attempts to verify
    all proofs within it. A fresh REPL instance is created for each request
    to ensure isolation.
    """
    # Check if gateway is available
    if not _check_gateway_available():
        raise HTTPException(
            status_code=503,
            detail="Py4J gateway is not available. Please wait for Isabelle to initialize."
        )

    try:
        # Run the verification in the thread pool with timeout
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                executor,
                _verify_theory_sync,
                request.thy_content,
                request.logic
            ),
            timeout=request.timeout_seconds
        )

        return VerifyResponse(
            success=True,
            verified=result["verified"],
            errors=result["errors"],
            state=result["state"],
            message="Verification complete" if result["verified"] else "Verification failed"
        )

    except asyncio.TimeoutError:
        return VerifyResponse(
            success=False,
            verified=False,
            errors=["Verification timed out"],
            state=None,
            message=f"Verification timed out after {request.timeout_seconds} seconds"
        )

    except Exception as e:
        logger.error(f"Unexpected error during verification: {e}")
        return VerifyResponse(
            success=False,
            verified=False,
            errors=[str(e)],
            state=None,
            message=f"Verification failed with error: {str(e)}"
        )


# Allowed build options (security whitelist)
ALLOWED_BUILD_OPTIONS = {"-v", "-j", "-N", "-o"}

# Session name validation pattern
SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _signal_name_from_returncode(returncode: int) -> str | None:
    """Best-effort: was this exit code a signal kill, not a normal exit?

    Covers two conventions, since we can't be sure which layer of the
    process tree reports the kill:
    - Negative (Python/POSIX ``subprocess`` convention): our direct child —
      the ``isabelle`` wrapper/JVM process — was itself signaled.
    - ``128 + signum`` (POSIX shell convention, e.g. 137 for SIGKILL): common
      when a wrapping shell reports a killed child's exit status this way.

    Neither convention fires if the OOM killer instead takes one of
    ``isabelle build``'s own ML worker subprocesses while the parent JVM
    survives and exits "normally" with its own nonzero code — that case
    isn't a signal kill by either convention, so it's caught separately by
    the content-based check in ``_build_session_sync`` (empty ``errors``
    despite ``built=False``).
    """
    if returncode < 0:
        signum = -returncode
    elif 128 < returncode <= 128 + 64:  # 64 covers up through SIGRTMAX
        signum = returncode - 128
    else:
        return None
    try:
        return signal.Signals(signum).name
    except ValueError:
        return str(signum)


# Substrings that show up when a build crashed or was killed before it could
# produce a proper Isar error — seen in practice from the OOM killer, but
# also covers other infra-level crashes (JVM heap errors, native segfaults)
# that don't fit the exit-code conventions `_signal_name_from_returncode`
# checks (e.g. an ML worker subprocess gets killed while the parent JVM
# survives and exits "normally" with its own nonzero code).
_INFRA_FAILURE_MARKERS = (
    "Killed",
    "Out of memory",
    "Cannot allocate memory",
    "OutOfMemoryError",
    "java.lang.OutOfMemory",
    "std::bad_alloc",
    "core dumped",
)


def _parse_build_errors(output: str) -> list[dict]:
    """
    Parse Isabelle build error output.

    Isabelle errors typically look like:
    *** <theory>.thy line <n>: <message>
    or
    *** <message>
    """
    errors = []
    error_pattern = re.compile(
        r"\*\*\*\s+(?:(\S+\.thy)(?:\s+line\s+(\d+))?:\s*)?(.+)"
    )

    for line in output.split('\n'):
        match = error_pattern.match(line.strip())
        if match:
            theory = match.group(1)
            line_num = int(match.group(2)) if match.group(2) else None
            message = match.group(3).strip()
            if message:
                errors.append({
                    "theory": theory,
                    "line": line_num,
                    "message": message
                })

    return errors


_YXML_X = "\x05"  # Isabelle YXML element/close delimiter (system manual §1.6)
_YXML_Y = "\x06"  # separates a YXML tag name from its attributes


def _yxml_to_text(data: bytes) -> str:
    """Decode Isabelle's YXML markup down to its plain body text.

    YXML (system manual §1.6) delimits elements with ASCII control chars
    X=\\x05/Y=\\x06: ``<name attr=val>`` is ``X Y name Y attr=val X`` and
    ``</name>`` is ``X Y X``. Splitting the document on X, any chunk
    starting with Y is markup (a tag open/close) and gets dropped; every
    other non-empty chunk is literal body text, kept in original order.
    That's not full pretty-printing (no block-driven line breaks), but
    prover output like `find_theorems` results already embeds its own
    line breaks as literal text, so this reproduces it faithfully enough
    to hand back to a caller.
    """
    text = data.decode("utf-8", errors="replace")
    return "".join(
        chunk for chunk in text.split(_YXML_X) if chunk and not chunk.startswith(_YXML_Y)
    )


def _fetch_verbose_messages(isabelle_bin: str, session_name: str) -> str | None:
    """Best-effort fetch of prover messages (`writeln`, `find_theorems`
    results, etc.) recorded for ``session_name`` during the build that just
    ran.

    ``isabelle build -v`` only raises the *build tool's own* verbosity (job
    scheduling, timing) — it never echoes what commands like `find_theorems`
    printed during theory processing. The ostensibly-for-this tool,
    ``isabelle build_log -v``, doesn't work either in this Isabelle version:
    its listing of "used theories" is driven by a `theory_timing` protocol
    marker that plain `isabelle build` never actually emits (confirmed even
    against the prebuilt `HOL` session — `isabelle build_log -T HOL.Nat HOL`
    reports "Unknown theories"), so it always comes back empty.

    The message text itself *is* recorded regardless, unconditionally, per
    theory, as a `PIDE/messages` export in the session's build database
    (`isabelle export -x "*:PIDE/messages"` finds it by session name alone,
    same as `build_log` would have). Pull that out directly and decode its
    YXML. Returns ``None`` on any failure, so callers can treat this as pure
    best-effort.
    """
    export_dir = tempfile.mkdtemp(prefix="isabelle_export_")
    try:
        result = subprocess.run(
            [
                isabelle_bin, "export",
                "-O", export_dir,
                "-x", "*:PIDE/messages",
                "-n",
                session_name,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(f"isabelle export failed for {session_name}: {result.stderr}")
            return None

        chunks = []
        for root, _dirs, files in os.walk(export_dir):
            if "messages" not in files:
                continue
            path = os.path.join(root, "messages")
            theory = os.path.basename(os.path.dirname(root))
            with open(path, "rb") as f:
                text = _yxml_to_text(f.read()).strip()
            if text:
                chunks.append(f"--- {theory} ---\n{text}")
        return "\n\n".join(sorted(chunks)) or None
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"isabelle export failed for {session_name}: {e}")
        return None
    finally:
        shutil.rmtree(export_dir, ignore_errors=True)


def _validate_build_options(options: list[str] | None) -> list[str]:
    """Validate and filter build options against whitelist."""
    if not options:
        return []

    validated = []
    for opt in options:
        # Check if option starts with an allowed prefix
        opt_base = opt.split("=")[0] if "=" in opt else opt
        # Handle options like -j2, -j 2, etc.
        opt_prefix = re.match(r"^(-[A-Za-z])", opt_base)
        if opt_prefix and opt_prefix.group(1) in ALLOWED_BUILD_OPTIONS:
            validated.append(opt)
        elif opt_base in ALLOWED_BUILD_OPTIONS:
            validated.append(opt)

    return validated


def _list_descendants(pid: int) -> list[int]:
    """Every descendant of ``pid`` (children, grandchildren, ...), found by
    walking ``/proc`` rather than by process group.

    Isabelle's own subprocess machinery calls ``setsid()`` on each process
    it launches internally (confirmed in practice: the actual ``poly``
    process doing proof search, and a `Naproche` helper server, each end up
    in a *new* session/process group of their own, not the top-level
    ``isabelle build`` launcher's group). That means a single
    ``os.killpg`` on the launcher's group only reaches the launcher itself
    — the real CPU-heavy work underneath survives as an orphan. Walking
    the actual parent/child tree via each process's ``/proc/<pid>/stat``
    finds it regardless of how many nested ``setsid`` calls sit in between.
    """
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
            # `comm` (2nd field) is parenthesized and may itself contain
            # spaces/parens, so split on the *last* ')' rather than field index.
            after_comm = stat.rsplit(")", 1)[1].split()
            ppid = int(after_comm[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))

    descendants = []
    frontier = [pid]
    while frontier:
        frontier = [c for p in frontier for c in children.get(p, ())]
        descendants.extend(frontier)
    return descendants


def _kill_process_tree(pid: int) -> None:
    """SIGKILL ``pid`` and every descendant found via `_list_descendants`.

    The descendant walk must happen before any killing starts: once a
    process dies its children are reparented (to pid 1 in this container),
    which would sever the very parent/child links we're trying to follow.
    """
    for p in _list_descendants(pid) + [pid]:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _build_session_sync(
    session_name: str,
    root_content: str,
    theory_files: dict[str, str],
    options: list[str] | None,
    timeout_seconds: int,
) -> dict:
    """
    Synchronously build an Isabelle session.

    Creates a temporary directory, writes the ROOT and theory files,
    runs isabelle build, and returns the results.
    """
    temp_dir = None
    start_time = time.time()

    try:
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="isabelle_build_")
        logger.info(f"Created temp directory: {temp_dir}")

        # Write ROOT file
        root_path = os.path.join(temp_dir, "ROOT")
        with open(root_path, "w") as f:
            f.write(root_content)
        logger.info(f"Wrote ROOT file: {root_path}")

        # Write theory files (with filename sanitization)
        for filename, content in theory_files.items():
            # Security: sanitize filename to prevent directory traversal
            safe_filename = os.path.basename(filename)
            if not safe_filename.endswith(".thy"):
                safe_filename += ".thy"

            file_path = os.path.join(temp_dir, safe_filename)
            with open(file_path, "w") as f:
                f.write(content)
            logger.info(f"Wrote theory file: {file_path}")

        # Build the command
        isabelle_bin = "/app/Isabelle2025-2/bin/isabelle"
        cmd = [isabelle_bin, "build", "-D", temp_dir]

        # Add validated options
        validated_options = _validate_build_options(options)
        cmd.extend(validated_options)

        logger.info(f"Running build command: {' '.join(cmd)}")

        # Popen (not subprocess.run) so a timeout below can actually kill the
        # whole process tree via _kill_process_tree — subprocess.run's own
        # timeout only kills its immediate child, which would leave isabelle
        # build's JVM/poly descendants (the ones actually burning CPU on a
        # runaway proof search) running forever, silently occupying one of
        # this container's fixed executor slots. start_new_session detaches
        # the build from this process's own session (isolation, not what
        # makes the kill work — Isabelle setsid()s its own children too, so
        # _kill_process_tree walks /proc by parent pid rather than relying
        # on process groups).
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            build_time = time.time() - start_time
            logger.error(
                f"Build exceeded {timeout_seconds}s — killing process group "
                f"for pid {proc.pid} instead of leaving it to run unbounded"
            )
            _kill_process_tree(proc.pid)
            # Drain whatever was buffered before the kill; the process is
            # dead now so this returns immediately instead of blocking.
            stdout, stderr = proc.communicate()
            message = (
                f"Build timed out after {timeout_seconds} seconds and was "
                "killed (including its JVM/poly subprocesses)"
            )
            return {
                "built": False,
                "build_log": (stdout or "") + (stderr or "") or message,
                "errors": [{"theory": None, "line": None, "message": message}],
                "build_time_seconds": build_time,
            }

        build_time = time.time() - start_time
        output = stdout + stderr
        built = returncode == 0

        # `-v` is meant to surface command-level output (find_theorems, etc.)
        # — `isabelle build` itself never prints that (see
        # `_fetch_verbose_messages`), so pull it from the build database
        # ourselves and fold it into the log the caller sees.
        if "-v" in validated_options:
            messages = _fetch_verbose_messages(isabelle_bin, session_name)
            if messages:
                output = f"{output}\n\n=== Prover messages (find_theorems, etc.) ===\n{messages}"

        # A process killed by a signal — most commonly SIGKILL from the OOM
        # killer when concurrent builds (see `executor` above) push the
        # container over its memory limit — produces no `***`-formatted Isar
        # error, so `_parse_build_errors` would otherwise find nothing and
        # this would surface as a content-free "Build failed" with no way to
        # tell it apart from a genuine proof error. Report it explicitly.
        signal_name = None if built else _signal_name_from_returncode(returncode)
        if signal_name is not None:
            message = (
                f"Build process was killed by signal {signal_name} "
                "before producing output — likely an out-of-memory kill "
                "under concurrent build load, not a proof error."
            )
            logger.error(message)
            return {
                "built": False,
                "build_log": output or message,
                "errors": [{"theory": None, "line": None, "message": message}],
                "build_time_seconds": build_time,
            }

        # Parse errors from output
        errors = _parse_build_errors(output)

        # The OOM killer can also take one of `isabelle build`'s own ML
        # worker subprocesses while the parent JVM survives and exits
        # "normally" with its own nonzero code — invisible to the exit-code
        # check above. If the build failed but produced no structured error
        # at all, that's the same signature (a crash upstream of Isabelle's
        # own error reporting, not a rejected proof): flag it instead of
        # silently falling through to a bare, content-free "Build failed".
        if not built and not errors:
            marker = next((m for m in _INFRA_FAILURE_MARKERS if m in output), None)
            message = (
                "Build failed with no Isabelle error output "
                f"({f'saw {marker!r} in the log' if marker else 'log was empty/unparseable'})"
                " — likely a crash or OOM kill of a build subprocess, not a "
                "proof error."
            )
            logger.error(message)
            errors = [{"theory": None, "line": None, "message": message}]

        logger.info(
            f"Build completed in {build_time:.2f}s. "
            f"Success: {built}, Errors: {len(errors)}"
        )

        return {
            "built": built,
            "build_log": output,
            "errors": errors,
            "build_time_seconds": build_time
        }

    except subprocess.TimeoutExpired:
        build_time = time.time() - start_time
        logger.error("Build timed out")
        return {
            "built": False,
            "build_log": "Build timed out",
            "errors": [{"theory": None, "line": None, "message": "Build timed out"}],
            "build_time_seconds": build_time
        }

    except Exception as e:
        build_time = time.time() - start_time
        logger.error(f"Build failed with exception: {e}")
        return {
            "built": False,
            "build_log": str(e),
            "errors": [{"theory": None, "line": None, "message": str(e)}],
            "build_time_seconds": build_time
        }

    finally:
        # Clean up temp directory
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                logger.info(f"Cleaned up temp directory: {temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory: {e}")


@app.post("/build", response_model=BuildResponse)
async def build_session(request: BuildRequest):
    """
    Build an Isabelle session using isabelle build.

    This endpoint builds a complete session with multiple theory files
    using Isabelle's native build system. Unlike /verify which uses
    the REPL for step-by-step verification, /build compiles the entire
    session at once.
    """
    # Validate session name
    if not SESSION_NAME_PATTERN.match(request.session_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid session name. Must start with a letter and contain only letters, numbers, underscores, and hyphens."
        )

    # Validate session name appears in ROOT content
    if request.session_name not in request.root_content:
        raise HTTPException(
            status_code=400,
            detail=f"Session name '{request.session_name}' not found in ROOT content"
        )

    # Validate theory_files is not empty
    if not request.theory_files:
        raise HTTPException(
            status_code=400,
            detail="theory_files cannot be empty"
        )

    try:
        # Run the build in the thread pool. The real deadline is enforced
        # inside `_build_session_sync` itself (it kills the isabelle build
        # process group on `timeout_seconds`), so this outer `wait_for` is
        # just a safety margin for the file I/O/kill/drain overhead around
        # that — it must never fire first, or we'd be back to abandoning a
        # still-running build that leaks an executor slot.
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                executor,
                _build_session_sync,
                request.session_name,
                request.root_content,
                request.theory_files,
                request.options,
                request.timeout_seconds,
            ),
            timeout=request.timeout_seconds + 30
        )

        # Convert error dicts to BuildError models
        errors = [BuildError(**e) for e in result["errors"]]

        return BuildResponse(
            success=True,
            built=result["built"],
            session_name=request.session_name,
            build_log=result["build_log"],
            errors=errors,
            build_time_seconds=result["build_time_seconds"],
            message="Build succeeded" if result["built"] else "Build failed"
        )

    except asyncio.TimeoutError:
        return BuildResponse(
            success=False,
            built=False,
            session_name=request.session_name,
            build_log="Build timed out",
            errors=[BuildError(message=f"Build timed out after {request.timeout_seconds} seconds")],
            build_time_seconds=request.timeout_seconds,
            message=f"Build timed out after {request.timeout_seconds} seconds"
        )

    except Exception as e:
        logger.error(f"Unexpected error during build: {e}")
        return BuildResponse(
            success=False,
            built=False,
            session_name=request.session_name,
            build_log=str(e),
            errors=[BuildError(message=str(e))],
            build_time_seconds=None,
            message=f"Build failed with error: {str(e)}"
        )


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "name": "DeepIsaHOL API",
        "version": "1.0.0",
        "description": "REST API for verifying Isabelle/HOL proofs",
        "endpoints": {
            "/health": "GET - Health check",
            "/sessions": "GET - Which Isabelle sessions the Benchmark heap was built with",
            "/verify": "POST - Verify a theory file",
            "/build": "POST - Build an Isabelle session"
        }
    }
