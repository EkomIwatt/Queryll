"""Contract 1 -- authentication, carried over from LedgerLite / TaskFlow."""

import uuid

from conftest import auth_headers, make_user

SIGNUP = "/api/auth/signup"
LOGIN = "/api/auth/login"
REFRESH = "/api/auth/refresh"
LOGOUT = "/api/auth/logout"
ME = "/api/auth/me"

CREDENTIALS = {"email": "reader@example.com", "password": "correct horse battery"}


class TestSignup:
    async def test_returns_201_with_an_access_token_and_the_user(self, client):
        response = await client.post(SIGNUP, json=CREDENTIALS)
        assert response.status_code == 201

        body = response.json()
        assert body["access_token"]
        assert body["user"]["email"] == "reader@example.com"
        assert uuid.UUID(body["user"]["id"])

    async def test_display_name_falls_back_to_the_email_local_part(self, client):
        response = await client.post(SIGNUP, json=CREDENTIALS)
        assert response.json()["user"]["display_name"] == "reader"

    async def test_sets_an_httponly_refresh_cookie(self, client):
        response = await client.post(SIGNUP, json=CREDENTIALS)
        cookie = response.headers.get("set-cookie", "")
        assert "queryll_refresh=" in cookie
        assert "HttpOnly" in cookie

    async def test_never_returns_the_password_or_its_hash(self, client):
        response = await client.post(SIGNUP, json=CREDENTIALS)
        assert "password" not in response.text.lower()

    async def test_rejects_a_password_under_eight_characters(self, client):
        response = await client.post(
            SIGNUP, json={"email": "a@example.com", "password": "short"}
        )
        assert response.status_code == 422
        assert response.json() == {
            "error": "Your password needs to be at least 8 characters long."
        }

    async def test_rejects_a_malformed_email_with_the_error_envelope(self, client):
        response = await client.post(
            SIGNUP, json={"email": "not-an-email", "password": "correct horse"}
        )
        assert response.status_code == 422
        assert list(response.json().keys()) == ["error"]

    async def test_a_duplicate_email_is_a_conflict(self, client):
        await client.post(SIGNUP, json=CREDENTIALS)
        response = await client.post(SIGNUP, json=CREDENTIALS)
        assert response.status_code == 409
        assert "already exists" in response.json()["error"]

    async def test_email_is_normalised_to_lower_case(self, client):
        await client.post(
            SIGNUP, json={"email": "Mixed@Example.com", "password": "correct horse"}
        )
        response = await client.post(
            LOGIN, json={"email": "mixed@example.com", "password": "correct horse"}
        )
        assert response.status_code == 200


class TestLogin:
    async def test_returns_a_fresh_access_token(self, client):
        await client.post(SIGNUP, json=CREDENTIALS)
        response = await client.post(LOGIN, json=CREDENTIALS)
        assert response.status_code == 200
        assert response.json()["access_token"]

    async def test_a_wrong_password_is_401(self, client):
        await client.post(SIGNUP, json=CREDENTIALS)
        response = await client.post(
            LOGIN, json={"email": CREDENTIALS["email"], "password": "wrong password"}
        )
        assert response.status_code == 401

    async def test_an_unknown_account_gives_the_same_sentence_as_a_wrong_password(
        self, client
    ):
        await client.post(SIGNUP, json=CREDENTIALS)
        wrong_password = await client.post(
            LOGIN, json={"email": CREDENTIALS["email"], "password": "wrong password"}
        )
        unknown_account = await client.post(
            LOGIN, json={"email": "nobody@example.com", "password": "wrong password"}
        )
        # Identical responses, so login cannot be used to find out who has an account.
        assert wrong_password.status_code == unknown_account.status_code == 401
        assert wrong_password.json() == unknown_account.json()


class TestRefresh:
    async def test_exchanges_the_cookie_for_a_new_access_token(self, client):
        await client.post(SIGNUP, json=CREDENTIALS)
        response = await client.post(REFRESH)
        assert response.status_code == 200
        assert response.json()["user"]["email"] == CREDENTIALS["email"]

    async def test_rotates_the_refresh_cookie(self, client):
        await client.post(SIGNUP, json=CREDENTIALS)
        first = client.cookies.get("queryll_refresh")
        response = await client.post(REFRESH)
        assert response.status_code == 200
        assert client.cookies.get("queryll_refresh") != first

    async def test_without_a_cookie_it_is_401(self, client):
        response = await client.post(REFRESH)
        assert response.status_code == 401

    async def test_an_access_token_is_not_accepted_as_a_refresh_token(self, client):
        signup = await client.post(SIGNUP, json=CREDENTIALS)
        # Replace the genuine refresh cookie, do not sit alongside it.
        client.cookies.clear()
        client.cookies.set("queryll_refresh", signup.json()["access_token"])
        response = await client.post(REFRESH)
        assert response.status_code == 401


class TestLogoutAndMe:
    async def test_logout_clears_the_cookie_and_returns_204(self, client):
        await client.post(SIGNUP, json=CREDENTIALS)
        response = await client.post(LOGOUT)
        assert response.status_code == 204
        assert not client.cookies.get("queryll_refresh")

    async def test_me_returns_the_signed_in_user(self, client, db):
        user = await make_user(db, "me@example.com")
        response = await client.get(ME, headers=auth_headers(user))
        assert response.status_code == 200
        assert response.json()["email"] == "me@example.com"

    async def test_me_without_a_token_is_401_with_the_error_envelope(self, client):
        response = await client.get(ME)
        assert response.status_code == 401
        assert list(response.json().keys()) == ["error"]

    async def test_me_with_a_garbage_token_is_401(self, client):
        response = await client.get(ME, headers={"Authorization": "Bearer nonsense"})
        assert response.status_code == 401

    async def test_me_with_the_wrong_scheme_is_401(self, client, db):
        user = await make_user(db)
        from app.security import create_access_token

        response = await client.get(
            ME, headers={"Authorization": "Basic " + create_access_token(user.id)}
        )
        assert response.status_code == 401

    async def test_a_token_signed_with_another_secret_is_rejected(self, client, db):
        import jwt

        user = await make_user(db)
        forged = jwt.encode(
            {"sub": str(user.id), "type": "access", "exp": 9999999999},
            "not-the-real-secret-but-long-enough-for-hmac-sha256",
            algorithm="HS256",
        )
        response = await client.get(ME, headers={"Authorization": "Bearer " + forged})
        assert response.status_code == 401

    async def test_an_expired_token_is_rejected(self, client, db):
        import jwt

        from app.config import get_settings

        user = await make_user(db)
        expired = jwt.encode(
            {"sub": str(user.id), "type": "access", "exp": 1000000000},
            get_settings().jwt_secret,
            algorithm="HS256",
        )
        response = await client.get(ME, headers={"Authorization": "Bearer " + expired})
        assert response.status_code == 401
