import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.shell_executor import execute_command


def test_execute_command_success():
    result = execute_command('echo "hello"')
    assert result['exit_code'] == 0
    assert 'hello' in result['stdout']
    assert result['stderr'] == ''


def test_execute_command_failure():
    result = execute_command('exit 1')
    assert result['exit_code'] == 1


def test_execute_command_nonexistent_command():
    result = execute_command('nonexistent_command_xyz_12345')
    assert result['exit_code'] != 0


def test_execute_command_empty_string():
    result = execute_command('')
    assert result['exit_code'] == 0


def test_execute_command_stderr():
    result = execute_command('echo "error" >&2')
    assert result['exit_code'] == 0
    assert 'error' in result['stderr']


def test_execute_command_output_structure():
    result = execute_command('echo "test"')
    assert 'exit_code' in result
    assert 'stdout' in result
    assert 'stderr' in result
    assert isinstance(result['exit_code'], int)
    assert isinstance(result['stdout'], str)
    assert isinstance(result['stderr'], str)


def test_append_event_mocked():
    with patch('tools.shell_executor.append_event') as mock_append:
        execute_command('echo "mocked"')
        mock_append.assert_called_once()
        call_args = mock_append.call_args
        assert call_args[0][0] == 'tool:shell_executor'
        assert call_args[0][1]['command'] == 'echo "mocked"'
        assert call_args[0][1]['success'] is True
