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

# Thread pool for blocking Isabelle operations
executor = ThreadPoolExecutor(max_workers=4)


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


def _build_session_sync(
    session_name: str,
    root_content: str,
    theory_files: dict[str, str],
    options: list[str] | None
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

        # Run the build
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200  # Hard limit of 2 hours
        )

        build_time = time.time() - start_time
        output = result.stdout + result.stderr
        built = result.returncode == 0

        # Parse errors from output
        errors = _parse_build_errors(output)

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
        # Run the build in the thread pool with timeout
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                executor,
                _build_session_sync,
                request.session_name,
                request.root_content,
                request.theory_files,
                request.options
            ),
            timeout=request.timeout_seconds
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
            "/verify": "POST - Verify a theory file",
            "/build": "POST - Build an Isabelle session"
        }
    }
