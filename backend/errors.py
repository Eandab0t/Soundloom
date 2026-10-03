"""Custom exception hierarchy for Soundloom.

All exceptions derive from VividlyError so they can be caught uniformly
and converted to structured API responses or logged with context.
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("soundloom.errors")


class VividlyError(Exception):
    """Base exception for all application errors."""
    code: str = "unknown"
    message: str = "An unexpected error occurred"
    default_recoverable: bool = True

    def __init__(self, message: str = "", *, code: str = "", recoverable: bool | None = None):
        self.message = message or self.__class__.message
        if code:
            self.code = code
        self.recoverable = self.__class__.default_recoverable if recoverable is None else recoverable
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "error": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
        }


class DownloadError(VividlyError):
    code = "download_failed"
    message = "Download failed"


class ResolveError(DownloadError):
    code = "resolve_failed"
    message = "Could not resolve URL metadata"


class ConversionError(VividlyError):
    code = "conversion_failed"
    message = "Audio conversion failed"


class TagError(VividlyError):
    code = "tag_failed"
    message = "Failed to read or write audio tags"


class OrganizeError(VividlyError):
    code = "organize_failed"
    message = "Failed to organize file into library"


class LibraryError(VividlyError):
    code = "library_error"
    message = "Library operation failed"


class ScanError(LibraryError):
    code = "scan_failed"
    message = "Library scan failed"


class ValidationError(VividlyError):
    code = "validation_failed"
    message = "Input validation failed"
    default_recoverable = False


class PathTraversalError(ValidationError):
    code = "path_traversal"
    message = "Path traversal detected"
    default_recoverable = False


class ConfigError(VividlyError):
    code = "config_error"
    message = "Configuration error"
    default_recoverable = False


class SourceError(VividlyError):
    code = "source_error"
    message = "Source provider error"


class UnsupportedSourceError(SourceError):
    code = "unsupported_source"
    message = "Unsupported source URL"
    recoverable = False


@dataclass
class ErrorContext:
    """Structured error context for logging and reporting."""
    operation: str = ""
    job_id: int = 0
    file_path: str = ""
    source_url: str = ""
    attempt: int = 0
    max_attempts: int = 0
    extra: dict = field(default_factory=dict)


def log_error(error: Exception, ctx: ErrorContext = None, level: int = logging.ERROR):
    """Log an error with structured context."""
    extra = {
        "error_type": type(error).__name__,
        "error_code": getattr(error, "code", "unknown"),
    }
    if ctx:
        extra["operation"] = ctx.operation
        if ctx.job_id:
            extra["job_id"] = ctx.job_id
        if ctx.file_path:
            extra["file_path"] = ctx.file_path
        if ctx.source_url:
            extra["source_url"] = ctx.source_url
        if ctx.attempt:
            extra["attempt"] = ctx.attempt
        extra.update(ctx.extra)

    logger.log(level, str(error), extra=extra, exc_info=True)
