"""Tests for backend.errors — exception hierarchy and logging."""
import pytest
from backend.errors import (
    VividlyError, DownloadError, ResolveError, ConversionError,
    TagError, OrganizeError, ValidationError, PathTraversalError,
    SourceError, UnsupportedSourceError, ErrorContext, log_error,
)


class TestVividlyError:
    def test_base_defaults(self):
        e = VividlyError()
        assert e.code == "unknown"
        assert e.recoverable is True
        assert "unexpected" in e.message.lower()

    def test_custom_message(self):
        e = VividlyError("boom")
        assert e.message == "boom"
        assert str(e) == "boom"

    def test_custom_code(self):
        e = VividlyError("fail", code="custom_code")
        assert e.code == "custom_code"

    def test_non_recoverable(self):
        e = VividlyError("fatal", recoverable=False)
        assert e.recoverable is False

    def test_to_dict(self):
        e = DownloadError("download broke", recoverable=True)
        d = e.to_dict()
        assert d["error"] == "download_failed"
        assert d["message"] == "download broke"
        assert d["recoverable"] is True


class TestInheritance:
    def test_resolve_is_download(self):
        assert issubclass(ResolveError, DownloadError)
        assert issubclass(ResolveError, VividlyError)

    def test_conversion_is_vividly(self):
        assert issubclass(ConversionError, VividlyError)

    def test_tag_is_vividly(self):
        assert issubclass(TagError, VividlyError)

    def test_organize_is_vividly(self):
        assert issubclass(OrganizeError, VividlyError)

    def test_path_traversal_is_validation(self):
        assert issubclass(PathTraversalError, ValidationError)
        assert issubclass(PathTraversalError, VividlyError)

    def test_unsupported_source_is_source(self):
        assert issubclass(UnsupportedSourceError, SourceError)
        assert issubclass(UnsupportedSourceError, VividlyError)

    def test_validation_is_non_recoverable(self):
        e = ValidationError()
        assert e.recoverable is False


class TestErrorContext:
    def test_defaults(self):
        ctx = ErrorContext()
        assert ctx.operation == ""
        assert ctx.job_id == 0
        assert ctx.extra == {}

    def test_with_values(self):
        ctx = ErrorContext(operation="download", job_id=42, source_url="https://x")
        assert ctx.operation == "download"
        assert ctx.job_id == 42
        assert ctx.source_url == "https://x"


class TestLogError:
    def test_log_error_runs(self, caplog):
        with caplog.at_level("ERROR"):
            log_error(VividlyError("test err"), ErrorContext(operation="test"))
        assert "test err" in caplog.text
