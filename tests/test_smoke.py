"""Step 0 smoke tests."""

import coding_agent
from coding_agent.cli import STARTUP_MESSAGE


def test_package_import_and_cli_startup_message() -> None:
    assert coding_agent.__name__ == "coding_agent"
    assert STARTUP_MESSAGE == "Coding Agent v1"
