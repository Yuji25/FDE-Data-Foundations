import os
from dataclasses import dataclass

@dataclass
class PipelineConfig:
    log_level: str
    max_retries: int
    retry_backoff_factor: int
    dispatch_api_url: str
    
    @classmethod
    def from_env(cls) -> "PipelineConfig":
        """
        Create a configuration object by reading from environment variables.
        """
        return cls(
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            max_retries=int(os.getenv("MAX_RETRIES", "3")),
            retry_backoff_factor=int(os.getenv("RETRY_BACKOFF_FACTOR", "2")),
            dispatch_api_url=os.getenv("DISPATCH_API_URL", "https://api.flasheats.com/events")
        )
