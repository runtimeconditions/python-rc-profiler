import runtimeconditions_common_integrations as common
from runtimeconditions_env_configuration import EnvConfiguration as Env

from models import UserResponse
from settings import Settings


def declare() -> None:
    if False:
        common.Api.declare(
            Settings.API_NAME,
            common.Api.spec("openapi", Settings.SPEC_URI, Settings.VERSION),
            common.Http.get(Settings.PATH, common.Http.response(UserResponse)),
            Env.env("token", Settings.AUTH_TOKEN, Env.sensitive(), Env.optional()),
        )

        common.Cache.declare(
            Settings.CACHE_NAME,
            common.Cache.key_value(common.Cache.Engine.REDIS),
            Env.env_alternative(Env.env("url", Settings.REDIS_URL)),
            Env.env_alternative(
                Env.env("hostname", Settings.REDIS_HOST),
                Env.env("port", Settings.REDIS_PORT),
            ),
        )

