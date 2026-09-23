"""Custom exception hierarchy for DiunTap."""


class DiunTapError(Exception):
    """Base class for all DiunTap errors."""


class ConfigError(DiunTapError):
    pass


class AuthDenied(DiunTapError):
    pass


class ValidationError(DiunTapError):
    pass


class DockerError(DiunTapError):
    pass


class UpdateFailed(DockerError):
    pass


class RollbackFailed(DockerError):
    pass


class HealthcheckFailed(DockerError):
    pass


class RemoteError(DiunTapError):
    pass


class TelegramError(DiunTapError):
    pass


class GitHubError(DiunTapError):
    pass
