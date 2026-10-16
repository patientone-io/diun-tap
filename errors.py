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


class ImageTagMismatch(ValueError):
    """The repository exists in the configuration, but its tag is pinned elsewhere."""

    def __init__(self, configured: list[str]):
        self.configured = configured
        super().__init__("Notification tag differs from configured image reference")
