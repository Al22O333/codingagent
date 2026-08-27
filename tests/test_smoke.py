"""Step 0 smoke tests."""

import coding_agent
from coding_agent.cli import STARTUP_MESSAGE, main


def test_package_import_and_cli_start(capsys) -> None:
    assert coding_agent.__name__ == "coding_agent"

    exit_code = main()

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == STARTUP_MESSAGE
