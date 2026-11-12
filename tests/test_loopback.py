"""#248 D12: one loopback check in ``kelpie.core`` for every listener and for the resources
and traces settings, behaviour unchanged. Each caller is compared with the copy it had
before, on fixed edge cases and on generated hosts."""

from __future__ import annotations

import ipaddress

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kelpie import httpguard
from kelpie.core.loopback import is_loopback
from kelpie.remote import config as remote_config
from kelpie.resources import config as resources_config
from kelpie.traces import config as traces_config

# --- the copies each caller had (main @ 541aa40) ------------------------------------------


def old_traces(host: str) -> bool:  # traces/config.is_loopback = resources/config._loopback
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def old_remote(host: str) -> bool:  # remote/config._loopback
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _old_bare(name: str) -> str:
    return name.strip("[]").rstrip(".")


def old_loopback_ip(name: str) -> bool:  # httpguard._loopback_ip
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def old_httpguard(host: str) -> bool:  # httpguard.is_loopback
    name = _old_bare(host.strip().lower())
    return name == "localhost" or old_loopback_ip(name)


EDGES = [
    "",
    " ",
    "localhost",
    "LOCALHOST",
    " localhost ",
    "localhost.",
    "[localhost]",
    "127.0.0.1",
    "127.1.2.3",
    " 127.0.0.1",
    "127.0.0.1.",
    "[127.0.0.1]",
    "::1",
    "[::1]",
    "[::1",
    "::1]",
    "::ffff:127.0.0.1",
    "[::ffff:127.0.0.1]",
    "::ffff:10.0.0.1",
    "0.0.0.0",
    "::",
    "10.0.0.1",
    "host-a",
    "localhost.example.org",
    "127.0.0.1.example.org",
    "fe80::1%lo",
    "::1%lo",
    "0x7f.1",
    "2130706433",
    # whitespace or brackets around what _bare leaves: httpguard must not take them off
    "[ localhost]",
    "[localhost ]",
    " [ localhost ]",
    "localhost .",
    "localhost\t.",
    "[\tlocalhost]",
    "[::1].",
    "[127.0.0.1].",
    "[::1]]..",
]


@pytest.mark.parametrize("host", EDGES)
def test_every_caller_answers_as_before(host: str) -> None:
    assert traces_config.is_loopback(host) is old_traces(host)
    assert resources_config._loopback(host) is old_traces(host)
    assert remote_config._loopback(host) is old_remote(host)
    assert httpguard.is_loopback(host) is old_httpguard(host)
    assert httpguard._loopback_ip(host) is old_loopback_ip(host)


HOSTS = st.one_of(
    st.text(alphabet=" \t[]:.0123456789abcdefLOCALHOSTlocalhost%f", max_size=24),
    st.tuples(
        st.sampled_from(["", " ", "[", " [", "[ "]),
        st.sampled_from(["localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1"]),
        st.sampled_from(["", " ", "]", " ]", "] ", ".", "].", " .", "]]..", "\t."]),
    ).map("".join),
    st.ip_addresses().map(str),
    st.ip_addresses(v=6).map(lambda ip: f"[{ip}]"),
    st.ip_addresses(v=4).map(lambda ip: f"::ffff:{ip}"),
)


@given(HOSTS)
def test_generated_hosts_answer_as_before(host: str) -> None:
    assert traces_config.is_loopback(host) is old_traces(host)
    assert resources_config._loopback(host) is old_traces(host)
    assert remote_config._loopback(host) is old_remote(host)
    assert httpguard.is_loopback(host) is old_httpguard(host)
    assert httpguard._loopback_ip(host) is old_loopback_ip(host)


def test_the_core_check_resolves_nothing_and_never_raises() -> None:
    """A name that resolves to loopback elsewhere is still not one: nothing is looked up
    (a rebound name must never count)."""
    for name in ("localhost.localdomain", "ip6-localhost", "kelpie.invalid"):
        assert not is_loopback(name)
    assert not is_loopback("localhost", names=False)
    assert is_loopback("::ffff:127.0.0.1", mapped=True)
    assert not is_loopback("::ffff:127.0.0.1")
    assert not is_loopback("::ffff:10.0.0.1", mapped=True)
    assert not is_loopback(2130706433)  # type: ignore[arg-type]  # not a str: never raises
