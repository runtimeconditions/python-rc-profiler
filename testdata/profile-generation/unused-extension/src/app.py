from runtimeconditions_common_integrations import Api, Http
from runtimeconditions_env_configuration import EnvConfiguration


class Todo:
    id: int
    title: str
    completed: bool


UNUSED = EnvConfiguration.env("baseUrl", "TODOS_API_URL")


def declare() -> None:
    if False:
        Api.declare(
            "todos-api",
            Api.spec("openapi", "catalog://api/default/todos-api", "1.0.0"),
            Http.get("/todos/{id}", Http.response(Todo)),
        )

