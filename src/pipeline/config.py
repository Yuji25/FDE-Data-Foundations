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
        """Create a configuration object from process environment variables."""
        return cls(
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            max_retries=int(os.getenv("MAX_RETRIES", "3")),
            retry_backoff_factor=int(os.getenv("RETRY_BACKOFF_FACTOR", "2")),
            # Base URL only; Phase 2 appends /health or /dispatch/orders.
            dispatch_api_url=os.getenv(
                "DISPATCH_API_URL",
                "http://127.0.0.1:8000",
            ),
        )
