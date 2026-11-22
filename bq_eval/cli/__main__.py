"""Module entrypoint enabling `python -m bq_eval.cli`."""

from bq_eval.cli.app import main

if __name__ == "__main__":
    raise SystemExit(main())
