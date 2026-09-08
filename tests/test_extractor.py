"""Extractor unit tests: pure function, no network."""
from youtube_game_detector.extractor import extract_game

PHASMO_HTML = """<div class="ytVideoAttributeViewModelMetadata" role="link">
  <div class="ytVideoAttributeViewModelTextContainer">
    <h1 class="ytVideoAttributeViewModelTitle">Phasmophobia</h1>
    <h4 class="ytVideoAttributeViewModelSubtitle"><span>2020</span></h4>
  </div>
</div>"""

FULL_PAGE_MINECRAFT = """<html><head>
<title>Minecraft Funny Moments Ep 42 - YouTube</title>
<meta name="title" content="Minecraft Funny Moments Ep 42">
</head><body>
<span id="channel">Dream</span>
<div id="info">Gaming | 1,234,567 views | Jan 1, 2024</div>
<div class="ytVideoAttributeViewModelMetadata" role="link">
  <div class="ytVideoAttributeViewModelTextContainer">
    <h1 class="ytVideoAttributeViewModelTitle">Minecraft</h1>
    <h4 class="ytVideoAttributeViewModelSubtitle"><span>2011</span></h4>
  </div>
</div>
</body></html>"""


def test_extract_phasmophobia_from_known_markup():
    assert extract_game(PHASMO_HTML) == "Phasmophobia"


def test_extract_game_not_confused_by_title_channel_category():
    # Must return the game, not the video title / channel / "Gaming".
    game = extract_game(FULL_PAGE_MINECRAFT)
    assert game == "Minecraft"
    assert game != "Minecraft Funny Moments Ep 42"
    assert game != "Dream"
    assert game != "Gaming"


def test_missing_game_metadata_returns_none():
    html = (
        "<html><head><title>Some vlog - YouTube</title></head>"
        "<body><span>MyChannel</span><div>Gaming</div></body></html>"
    )
    assert extract_game(html) is None


def test_category_gaming_alone_is_not_a_game():
    html = (
        '<h1 class="ytVideoAttributeViewModelTitle">Gaming</h1>'
        "<div>gaming category page</div>"
    )
    assert extract_game(html) is None


def test_year_subtitle_not_returned_as_game():
    assert extract_game("<h1>2020</h1>") is None
    assert extract_game("") is None
    assert extract_game(None) is None  # type: ignore[arg-type]
    assert extract_game(123) is None  # type: ignore[arg-type]


def test_attribute_json_fallback():
    html = '{"videoAttributeViewModel":{"title":"Valorant","subtitle":"2020"}}'
    assert extract_game(html) == "Valorant"


def test_game_title_key_fallback():
    assert extract_game('{"gameTitle":"Fortnite"}') == "Fortnite"
    assert extract_game('{"videoGame":"Counter-Strike 2"}') == "Counter-Strike 2"


def test_ldjson_fallback():
    html = (
        '<script type="application/ld+json">'
        '{"@context":"http://schema.org","@type":"VideoObject",'
        '"name":"Epic plays","videoGame":"Minecraft"}'
        "</script>"
    )
    assert extract_game(html) == "Minecraft"


def test_html_entities_unescaped():
    html = (
        '<h1 class="ytVideoAttributeViewModelTitle">'
        "Grand Theft Auto V &amp; Online</h1>"
    )
    assert extract_game(html) == "Grand Theft Auto V & Online"


def test_known_games():
    for name in (
        "Phasmophobia",
        "Minecraft",
        "Valorant",
        "Fortnite",
        "Grand Theft Auto V",
        "Counter-Strike 2",
    ):
        html = f'<h1 class="ytVideoAttributeViewModelTitle">{name}</h1>'
        assert extract_game(html) == name
