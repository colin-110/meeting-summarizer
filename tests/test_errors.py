from backend.app.utils.errors import clean_provider_message


class _FakeProviderError(Exception):
    def __init__(self, message, body=None):
        super().__init__(message)
        self.body = body


def test_extracts_message_from_provider_body():
    exc = _FakeProviderError(
        "Error code: 400 - {'error': {'message': 'could not process file', 'type': 'invalid_request_error'}}",
        body={"error": {"message": "could not process file", "type": "invalid_request_error"}},
    )
    assert clean_provider_message(exc) == "could not process file"


def test_falls_back_to_str_when_body_missing():
    exc = RuntimeError("plain old error")
    assert clean_provider_message(exc) == "plain old error"


def test_falls_back_to_str_when_body_is_not_a_dict():
    exc = _FakeProviderError("weird error", body="not a dict")
    assert clean_provider_message(exc) == str(exc)


def test_falls_back_to_str_when_error_message_missing():
    exc = _FakeProviderError("weird error", body={"error": {"type": "invalid_request_error"}})
    assert clean_provider_message(exc) == str(exc)
