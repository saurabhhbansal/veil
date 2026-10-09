import pytest

from veil.proxy import GatewayProxy, Proxy, ProxyPool


@pytest.mark.parametrize(
	'raw, expected',
	[
		('http://u:p@1.2.3.4:8080', Proxy('http', '1.2.3.4', 8080, 'u', 'p')),
		('1.2.3.4:8080', Proxy('http', '1.2.3.4', 8080)),
		('gw.example.com:823:user-zone-res:s3cr3t', Proxy('http', 'gw.example.com', 823, 'user-zone-res', 's3cr3t')),
		('u:p@host:1', Proxy('http', 'host', 1, 'u', 'p')),
		('socks5://10.0.0.1:1080', Proxy('socks5', '10.0.0.1', 1080)),
		('http://user%40mail:p%3Ass@h:9', Proxy('http', 'h', 9, 'user@mail', 'p:ss')),
		('gw.example.com:823:user:pa:ss', Proxy('http', 'gw.example.com', 823, 'user', 'pa:ss')),
		('http://[2001:db8::1]:8080', Proxy('http', '2001:db8::1', 8080)),
	],
)
def test_parse(raw, expected):
	assert Proxy.parse(raw) == expected


def test_server_never_contains_credentials_but_url_roundtrips():
	p = Proxy.parse('http://us%3Aer:p%40ss@h:1')
	assert '@' not in p.server
	assert Proxy.parse(p.url) == p
	assert 'p@ss' not in p.redacted and p.username == 'us:er'


@pytest.mark.parametrize('raw', ['', 'ftp://h:1', 'justahost', 'socks5://u:p@h:1080'])
def test_parse_rejects(raw):
	with pytest.raises(ValueError):
		Proxy.parse(raw)


def test_round_robin_skips_proxies_on_cooldown():
	a, b, c = (Proxy.parse(f'10.0.0.{i}:80') for i in (1, 2, 3))
	pool = ProxyPool([a, b, c])
	assert [pool.acquire() for _ in range(3)] == [a, b, c]
	pool.report(b, ok=False, blocked=True)
	assert b not in pool.available()
	assert b not in {pool.acquire() for _ in range(6)}


def test_blocks_back_off_harder_than_errors():
	a, b = Proxy.parse('10.0.0.1:80'), Proxy.parse('10.0.0.2:80')
	pool = ProxyPool([a, b], base_cooldown=10)
	pool.report(a, ok=False)
	pool.report(b, ok=False, blocked=True)
	st = pool._stats
	assert st[b].cooldown_until > st[a].cooldown_until


def test_all_cooling_down_still_returns_soonest():
	a, b = Proxy.parse('10.0.0.1:80'), Proxy.parse('10.0.0.2:80')
	pool = ProxyPool([a, b])
	pool.report(a, ok=False)
	pool.report(b, ok=False, blocked=True)
	assert pool.acquire() == a


def test_success_resets_consecutive_failures():
	a = Proxy.parse('10.0.0.1:80')
	pool = ProxyPool([a])
	pool.report(a, ok=False)
	pool.report(a, ok=True)
	assert pool._stats[a].consecutive_failures == 0


def test_gateway_session_token_gives_sticky_distinct_sessions():
	gw = GatewayProxy('http://customer-session-{session}:pw@gw.example.com:7777')
	assert gw.sticky
	assert gw.acquire('abc').username == 'customer-session-abc'
	assert gw.acquire().username != gw.acquire().username


def test_pool_from_file_skips_comments(tmp_path):
	f = tmp_path / 'proxies.txt'
	f.write_text('# residential\n1.1.1.1:80:u:p\n\n2.2.2.2:8080\n')
	assert len(ProxyPool.from_file(f).proxies) == 2


def test_ipv6_proxy_keeps_its_brackets():
	p = Proxy.parse('http://u:p@[2001:db8::1]:8080')
	assert p.server == 'http://[2001:db8::1]:8080'
	assert Proxy.parse(p.url) == p
