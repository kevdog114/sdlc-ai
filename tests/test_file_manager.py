import sys
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.file_manager import read_file, write_file, list_dir, delete_file


class TestReadFile:
    def test_read_existing_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('test content')
            f.flush()
            tmp_path = f.name
        try:
            result = read_file(tmp_path)
            assert result['success'] is True
            assert result['content'] == 'test content'
        finally:
            os.unlink(tmp_path)

    def test_read_nonexistent_file(self):
        result = read_file('/nonexistent/path/file.txt')
        assert result['success'] is False
        assert 'error' in result

    def test_read_empty_string(self):
        result = read_file('')
        assert result['success'] is False
        assert 'error' in result

    def test_read_directory_instead_of_file(self):
        result = read_file('/tmp')
        assert result['success'] is False
        assert 'error' in result

    def test_append_event_mocked(self):
        with patch('tools.file_manager.append_event') as mock_append:
            read_file('/nonexistent/file.txt')
            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args[0][0] == 'tool:file_manager'
            assert call_args[0][1]['action'] == 'read'
            assert call_args[0][1]['success'] is False


class TestWriteFile:
    def test_write_new_file(self):
        tmp_path = tempfile.mktemp(suffix='.txt')
        try:
            result = write_file(tmp_path, 'new content')
            assert result['success'] is True
            assert Path(tmp_path).read_text() == 'new content'
        finally:
            if Path(tmp_path).exists():
                os.unlink(tmp_path)

    def test_write_overwrite_existing(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('original')
            f.flush()
            tmp_path = f.name
        try:
            result = write_file(tmp_path, 'overwritten')
            assert result['success'] is True
            assert Path(tmp_path).read_text() == 'overwritten'
        finally:
            os.unlink(tmp_path)

    def test_write_empty_content(self):
        tmp_path = tempfile.mktemp(suffix='.txt')
        try:
            result = write_file(tmp_path, '')
            assert result['success'] is True
            assert Path(tmp_path).read_text() == ''
        finally:
            if Path(tmp_path).exists():
                os.unlink(tmp_path)

    def test_write_creates_parent_dirs(self):
        tmp_dir = tempfile.mkdtemp()
        nested_path = os.path.join(tmp_dir, 'a', 'b', 'c', 'file.txt')
        try:
            result = write_file(nested_path, 'nested')
            assert result['success'] is True
            assert Path(nested_path).read_text() == 'nested'
        finally:
            import shutil
            shutil.rmtree(tmp_dir)

    def test_append_event_mocked(self):
        tmp_path = tempfile.mktemp(suffix='.txt')
        with patch('tools.file_manager.append_event') as mock_append:
            write_file(tmp_path, 'mocked content')
            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args[0][0] == 'tool:file_manager'
            assert call_args[0][1]['action'] == 'write'
            assert call_args[0][1]['success'] is True
        if Path(tmp_path).exists():
            os.unlink(tmp_path)


class TestListDir:
    def test_list_existing_directory(self):
        result = list_dir('/tmp')
        assert result['success'] is True
        assert 'files' in result
        assert isinstance(result['files'], list)

    def test_list_nonexistent_directory(self):
        result = list_dir('/nonexistent/directory/path')
        assert result['success'] is False
        assert 'error' in result

    def test_list_empty_string(self):
        result = list_dir('./nonexistent_empty_string_dir_xyz')
        assert result['success'] is False
        assert 'error' in result

    def test_list_file_instead_of_directory(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('test')
            f.flush()
            tmp_path = f.name
        try:
            result = list_dir(tmp_path)
            assert result['success'] is False
            assert 'error' in result
        finally:
            os.unlink(tmp_path)

    def test_append_event_mocked(self):
        with patch('tools.file_manager.append_event') as mock_append:
            list_dir('/tmp')
            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args[0][0] == 'tool:file_manager'
            assert call_args[0][1]['action'] == 'list'
            assert call_args[0][1]['success'] is True


class TestDeleteFile:
    def test_delete_existing_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('to delete')
            f.flush()
            tmp_path = f.name
        result = delete_file(tmp_path)
        assert result['success'] is True
        assert not Path(tmp_path).exists()

    def test_delete_nonexistent_file(self):
        result = delete_file('/nonexistent/file/to/delete.txt')
        assert result['success'] is False
        assert 'error' in result

    def test_delete_empty_string(self):
        result = delete_file('')
        assert result['success'] is False
        assert 'error' in result

    def test_append_event_mocked(self):
        with patch('tools.file_manager.append_event') as mock_append:
            delete_file('/nonexistent/file.txt')
            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args[0][0] == 'tool:file_manager'
            assert call_args[0][1]['action'] == 'delete'
            assert call_args[0][1]['success'] is False
