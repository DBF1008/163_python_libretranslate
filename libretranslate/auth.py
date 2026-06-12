"""Centralized API-key access policy.

Single source of truth for *when a request without a valid API key is rejected*.
Keeping the rule in one place means the enforcement in ``access_check`` and the
policy advertised to clients via ``/frontend/settings`` (and the web UI) cannot
drift apart.

The functions here are deliberately free of any Flask request context: every
request-derived value is passed in as an argument, so the policy can be unit
tested without booting the app.
"""
import re

from libretranslate import flood, secret

# Decision codes returned by check()
ALLOW = "allow"  # request may proceed
NEED_KEY = "need_key"  # reject: a valid API key is required (HTTP 400)
INVALID_KEY = "invalid_key"  # an API key was supplied but is not valid (HTTP 403)
SERVE_BOGUS = "serve_bogus"  # honeypot: feign a successful translation (HTTP 200)


def key_required(args):
    """Whether keyless API access is gated *at all* on this server.

    Returns True when a client without a valid API key may be rejected unless it
    satisfies one of the alternative checks (matching Origin, secret or
    fingerprint) or — under attack mode — is always rejected. This is the value
    advertised by ``/frontend/settings`` (``keyRequired``) so that what we tell
    clients matches what :func:`check` actually enforces.
    """
    if not args.api_keys:
        return False
    return bool(
        args.under_attack
        or args.require_api_key_origin
        or args.require_api_key_secret
        or args.require_api_key_fingerprint
    )


def under_attack(args):
    """Whether the official web UI itself is unconditionally forced to use a key.

    Only ``under_attack`` mode forces a key on the bundled frontend: the Origin,
    secret and fingerprint checks all exempt the official UI (it is same-origin,
    it is served the secret, it establishes the fingerprint). This drives the
    frontend ``disableInput`` flag and the abuse banner.
    """
    return bool(args.api_keys and args.under_attack)


def check(args, api_keys_db, *, api_key, req_secret, origin, ip, fingerprint):
    """Decide whether a single request may access the API.

    Faithful extraction of the logic previously inlined in ``access_check``:
    same order, same side effects, and the bogus honeypot still short-circuits
    ahead of the generic rejection. Returns one of the decision codes above; the
    caller turns that into an HTTP response, keeping all Flask interaction in the
    route layer.
    """
    # A valid key always grants access; a supplied-but-unknown key is rejected
    # outright, regardless of the alternative checks.
    if api_key:
        if api_keys_db is not None and api_keys_db.lookup(api_key) is not None:
            return ALLOW
        return INVALID_KEY

    # No key supplied: the request is allowed unless a configured check fails.
    need_key = False

    if args.require_api_key_origin and not re.match(args.require_api_key_origin, origin or ""):
        need_key = True

    if args.require_api_key_secret and not secret.secret_match(req_secret):
        need_key = True
        # Clients that submit the *bogus* secret (served to non-browsers) are
        # occasionally fed a fake translation to waste a scraper's time. This
        # takes precedence over the generic rejection. secret_bogus_match is
        # intentionally random.
        if secret.secret_bogus_match(req_secret):
            return SERVE_BOGUS

    if args.require_api_key_fingerprint and flood.fingerprint_mismatch(ip, fingerprint):
        need_key = True

    if args.under_attack:
        need_key = True

    return NEED_KEY if need_key else ALLOW
