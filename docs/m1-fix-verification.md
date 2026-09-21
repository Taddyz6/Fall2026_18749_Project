# M1 Fix Summary

README now provides a project overview and links to the detailed [M1 instructions](milestone-1-instructions.md).

Clients send automatically, waiting one second after each attempt; use `--interval` to change the delay or `--interactive` for manual input. They retry after connection failures and stop with Ctrl-C.

Validation passed: 101 tests and the M1 smoke test on macOS with Python 3.13. Multi-machine deployment was not tested; server state remains in memory, and retries after a lost reply may repeat an increment.
