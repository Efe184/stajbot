import ssl

from stajbot.collectors import base


def test_make_http_client_uses_system_trust_store():
    with base.make_http_client() as client:
        ctx = base._ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert type(ctx).__module__.startswith("truststore")
        assert client.headers["User-Agent"]


def test_make_http_client_respects_explicit_verify():
    with base.make_http_client(verify=False) as client:
        assert client is not None
