from pydantic_settings import  BaseSettings, SettingsConfigDict 

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://arthatantra:arthatantra@localhost:5432/arthatantra"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()