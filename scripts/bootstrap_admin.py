"""Grant the first Platform Admin role: uv run python scripts/bootstrap_admin.py --email <email>."""

import argparse
import asyncio
import sys

from management.core.errors import APIError
from management.identity.bootstrap_admin import bootstrap_admin


def main() -> None:
    parser = argparse.ArgumentParser(description="Grant the first Platform Admin role")
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(bootstrap_admin(args.email))
    except APIError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Platform Admin role granted.")


if __name__ == "__main__":
    main()
