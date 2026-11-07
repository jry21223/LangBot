"""Fresh-process pytest entrypoint for the disposable native verification image.

pytest-asyncio 1.3 probes the policy's previous loop during Runner setup. With a
fresh Python 3.12 policy, that probe otherwise allocates an unused implicit loop
which may be collected during later synchronous/async migration tests. Explicit
None prevents that bootstrap-only allocation; it does not close test/provider
loops or filter ResourceWarning/PytestUnraisableExceptionWarning.
"""
import asyncio
import sys

import pytest


def main() -> int:
    asyncio.set_event_loop(None)
    return int(pytest.main(sys.argv[1:]))


if __name__ == '__main__':
    raise SystemExit(main())
