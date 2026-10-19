"""A retained Windows handle must not make an exited process look alive."""
from types import SimpleNamespace
from unittest.mock import Mock
import ctypes
import pytest

from studycenter_ice import runtime


def test_signaled_process_handle_has_no_live_identity(monkeypatch):
    record={'pid':100,'started':1,'exe':'pythonw.exe'}
    kernel=SimpleNamespace(OpenProcess=Mock(return_value=123),WaitForSingleObject=Mock(return_value=0),CloseHandle=Mock())
    monkeypatch.setattr(runtime.sys,'platform','win32')
    monkeypatch.setattr(runtime,'_process_api',lambda:kernel)
    read=Mock(return_value=record)
    monkeypatch.setattr(runtime,'_identity_on_handle',read)
    assert runtime.process_identity(record['pid']) is None
    read.assert_not_called()
    kernel.CloseHandle.assert_called_once_with(123)


@pytest.mark.parametrize('after_failure,already_exited',[(0,True),(258,False)])
def test_exit_race_is_distinct_from_failed_live_process_termination(monkeypatch,after_failure,already_exited):
    record={'pid':100,'started':1,'exe':'pythonw.exe'}
    kernel=SimpleNamespace(OpenProcess=Mock(return_value=123),WaitForSingleObject=Mock(side_effect=[258,after_failure]),
                           TerminateProcess=Mock(return_value=False),CloseHandle=Mock())
    monkeypatch.setattr(runtime,'_process_api',lambda:kernel)
    monkeypatch.setattr(runtime,'_identity_on_handle',lambda *args:record)
    monkeypatch.setattr(ctypes,'get_last_error',lambda:5,raising=False)
    monkeypatch.setattr(ctypes,'WinError',lambda code:OSError(code,'synthetic live-process error'),raising=False)
    if already_exited:
        assert runtime.terminate_identity(record) is False
    else:
        with pytest.raises(OSError,match='synthetic live-process error'):
            runtime.terminate_identity(record)
    kernel.CloseHandle.assert_called_once_with(123)


def test_termination_waits_for_same_owned_handle_to_signal(monkeypatch):
    record={'pid':100,'started':1,'exe':'pythonw.exe'}
    kernel=SimpleNamespace(OpenProcess=Mock(return_value=123),WaitForSingleObject=Mock(side_effect=[258,0]),
                           TerminateProcess=Mock(return_value=True),CloseHandle=Mock())
    monkeypatch.setattr(runtime,'_process_api',lambda:kernel)
    monkeypatch.setattr(runtime,'_identity_on_handle',lambda *args:record)
    assert runtime.terminate_identity(record) is True
    kernel.WaitForSingleObject.assert_any_call(123,5000)
    assert kernel.WaitForSingleObject.call_count==2
    kernel.CloseHandle.assert_called_once_with(123)
