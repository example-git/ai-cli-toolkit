from ai_cli.addons import gemini_addon


def test_path_matches_multi_target_case_insensitive() -> None:
    target = "/v1beta/models,/v1alpha/models,/v1/models,/v1internal:"
    assert gemini_addon._path_matches_target("/v1internal:generateContent", target) is True
    assert (
        gemini_addon._path_matches_target("/V1INTERNAL:streamGenerateContent?alt=sse", target)
        is True
    )
    assert gemini_addon._path_matches_target("/v1/messages", target) is False


def test_generate_content_detection_is_case_insensitive() -> None:
    assert (
        gemini_addon._is_generate_content_path("/v1internal:streamGenerateContent?alt=sse") is True
    )
    assert gemini_addon._is_generate_content_path("/v1internal:generateContent") is True
    assert gemini_addon._is_generate_content_path("/v1internal:recordCodeAssistMetrics") is False


def test_internal_request_envelope_detection() -> None:
    assert gemini_addon._uses_internal_request_envelope("/v1internal:generateContent") is True
    assert (
        gemini_addon._uses_internal_request_envelope("/v1beta/models/gemini:generateContent")
        is False
    )
