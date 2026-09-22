"""Network settings live only in the host platform configuration."""
import ipaddress

from .errors import V2Error

DEFAULT_NETWORK = {"enable": False, "host": "127.0.0.1", "port": 5700, "token": "", "writes": False}
NETWORK_FIELDS = set(DEFAULT_NETWORK) - {"token"}


def network_config(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or value.keys() - DEFAULT_NETWORK.keys():
        raise V2Error("invalid_network_config", "Only documented OneBot network fields are accepted.")
    result = {**DEFAULT_NETWORK, **value}
    if type(result["enable"]) is not bool or type(result["writes"]) is not bool:
        raise V2Error("invalid_network_config", "Network enable and writes must be boolean.")
    try:
        if not isinstance(result["host"], str) or "%" in result["host"]:
            raise ValueError
        ipaddress.ip_address(result["host"])
    except ValueError:
        raise V2Error("invalid_network_config", "Bind to an explicit IPv4 or IPv6 address.") from None
    if type(result["port"]) is not int or not 1 <= result["port"] <= 65535:
        raise V2Error("invalid_network_config", "Network port must be an integer in 1..65535.")
    token = result["token"]
    if (not isinstance(token, str) or token and not 16 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token)
            or token and (all(c in "*" for c in token) or token == "[REDACTED]")):
        raise V2Error("invalid_network_token", "Use a dedicated non-masked 16..512 character printable ASCII access token.")
    if result["enable"] and not token:
        raise V2Error("missing_network_token", "An enabled OneBot listener requires a dedicated token.")
    return result
