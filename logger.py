import logging

# Configure logger
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG for full details, INFO for less
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()  # logs will appear in GitHub Actions logs
    ]
)

logger = logging.getLogger("TradingBot")
