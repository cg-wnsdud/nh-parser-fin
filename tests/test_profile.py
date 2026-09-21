from nh_parser_fin import config


def test_paddlex_options_are_owned_by_server_yaml():
    profile = config.Profile()

    assert profile.request_payload == {"fileType": 1}
    assert profile.manifest()["paddlex_options_source"] == "server_pipeline_yaml"
