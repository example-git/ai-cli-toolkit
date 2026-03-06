from ai_cli.addons import traffic_log_addon


def test_identify_chatgpt_wham_apps_as_confirmed_openai_api() -> None:
    provider, is_api = traffic_log_addon._identify("chatgpt.com", "/backend-api/wham/apps")
    assert provider == "openai"
    assert is_api is True


def test_identify_connector_path_with_double_slash() -> None:
    provider, is_api = traffic_log_addon._identify(
        "chatgpt.com",
        "/backend-api//connector?foo=bar",
    )
    assert provider == "openai"
    assert is_api is True


def test_identify_developers_mcp_as_confirmed_openai_api() -> None:
    provider, is_api = traffic_log_addon._identify("developers.openai.com", "/mcp")
    assert provider == "openai"
    assert is_api is True


def test_identify_otlp_metrics_as_non_api_openai_traffic() -> None:
    provider, is_api = traffic_log_addon._identify("ab.chatgpt.com", "/otlp/v1/metrics")
    assert provider == "openai"
    assert is_api is False


def test_do_not_mark_mcp_on_unrelated_host_as_openai_api() -> None:
    provider, is_api = traffic_log_addon._identify("example.com", "/mcp")
    assert provider == ""
    assert is_api is False
