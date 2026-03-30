from pydantic_settings import BaseSettings, SettingsConfigDict 

class Config(BaseSettings): 
    OPENAI_API_KEY: str  
    GROQ_API_KEY: str   
    GOOGLE_API_KEY: str  
    
    model_config = SettingsConfigDict(env_file = ".env", extra="ignore")

    # API_URL: str = "http://api:8000"
    API_URL: str = "http:/127.0.0.1:8000"

config = Config()