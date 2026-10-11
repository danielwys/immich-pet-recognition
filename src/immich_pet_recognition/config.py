from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PET_", env_file=".env", extra="ignore")

    immich_url: str = "http://immich-server:2283"
    immich_api_key: SecretStr = SecretStr("")
    service_token: SecretStr = SecretStr("")
    data_dir: Path = Path("data")
    demo: bool = False
    background: bool = True
    poll_seconds: int = Field(default=300, ge=10)
    auto_tag: bool = False
    match_threshold: float = Field(default=0.85, ge=0, le=1)
    match_margin: float = Field(default=0.08, ge=0, le=2)
    min_references: int = Field(default=10, ge=1)
    detector_threshold: float = Field(default=0.5, ge=0, le=1)
    provider: str = "CPUExecutionProvider"
    detector_repo: str = "open-noodle/rfdetr-nano"
    detector_revision: str = "1618db5f22919339f4347d436da8c43afaf91926"
    recognizer_repo: str = "open-noodle/pet-recognition-large"
    recognizer_revision: str = "cff7cf1559cb99596273f26f1eaae117327d8eb1"

    @model_validator(mode="after")
    def validate_configuration(self):
        if not self.demo and not self.service_token.get_secret_value():
            raise ValueError("Set PET_SERVICE_TOKEN, or use PET_DEMO=true for an offline preview")
        if self.provider not in {"CPUExecutionProvider", "CUDAExecutionProvider"}:
            raise ValueError("PET_PROVIDER must be CPUExecutionProvider or CUDAExecutionProvider")
        if self.immich_url.startswith(("http://", "https://")) is False:
            raise ValueError("PET_IMMICH_URL must be an http(s) URL")
        return self

    @property
    def detector_key(self) -> str:
        return f"{self.detector_repo}@{self.detector_revision}:{self.detector_threshold}"

    @property
    def recognizer_key(self) -> str:
        return f"{self.recognizer_repo}@{self.recognizer_revision}"

    @property
    def pipeline_key(self) -> str:
        return f"{self.detector_key}|{self.recognizer_key}"
