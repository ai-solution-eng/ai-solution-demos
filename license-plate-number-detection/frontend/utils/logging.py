import logging

logger = logging.getLogger(__name__)

consoleHandler = logging.StreamHandler()

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:   %(asctime)s   %(message)s",
    handlers=[
        consoleHandler
    ]
)