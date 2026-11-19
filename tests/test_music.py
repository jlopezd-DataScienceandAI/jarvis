"""The Spotify tool, with the command-line program replaced by a scripted fake."""

from __future__ import annotations

import json

import pytest

from jarvis.brain.tools import ToolError, music, registry

BRIGHTSIDE = {"uri": "spotify:track:bright", "name": "Mr. Brightside", "artists": ["The Killers"]}
BRIGHTSIDE_LIVE = {"uri": "spotify:track:live", "name": "Mr. Brightside - Live", "artists": ["The Killers"]}
ESTOPA = {"uri": "spotify:artist:estopa", "name": "Estopa"}
CAMARON = {"uri": "spotify:track:camaron", "name": "Como Camarón", "artists": ["Estopa"]}
ABBEY = {"uri": "spotify:album:abbey", "name": "Abbey Road", "artists": ["The Beatles"]}
FOCUS = {"uri": "spotify:playlist:focus", "name": "Concentración profunda", "author": "Spotify"}
MY_SOUNDTRACKS = {"uri": "spotify:playlist:mine", "name": "Bandas Sonoras", "type": "playlist", "artists": []}
MY_UNNAMED = {"uri": "spotify:playlist:blank", "name": "", "type": "playlist", "artists": []}


class FakeSpotify:
    """Stands in for spotify_cli.exe: answers the commands the tool sends and records them."""

    def __init__(self, running=True, logged_in=True, search=None, library=(), plays=True, login_after=0):
        self.running = running
        self.logged_in = logged_in
        self.login_after = login_after  # status calls answered "Logged in: No" right after opening
        self.search = search or {}
        self.library = list(library)
        self.plays = plays  # False: 'play' is accepted but nothing starts (unplayable content)
        self.current: dict | None = None
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args, timeout=25.0):
        self.calls.append(args)
        command = args[0]
        if command == "open":
            self.running = True
            return 0, ""
        if not self.running:
            return 1, "Spotify desktop client is not running"
        if command == "status":
            signed_in = self.logged_in and self.login_after <= 0
            self.login_after -= 1
            return 0, f"Spotify desktop client is running\nLogged in: {'Yes' if signed_in else 'No'}"
        if command == "search":
            return 0, json.dumps(self.search)
        if command == "library":
            return 0, json.dumps({"items": self.library})
        if command == "play":
            if self.plays:
                self.current = {"uri": args[1] if args[1].startswith("spotify:track:") else "spotify:track:inside",
                                "description": f"reproduciendo {args[1]}", "is_playing": True}
            return 0, ""
        if command == "queue":
            return 0, "Added to queue"  # plain text, as the real tool answers
        if command == "now-playing":
            return 0, json.dumps({"currently_playing": self.current} if self.current else {})
        raise AssertionError(f"unexpected command: {args}")

    def sent(self, command):
        return [call for call in self.calls if call[0] == command]


@pytest.fixture
def spotify(monkeypatch):
    def install(**kwargs):
        fake = FakeSpotify(**kwargs)
        monkeypatch.setattr(music, "_run", fake)
        monkeypatch.setattr(music.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(music, "_PLAY_CONFIRM_S", 0.05)
        monkeypatch.setattr(music, "_START_TIMEOUT_S", 0.5)
        monkeypatch.setattr(music, "_SESSION_GRACE_S", 0.05)
        return fake

    return install


def test_plays_the_requested_song_and_reports_what_is_on(spotify):
    fake = spotify(search={"tracks": [BRIGHTSIDE, BRIGHTSIDE_LIVE]})
    result = music.play_music("mr brightside the killers", "track")
    assert fake.sent("play") == [("play", "spotify:track:bright")]
    assert result.startswith("Sonando: reproduciendo spotify:track:bright")
    assert "Otras coincidencias: Mr. Brightside - Live — The Killers" in result  # lets the model correct the pick
    assert fake.sent("search")[0][:5] == ("search", "mr brightside the killers", "--limit", "5", "--type")


def test_opens_spotify_first_when_it_is_closed(spotify):
    fake = spotify(running=False, search={"tracks": [BRIGHTSIDE]})
    music.play_music("mr brightside", "track")
    commands = [call[0] for call in fake.calls]
    assert commands.index("open") < commands.index("search") < commands.index("play")


def test_waits_for_the_session_a_freshly_opened_app_has_not_restored_yet(spotify):
    fake = spotify(running=False, login_after=3, search={"tracks": [BRIGHTSIDE]})
    assert music.play_music("mr brightside", "track").startswith("Sonando")
    assert len(fake.sent("status")) >= 4


def test_needs_a_signed_in_app(spotify):
    spotify(logged_in=False)
    with pytest.raises(ToolError, match="sin sesión iniciada"):
        music.play_music("algo")


def test_a_song_that_does_not_exist_is_reported(spotify):
    fake = spotify(search={"tracks": []})
    with pytest.raises(ToolError, match="No encuentro «xyz»"):
        music.play_music("xyz", "track")
    assert fake.sent("play") == []


def test_success_is_only_claimed_if_it_really_started(spotify):
    spotify(search={"tracks": [BRIGHTSIDE]}, plays=False)
    with pytest.raises(ToolError, match="no ha empezado a sonar"):
        music.play_music("mr brightside", "track")


def test_auto_treats_an_exact_artist_name_as_the_artist(spotify):
    fake = spotify(search={"artists": [ESTOPA], "tracks": [CAMARON]})
    result = music.play_music("Estopa")
    assert fake.sent("play")[0][1] == "spotify:artist:estopa"
    assert "(artista: Estopa)" in result


def test_auto_prefers_the_song_when_the_query_is_not_just_the_artist(spotify):
    fake = spotify(search={"artists": [ESTOPA], "tracks": [CAMARON]})
    music.play_music("como camarón de estopa")
    assert fake.sent("play")[0][1] == "spotify:track:camaron"


def test_the_users_own_playlists_win_over_the_catalogue(spotify):
    fake = spotify(search={"playlists": [FOCUS]}, library=[MY_UNNAMED, MY_SOUNDTRACKS])
    music.play_music("bandas sonoras", "playlist")
    assert fake.sent("play")[0][1] == "spotify:playlist:mine"

    fake = spotify(search={"playlists": [FOCUS]}, library=[MY_UNNAMED, MY_SOUNDTRACKS])
    music.play_music("concentración", "playlist")  # not one of theirs: catalogue
    assert fake.sent("play")[0][1] == "spotify:playlist:focus"


def test_auto_only_takes_an_own_playlist_on_an_exact_name(spotify):
    fake = spotify(search={"tracks": [BRIGHTSIDE]}, library=[MY_SOUNDTRACKS])
    music.play_music("Bandas Sonoras")
    assert fake.sent("play")[0][1] == "spotify:playlist:mine"

    fake = spotify(search={"tracks": [BRIGHTSIDE]}, library=[MY_SOUNDTRACKS])
    music.play_music("bandas")  # partial: could be anything, so the search decides
    assert fake.sent("play")[0][1] == "spotify:track:bright"


def test_album(spotify):
    fake = spotify(search={"albums": [ABBEY]})
    assert "(álbum: Abbey Road — The Beatles)" in music.play_music("abbey road", "album")
    assert fake.sent("play")[0][1] == "spotify:album:abbey"


def test_queue_adds_without_interrupting(spotify):
    fake = spotify(search={"tracks": [BRIGHTSIDE]})
    assert music.play_music("mr brightside", "track", queue=True) == "Añadida a la cola: Mr. Brightside — The Killers."
    assert fake.sent("queue") == [("queue", "add", "spotify:track:bright")]
    assert fake.sent("play") == []

    spotify(search={"albums": [ABBEY]})
    with pytest.raises(ToolError, match="Solo puedo añadir canciones"):
        music.play_music("abbey road", "album", queue=True)


def test_now_playing(spotify):
    fake = spotify()
    assert music.now_playing() == "Ahora mismo no hay nada cargado en Spotify."
    fake.current = {
        "description": "Mr. Brightside — The Killers", "context_description": "Hot Fuss", "is_playing": False,
    }
    assert music.now_playing() == "En pausa: Mr. Brightside — The Killers (de: Hot Fuss)"


def test_tool_and_mode_step_use_the_same_player(spotify, ctx):
    fake = spotify(search={"playlists": [FOCUS]}, library=[MY_SOUNDTRACKS])
    result = registry.call("play_music", {"query": "bandas sonoras", "kind": "playlist"}, ctx)
    assert not result.is_error and fake.sent("play")[0][1] == "spotify:playlist:mine"
    assert registry.call("play_music", {"query": "x", "kind": "podcast"}, ctx).is_error  # not a valid kind

    registry.call("save_mode", {"name": "foco", "steps": [{"action": "play_music", "value": "Bandas Sonoras"}]}, ctx)
    ran = registry.call("run_mode", {"name": "foco"}, ctx)
    assert "FALLÓ" not in ran.content and "Sonando" in ran.content


def test_missing_cli_is_explained(monkeypatch, tmp_path):
    monkeypatch.setattr(music, "_CLI", tmp_path / "spotify_cli.exe")
    with pytest.raises(ToolError, match="Microsoft Store"):
        music.play_music("algo")
