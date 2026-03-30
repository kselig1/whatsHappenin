from pydantic_settings import BaseSettings, SettingsConfigDict 

class Config(BaseSettings): 
    OPENAI_API_KEY: str  
    GROQ_API_KEY: str   
    GOOGLE_API_KEY: str  
    QDRANT_API_KEY: str
    QDRANT_URL: str

    model_config = SettingsConfigDict(env_file = ".env", extra="ignore")

config = Config()