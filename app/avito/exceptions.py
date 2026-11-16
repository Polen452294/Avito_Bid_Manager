class AvitoError(Exception):
    pass


class CapabilityBlocked(AvitoError):
    pass


class UncertainWrite(AvitoError):
    pass


class AccessDenied(AvitoError):
    pass


class AuthenticationError(AccessDenied):
    pass


class ContractUnverified(CapabilityBlocked):
    pass


class ModelUnavailable(CapabilityBlocked):
    pass


class ResourceNotFound(CapabilityBlocked):
    pass


class InvalidRequest(AvitoError):
    pass


class InvalidResponse(AvitoError):
    pass


class RateLimited(AvitoError):
    def __init__(self, message="Request rate limited; retry later", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class TemporaryUnavailable(AvitoError):
    pass


class ReadTimeout(TemporaryUnavailable):
    pass


class VerificationFailed(UncertainWrite):
    pass
