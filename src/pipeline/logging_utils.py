import logging
import os
import sys

def setup_logger(name: str, log_level: str = "INFO", log_file: str = None) -> logging.Logger:
    """
    Configure standard Python logging to both console and a specified log file.
    
    Args:
        name: Name of the logger
        log_level: Desired log level (e.g., INFO, DEBUG)
        log_file: Optional file path to output logs to
        
    Returns:
        Configured logging.Logger instance
    """
    logger = logging.getLogger(name)
    
    # Avoid adding duplicate handlers if the logger is already configured
    if logger.hasHandlers():
        return logger
        
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File Handler
    if log_file:
        # Ensure log directory exists
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
    return logger
