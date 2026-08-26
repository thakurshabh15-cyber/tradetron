#!/usr/bin/env python
"""Run webhook integration tests with proper setup."""

import sys
import subprocess
import os


def main():
    # Ensure we're in the right directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
    
    # Run pytest with asyncio support
    cmd = [
        sys.executable, "-m", "pytest",
        "tests/test_webhooks_integration.py",
        "-v",
        "--tb=short",
        "-x",  # Stop on first failure
    ]
    
    # Add coverage if available
    try:
        import pytest_cov
        cmd.extend(["--cov=app.webhooks", "--cov-report=term-missing"])
    except ImportError:
        pass
    
    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())