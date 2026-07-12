from runtimeconditions_common_integrations import *


def declare() -> None:
    if False:
        Cache.declare(
            "request-cache",
            Cache.key_value(Cache.Engine.REDIS),
        )

