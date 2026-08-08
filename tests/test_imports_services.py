"""Tests for imports service."""

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, UploadFile

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import imports as imports_service
from app.services import settings_spec

# =============================================================================
# Helper Function Tests
# =============================================================================


class TestImportsIntSetting:
    """`_imports_int_setting` resolves through the spec, not a caller default.

    It used to take a `default` argument and query `DomainSetting` directly, so
    `imports.py` restated defaults the specs already declared and skipped the
    specs' bounds. These tests were rewritten with that contract: the key must
    be registered, the row wins when present, and everything else falls back to
    the SPEC's default rather than one the call site invented.
    """

    def test_absent_row_resolves_the_spec_default(self, db_session):
        spec = settings_spec.get_spec(SettingDomain.imports, "max_rows")
        assert spec is not None
        assert imports_service._imports_int_setting(db_session, "max_rows") == (
            spec.default
        )

    def test_a_stored_row_wins(self, db_session):
        db_session.add(
            DomainSetting(
                domain=SettingDomain.imports,
                key="max_rows",
                value_type=SettingValueType.integer,
                value_text="500",
                is_active=True,
            )
        )
        db_session.commit()

        assert imports_service._imports_int_setting(db_session, "max_rows") == 500

    def test_an_unparseable_row_falls_back_to_the_spec_default(self, db_session):
        db_session.add(
            DomainSetting(
                domain=SettingDomain.imports,
                key="max_rows",
                value_type=SettingValueType.integer,
                value_text="not-a-number",
                is_active=True,
            )
        )
        db_session.commit()

        spec = settings_spec.get_spec(SettingDomain.imports, "max_rows")
        assert spec is not None
        assert imports_service._imports_int_setting(db_session, "max_rows") == (
            spec.default
        )

    def test_an_inactive_row_is_ignored(self, db_session):
        db_session.add(
            DomainSetting(
                domain=SettingDomain.imports,
                key="max_rows",
                value_type=SettingValueType.integer,
                value_text="999",
                is_active=False,
            )
        )
        db_session.commit()

        spec = settings_spec.get_spec(SettingDomain.imports, "max_rows")
        assert spec is not None
        assert imports_service._imports_int_setting(db_session, "max_rows") == (
            spec.default
        )

    def test_a_row_below_the_spec_floor_falls_back(self, db_session):
        """The bound is the spec's, and it now applies on the READ path too."""

        db_session.add(
            DomainSetting(
                domain=SettingDomain.imports,
                key="max_file_bytes",
                value_type=SettingValueType.integer,
                value_text="512",
                is_active=True,
            )
        )
        db_session.commit()

        spec = settings_spec.get_spec(SettingDomain.imports, "max_file_bytes")
        assert spec is not None and spec.min_value == 1024
        assert imports_service._imports_int_setting(db_session, "max_file_bytes") == (
            spec.default
        )

    def test_an_unregistered_key_is_a_programming_error(self, db_session):
        """The old signature accepted any key with a caller default.

        Nothing declared such a key's type or bounds, so an unregistered key
        silently became a setting. It is now loud.
        """

        with pytest.raises(RuntimeError):
            imports_service._imports_int_setting(db_session, "nonexistent")


class TestImportSubscriberCustomFieldsFromCsv:
    """Tests for import_subscriber_custom_fields_from_csv function."""

    def test_returns_counts(self, db_session):
        """Test returns created count and errors."""
        csv_content = "subscriber_id,field_key,field_value\n"

        with patch.object(imports_service, "load_csv_content") as mock_load:
            mock_load.return_value = ([], [])

            created, errors = imports_service.import_subscriber_custom_fields_from_csv(
                db_session, csv_content
            )

            assert created == 0
            assert errors == []

    def test_handles_row_errors(self, db_session):
        """Test captures row parsing errors."""
        csv_content = "subscriber_id,field_key,field_value\n"

        mock_error = MagicMock()
        mock_error.index = 1
        mock_error.detail = "Invalid row"

        with patch.object(imports_service, "load_csv_content") as mock_load:
            mock_load.return_value = ([], [mock_error])

            created, errors = imports_service.import_subscriber_custom_fields_from_csv(
                db_session, csv_content
            )

            assert created == 0
            assert len(errors) == 1
            assert errors[0]["index"] == 1
            assert errors[0]["detail"] == "Invalid row"


class TestImportSubscriberCustomFieldsUpload:
    """Tests for import_subscriber_custom_fields_upload function."""

    def test_rejects_non_csv_file(self, db_session):
        """Test rejects non-CSV files."""
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.xlsx"

        with pytest.raises(HTTPException) as exc_info:
            imports_service.import_subscriber_custom_fields_upload(
                db_session, mock_file
            )

        assert exc_info.value.status_code == 400
        assert "CSV file required" in exc_info.value.detail

    def test_rejects_empty_filename(self, db_session):
        """Test rejects empty filename."""
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = None

        with pytest.raises(HTTPException) as exc_info:
            imports_service.import_subscriber_custom_fields_upload(
                db_session, mock_file
            )

        assert exc_info.value.status_code == 400

    def test_rejects_large_file(self, db_session):
        """Test rejects file exceeding max size."""
        large_content = b"x" * (6 * 1024 * 1024)  # 6MB
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.csv"
        mock_file.file = BytesIO(large_content)

        with pytest.raises(HTTPException) as exc_info:
            imports_service.import_subscriber_custom_fields_upload(
                db_session, mock_file
            )

        assert exc_info.value.status_code == 413
        assert "too large" in exc_info.value.detail

    def test_rejects_invalid_utf8(self, db_session):
        """Test rejects invalid UTF-8 content."""
        invalid_content = b"\xff\xfe"  # Invalid UTF-8
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.csv"
        mock_file.file = BytesIO(invalid_content)

        with pytest.raises(HTTPException) as exc_info:
            imports_service.import_subscriber_custom_fields_upload(
                db_session, mock_file
            )

        assert exc_info.value.status_code == 400
        assert "Invalid UTF-8" in exc_info.value.detail

    def test_successful_upload(self, db_session):
        """Test successful CSV upload."""
        csv_content = b"subscriber_id,field_key,field_value\n"
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.csv"
        mock_file.file = BytesIO(csv_content)

        with patch.object(
            imports_service, "import_subscriber_custom_fields_from_csv"
        ) as mock_import:
            mock_import.return_value = (5, [])

            result = imports_service.import_subscriber_custom_fields_upload(
                db_session, mock_file
            )

            assert result["created"] == 5
            assert result["errors"] == []
            assert result["error_count"] == 0

    def test_raises_on_row_limit_exceeded(self, db_session):
        """Test raises when row limit exceeded."""
        csv_content = b"subscriber_id,field_key,field_value\n"
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.csv"
        mock_file.file = BytesIO(csv_content)

        with patch.object(
            imports_service, "import_subscriber_custom_fields_from_csv"
        ) as mock_import:
            mock_import.return_value = (0, [{"detail": "Row limit exceeded"}])

            with pytest.raises(HTTPException) as exc_info:
                imports_service.import_subscriber_custom_fields_upload(
                    db_session, mock_file
                )

            assert exc_info.value.status_code == 400
            assert "row limit exceeded" in exc_info.value.detail.lower()
