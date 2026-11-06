"""The reaper ends only the tunnel account's silent sessions, never anybody else's."""

import importlib.util
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "deploy" / "game-tunnel" / "aw_tunnel_reaper.py"
SPEC = importlib.util.spec_from_file_location("aw_tunnel_reaper", PATH)
assert SPEC and SPEC.loader
reaper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reaper)

# The shape of `ss -H -tnpi` on the server: a line per connection, then its socket details.
SS = """\
0 8320 192.0.2.1:22 198.51.100.7:58515 users:(("sshd",pid=1276997,fd=4),("sshd",pid=1276900,fd=4))
\t sack cubic wscale:8,7 rto:120000 backoff:10 rtt:179.3/11.8 bytes_sent:3026171 lastsnd:120718 lastrcv:443234 lastack:120718 cwnd:1
0 0 192.0.2.1:22 198.51.100.7:58521 users:(("sshd",pid=1277100,fd=4),("sshd",pid=1277090,fd=4))
\t sack cubic wscale:8,7 rto:409 rtt:208.7/19.3 bytes_sent:22763 lastsnd:714247 lastrcv:714247 lastack:714025 cwnd:8
0 0 192.0.2.1:22 203.0.113.9:40001 users:(("sshd",pid=1277200,fd=4),("sshd",pid=1277190,fd=4))
\t sack cubic wscale:8,7 rto:204 rtt:20.1/3.2 bytes_sent:9000 lastsnd:1200 lastrcv:6100 lastack:1200 cwnd:10
"""  # noqa: E501
TUNNEL = {1276997, 1276900, 1277200, 1277190}  # 1277100 is somebody's own terminal


def test_only_silent_tunnel_sessions_are_chosen():
    found = reaper.stale(SS, 75.0, lambda pid: pid in TUNNEL)
    assert sorted(pid for pid, _ in found) == [1276900, 1276997]  # the silent one of the tunnel
    assert all(silent > 400 for _, silent in found)


def test_a_session_with_news_every_few_seconds_is_left_alone():
    assert reaper.stale(SS, 75.0, lambda pid: pid in {1277200, 1277190}) == []


def test_somebody_elses_silent_session_is_not_touched():
    assert reaper.stale(SS, 75.0, lambda pid: False) == []


def test_empty_and_odd_output_is_harmless():
    assert reaper.stale("", 75.0, lambda pid: True) == []
    assert reaper.stale("garbage\n\t no numbers here\n", 75.0, lambda pid: True) == []


def test_only_the_tunnel_account_process_name_matches(tmp_path, monkeypatch):
    assert reaper.ACCOUNT == b"sshd: aw-game-tunnel"
    assert reaper.is_tunnel_process(0) is False  # no such process
