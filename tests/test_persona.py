from veil import persona as P
from veil.geo import ExitInfo, languages_for_country


def test_same_seed_same_device():
	assert P.generate(seed=42) == P.generate(seed=42)


def test_different_seeds_vary():
	gpus = {P.generate(seed=s).gpu_id for s in range(200)}
	seeds = {P.generate(seed=s).canvas_seed for s in range(200)}
	assert len(gpus) > 5 and len(seeds) == 200


def test_device_is_coherent_with_its_machine_class():
	data = P.device_data()
	for s in range(500):
		p = P.generate(seed=s)
		machine = data['machine_classes'][p.machine_class]
		assert p.hw_concurrency in [v for v, _ in machine['hw_concurrency']]
		assert p.device_memory in [v for v, _ in machine['device_memory']]
		assert [p.screen_width, p.screen_height] in [v for v, _ in machine['screens']]
		gpu = next(g for g in data['gpus'] if g['id'] == p.gpu_id)
		assert gpu['class'] == p.machine_class
		assert p.webgl_renderer == gpu['renderer'] and p.webgpu_vendor == gpu['webgpu_vendor']
		assert p.device_memory in (0.25, 0.5, 1, 2, 4, 8)  # the only values Chrome can report


def test_geo_overrides_only_timezone_and_language():
	base = P.generate(seed=7)
	geo = base.with_geo(ExitInfo(ip='1.2.3.4', country='IN', timezone='Asia/Kolkata'))
	assert geo.timezone == 'Asia/Kolkata' and geo.languages == 'en-IN,en,hi'
	assert (geo.gpu_id, geo.canvas_seed, geo.screen_width) == (base.gpu_id, base.canvas_seed, base.screen_width)


def test_flags_and_env():
	p = P.generate(seed=1, exit_info=ExitInfo(ip='x', country='DE', timezone='Europe/Berlin'))
	flags = p.flags()
	assert '--uxr-timezone=Europe/Berlin' in flags
	assert '--accept-lang=de-DE,de,en' in flags
	assert not any(f.startswith('--user-agent') for f in flags)  # desyncs UA vs Client Hints
	# WebGPU overrides must come after nothing else re-sets them, and window must fit the screen
	w, h = p.window_size
	assert w <= p.screen_width and h < p.screen_height
	assert p.env()['TILION_NO_DEFAULTS'] == '1' and p.env()['TZ'] == 'Europe/Berlin'


def test_roundtrip_dict():
	p = P.generate(seed=3)
	assert P.Persona.from_dict(p.to_dict()) == p


def test_unknown_country_falls_back_to_en_us():
	assert languages_for_country('ZZ') == 'en-US,en'
	assert languages_for_country(None) == 'en-US,en'
