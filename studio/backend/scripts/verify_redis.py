"""Fail a service entrypoint unless the shared Redis dependency is reachable."""

from data.infra.redis_client import RedisService


def main() -> None:
    service = RedisService()
    service.connect_required()
    service.disconnect()


if __name__ == "__main__":
    main()
