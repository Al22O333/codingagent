"""Allow the package to run with ``python -m coding_agent``."""

from coding_agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
