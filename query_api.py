#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.27.0",
# ]
# ///
"""
Query the DeepIsaHOL API to verify an Isabelle theory file.

Usage:
    uv run query_api.py example.thy
    uv run query_api.py example.thy --api-url http://localhost:8000
    uv run query_api.py example.thy --logic HOL-Analysis --timeout 600
"""

import argparse
import sys
from pathlib import Path

import httpx


def load_theory_file(path: Path) -> str:
    """Load the content of a .thy file."""
    if not path.exists():
        raise FileNotFoundError(f"Theory file not found: {path}")
    if not path.suffix == ".thy":
        print(f"Warning: File does not have .thy extension: {path}", file=sys.stderr)
    return path.read_text()


def verify_theory(
    api_url: str,
    thy_content: str,
    logic: str = "HOL",
    timeout_seconds: int = 300,
) -> dict:
    """Send a theory to the API for verification."""
    url = f"{api_url.rstrip('/')}/verify"
    payload = {
        "thy_content": thy_content,
        "logic": logic,
        "timeout_seconds": timeout_seconds,
    }

    with httpx.Client(timeout=timeout_seconds + 30) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def check_health(api_url: str) -> dict:
    """Check the health of the API."""
    url = f"{api_url.rstrip('/')}/health"
    with httpx.Client(timeout=10) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def main():
    parser = argparse.ArgumentParser(
        description="Query the DeepIsaHOL API to verify an Isabelle theory file."
    )
    parser.add_argument(
        "thy_file",
        type=Path,
        help="Path to the .thy file to verify",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="URL of the DeepIsaHOL API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--logic",
        default="HOL",
        help="Isabelle logic to use (default: HOL)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds for verification (default: 300)",
    )
    parser.add_argument(
        "--health-only",
        action="store_true",
        help="Only check the health of the API",
    )

    args = parser.parse_args()

    # Check health first
    print(f"Checking API health at {args.api_url}...")
    try:
        health = check_health(args.api_url)
        print(f"  Status: {health['status']}")
        print(f"  Gateway available: {health['gateway_available']}")
        print(f"  Message: {health['message']}")
    except httpx.ConnectError:
        print(f"Error: Could not connect to API at {args.api_url}", file=sys.stderr)
        print("Make sure the Docker container is running: docker-compose up", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"Error: API returned status {e.response.status_code}", file=sys.stderr)
        sys.exit(1)

    if args.health_only:
        sys.exit(0)

    # Load and verify the theory file
    print(f"\nLoading theory file: {args.thy_file}")
    try:
        thy_content = load_theory_file(args.thy_file)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Verifying with logic '{args.logic}' (timeout: {args.timeout}s)...")
    print("-" * 50)

    try:
        result = verify_theory(
            api_url=args.api_url,
            thy_content=thy_content,
            logic=args.logic,
            timeout_seconds=args.timeout,
        )
    except httpx.HTTPStatusError as e:
        print(f"Error: API returned status {e.response.status_code}", file=sys.stderr)
        if e.response.text:
            print(f"Details: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except httpx.ReadTimeout:
        print("Error: Request timed out", file=sys.stderr)
        sys.exit(1)

    # Display results
    print(f"Success: {result['success']}")
    print(f"Verified: {result['verified']}")
    print(f"Message: {result['message']}")

    if result.get("errors"):
        print("\nErrors:")
        for error in result["errors"]:
            print(f"  - {error}")

    if result.get("state"):
        print(f"\nFinal state:\n{result['state']}")

    # Exit with appropriate code
    if result["verified"]:
        print("\n[OK] Theory verified successfully!")
        sys.exit(0)
    else:
        print("\n[FAIL] Theory verification failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
