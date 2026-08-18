"""Tests for tools/file_manager.py.

File operations are confined to the allowed roots (bootstrap.BASE_DIR,
USER_PROJECTS_ROOT, SDLCAI_FILE_ROOTS) — conftest points BASE_DIR at a
per-test tmp dir, so tests operate inside it. The escape/denial cases live in
tests/test_security.py::TestFileConfinement.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap
from tools.file_manager import read_file, write_file, list_dir, delete_file


def _p(*parts) -> str:
    """A path inside the hermetic project root."""
    return str(Path(bootstrap.BASE_DIR).joinpath(*parts))


class TestReadFile:
    def test_read_existing_file(self):
        target = Path(_p("sample.txt"))
        target.write_text("test content", encoding="utf-8")
        result = read_file(str(target))
        assert result['success'] is True
        assert result['content'] == 'test content'

    def test_read_nonexistent_file(self):
        result = read_file(_p("nope", "file.txt"))
        assert result['success'] is False
        assert 'error' in result

    def test_read_empty_string(self):
        result = read_file('')
        assert result['success'] is False
        assert 'error' in result

    def test_read_directory_instead_of_file(self):
        result = read_file(_p("state"))
        assert result['success'] is False
        assert 'error' in result

    def test_append_event_mocked(self):
        with patch('tools.file_manager.append_event') as mock_append:
            read_file(_p("missing.txt"))
            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args[0][0] == 'tool:file_manager'
            assert call_args[0][1]['action'] == 'read'
            assert call_args[0][1]['success'] is False


class TestWriteFile:
    def test_write_new_file(self):
        target = _p("new.txt")
        result = write_file(target, 'new content')
        assert result['success'] is True
        assert Path(target).read_text() == 'new content'

    def test_write_overwrite_existing(self):
        target = Path(_p("existing.txt"))
        target.write_text("original", encoding="utf-8")
        result = write_file(str(target), 'overwritten')
        assert result['success'] is True
        assert target.read_text() == 'overwritten'

    def test_write_empty_content(self):
        target = _p("empty.txt")
        result = write_file(target, '')
        assert result['success'] is True
        assert Path(target).read_text() == ''

    def test_write_creates_parent_dirs(self):
        nested = _p("a", "b", "c", "file.txt")
        result = write_file(nested, 'nested')
        assert result['success'] is True
        assert Path(nested).read_text() == 'nested'

    def test_append_event_mocked(self):
        with patch('tools.file_manager.append_event') as mock_append:
            write_file(_p("evented.txt"), 'mocked content')
            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args[0][0] == 'tool:file_manager'
            assert call_args[0][1]['action'] == 'write'
            assert call_args[0][1]['success'] is True


class TestListDir:
    def test_list_existing_directory(self):
        Path(_p("stuff")).mkdir()
        Path(_p("stuff", "one.txt")).write_text("1", encoding="utf-8")
        result = list_dir(_p("stuff"))
        assert result['success'] is True
        assert result['files'] == ['one.txt']

    def test_list_nonexistent_directory(self):
        result = list_dir(_p("no", "such", "dir"))
        assert result['success'] is False
        assert 'error' in result

    def test_list_file_instead_of_directory(self):
        target = Path(_p("notadir.txt"))
        target.write_text("x", encoding="utf-8")
        result = list_dir(str(target))
        assert result['success'] is False
        assert 'error' in result

    def test_append_event_mocked(self):
        with patch('tools.file_manager.append_event') as mock_append:
            list_dir(str(bootstrap.BASE_DIR))
            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args[0][0] == 'tool:file_manager'
            assert call_args[0][1]['action'] == 'list'
            assert call_args[0][1]['success'] is True


class TestDeleteFile:
    def test_delete_existing_file(self):
        target = Path(_p("todelete.txt"))
        target.write_text("to delete", encoding="utf-8")
        result = delete_file(str(target))
        assert result['success'] is True
        assert not target.exists()

    def test_delete_nonexistent_file(self):
        result = delete_file(_p("ghost.txt"))
        assert result['success'] is False
        assert 'error' in result

    def test_delete_empty_string(self):
        result = delete_file('')
        assert result['success'] is False
        assert 'error' in result

    def test_append_event_mocked(self):
        with patch('tools.file_manager.append_event') as mock_append:
            delete_file(_p("ghost2.txt"))
            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args[0][0] == 'tool:file_manager'
            assert call_args[0][1]['action'] == 'delete'
            assert call_args[0][1]['success'] is False
