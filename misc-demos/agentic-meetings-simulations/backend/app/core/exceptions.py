class NexusBaseException(Exception):
    """Base exception for all Nexus demo apps"""
    def __init__(self, message: str, code: str):
        self.message = message
        self.code = code
        super().__init__(self.message)
