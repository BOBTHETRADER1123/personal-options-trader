"""A diagnostic keeps every frame and exception type while excluding secrets."""
from app.master_diagnostics import safe_master_traceback


def test_traceback_preserves_nested_frames_and_never_logs_messages_or_locals():
    secret='synthetic-private-token-MPIN'
    try:
        try:raise ValueError(secret)
        except ValueError as cause:raise RuntimeError('Authorization: '+secret) from cause
    except RuntimeError as error:result=safe_master_traceback(error)
    assert 'ValueError' in result and 'RuntimeError' in result
    assert result.count('test_traceback_preserves_nested_frames')==2
    assert secret not in result and 'Authorization:' not in result
