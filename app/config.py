from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Diadoc service API"
    APP_DESCRIPTION: str = "Сервис для работы с документами из Diadoc"
    APP_VERSION: str = "1.0.0"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
