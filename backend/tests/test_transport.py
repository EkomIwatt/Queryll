"""CORS and cookie transport (Contracts 9 and 1).

Neither of these is visible from inside the API. They are browser-enforced, so a mistake
here passes every other test in this suite and then breaks Instance 3 completely at merge
with an opaque console error and a working `curl`.

Both are asserted at the level the browser actually checks: the response headers.
"""

import pytest

from app.config import get_settings

ORIGIN = "http://localhost:5173"


@pytest.fixture
def restore_settings():
    settings = get_settings()
    saved = (
        settings.cookie_secure,
        settings.cookie_samesite,
        settings.cors_origins,
    )
    yield settings
    (settings.cookie_secure, settings.cookie_samesite, settings.cors_origins) = saved


class TestCors:
    async def test_a_preflight_from_an_allowed_origin_is_approved(self, client):
        response = await client.options(
            "/api/documents",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert response.status_code in (200, 204)
        assert response.headers["access-control-allow-origin"] == ORIGIN

    async def test_credentials_are_allowed_so_the_refresh_cookie_can_travel(
        self, client
    ):
        # Without this the httpOnly refresh cookie never reaches /api/auth/refresh from
        # the browser, and the whole session-renewal path silently does not work.
        response = await client.options(
            "/api/auth/refresh",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.headers["access-control-allow-credentials"] == "true"

    async def test_the_authorization_header_is_on_the_allow_list(self, client):
        response = await client.options(
            "/api/documents",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        allowed = response.headers.get("access-control-allow-headers", "").lower()
        assert "authorization" in allowed
        assert "content-type" in allowed

    async def test_the_origin_is_echoed_not_wildcarded(self, client):
        # A literal "*" is illegal alongside allow-credentials and browsers reject it,
        # so this asserts the specific origin comes back rather than a wildcard.
        response = await client.get("/api/health", headers={"Origin": ORIGIN})
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert response.headers["access-control-allow-origin"] != "*"

    async def test_an_origin_that_is_not_on_the_list_gets_no_approval(self, client):
        response = await client.get(
            "/api/health", headers={"Origin": "https://not-our-frontend.example"}
        )
        assert "access-control-allow-origin" not in response.headers

    async def test_every_method_the_frontend_uses_is_permitted(self, client):
        for method in ["GET", "POST", "DELETE"]:
            response = await client.options(
                "/api/documents",
                headers={
                    "Origin": ORIGIN,
                    "Access-Control-Request-Method": method,
                },
            )
            allowed = response.headers.get("access-control-allow-methods", "")
            assert method in allowed, method


class TestRefreshCookieAttributes:
    async def test_production_settings_produce_a_secure_cross_site_cookie(
        self, client, db, restore_settings
    ):
        """Vercel -> Render is cross-site, so the cookie needs Secure + SameSite=None.

        The suite otherwise runs with the local development settings (insecure, Lax),
        because a Secure cookie is not stored over plain http. This test flips to the
        production shape so a regression there is caught here rather than on Render.
        """
        restore_settings.cookie_secure = True
        restore_settings.cookie_samesite = "none"

        response = await client.post(
            "/api/auth/signup",
            json={"email": "cookies@example.com", "password": "correct horse battery"},
        )
        assert response.status_code == 201

        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=none" in cookie.replace("samesite", "SameSite")
        assert "Path=/" in cookie

    async def test_the_refresh_token_value_is_never_the_access_token(self, client):
        response = await client.post(
            "/api/auth/signup",
            json={"email": "distinct@example.com", "password": "correct horse battery"},
        )
        access_token = response.json()["access_token"]
        assert access_token not in response.headers["set-cookie"]

    async def test_logout_expires_the_cookie_rather_than_leaving_it(self, client):
        await client.post(
            "/api/auth/signup",
            json={"email": "bye@example.com", "password": "correct horse battery"},
        )
        response = await client.post("/api/auth/logout")
        cookie = response.headers["set-cookie"]
        # Starlette expresses deletion as an immediate expiry, not as an absent header.
        assert "queryll_refresh=" in cookie
        assert "Max-Age=0" in cookie or "expires=" in cookie.lower()
