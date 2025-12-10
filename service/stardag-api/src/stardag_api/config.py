from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://stardag:stardag@localhost:5432/stardag"
    debug: bool = False

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_")


settings = Settings()
