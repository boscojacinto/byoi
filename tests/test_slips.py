from apps.api.slips import compose_checkin_slip, public_base, seat_join_url


def test_public_base_rewrites_loopback_to_lan():
    url = public_base("http://127.0.0.1:8787")
    assert url.startswith("https://")
    assert ":8787" in url
    assert "192.168.44.1" not in url


def test_seat_join_is_lan_not_bluetooth_pan():
    join = seat_join_url({"agent_url": "http://127.0.0.1:8787"}, "deadbeef")
    assert "otp=deadbeef" in join
    assert join.endswith("/join?otp=deadbeef") or "/join?otp=deadbeef" in join
    assert "192.168.44.1" not in join


def test_seat_join_keeps_non_loopback_host():
    join = seat_join_url({"agent_url": "http://10.0.0.12:8787"}, "abcd")
    assert join == "https://10.0.0.12:8787/join?otp=abcd"


def test_checkin_slip_is_wifi_not_rfcomm():
    image = compose_checkin_slip(
        salon="BYOI",
        seat_name="Seat 1",
        coder_name="Ada",
        board_title=None,
        otp="deadbeef",
        wellness_minutes=90,
        break_after=50,
        wifi_ssid="salon Wi-Fi",
        join="http://10.0.0.12:8787/join?otp=deadbeef",
    )
    assert image.size[0] == 576
    assert image.size[1] > 400
