"""Request errors shared without importing either device backend."""


class APIError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
