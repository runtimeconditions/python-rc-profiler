from runtimeconditions_common_integrations import Api, Cache, Http
from runtimeconditions_env_configuration import EnvConfiguration

from models import Todo


def declare() -> None:
    if False:
        Api.declare(
            "todos-api",
            Api.spec("openapi", "catalog://api/default/todos-api", "1.0.0"),
            Http.get("/todos/{id}", Http.response(Todo)),
            EnvConfiguration.env("baseUrl", "TODOS_API_URL"),
        )

        Cache.declare(
            "request-cache",
            Cache.key_value(Cache.Engine.REDIS),
            EnvConfiguration.env_alternative(EnvConfiguration.env("url", "REDIS_URL")),
            EnvConfiguration.env_alternative(
                EnvConfiguration.env("hostname", "REDIS_HOST"),
                EnvConfiguration.env("port", "REDIS_PORT"),
            ),
        )

