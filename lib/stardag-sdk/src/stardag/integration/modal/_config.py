from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag.utils.resource_provider import resource_provider


class ModalConfig(BaseSettings):
    """Configuration of the modal integration.

    Examples:

    Setting the default modal app name:
    ```
    export STARDAG_MODAL_APP_NAME=stardag-default
    ```
    """

    default_app_name: str = "stardag-default"

    volume_mounts: dict[str, str] = {}  # path -> volume name

    model_config = SettingsConfigDict(
        env_prefix="stardag_modal_",
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,
    )

    @property
    def volume_name_to_mount_path(self) -> dict[str, Path]:
        return {v: Path(p.removesuffix("/")) for p, v in self.volume_mounts.items()}


modal_config_provider = resource_provider(ModalConfig, ModalConfig)
