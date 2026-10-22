"""Accounts end to end: sign-up, login, sessions, API keys, the per-user quota, and the account page."""

import re

from conftest import PASSWORD
from fastapi.testclient import TestClient

from scraper import auth, db, users
from scraper.app import EXAMPLES, SITE_CARDS, app, highlight_json

SCRAPE = {"url": "https://example.com/page"}


def signup(client: TestClient, email: str = "bob@example.com", password: str = PASSWORD, confirm: str | None = None):
    data = {"email": email, "password": password, "confirm": password if confirm is None else confirm}
    return client.post("/signup", data=data, follow_redirects=False)


def login(client: TestClient, email: str = "ann@example.com", password: str = PASSWORD, next: str = "/"):
    return client.post("/login", data={"email": email, "password": password, "next": next}, follow_redirects=False)


def csrf(client: TestClient) -> str:
    """The logged-in session's CSRF token, as the pages hand it to their forms and scripts."""
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get("/app").text).group(1)


def logged_in(user) -> TestClient:
    client = TestClient(app)
    assert login(client, user.email).status_code == 303
    return client


def new_key(client: TestClient) -> str:
    """Create a key through the dashboard form and read it off the page, as a user would."""
    response = client.post("/dashboard/keys", data={"name": "laptop", "csrf_token": csrf(client)})
    assert response.status_code == 201
    return re.search(r'id="new-key">([^<]+)<', response.text).group(1)


# ---- Sign-up -----------------------------------------------------------------------------------


def test_signup_logs_in_and_lands_on_the_dashboard(client):
    response = signup(client, "  Bob@Example.com ")
    assert response.status_code == 303 and response.headers["location"] == "/app"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200 and "bob@example.com" in dashboard.text  # stored lowercased


def test_signup_rejects_bad_input(client, user):
    assert signup(client, "not-an-email").status_code == 400
    assert signup(client, password="short").status_code == 400
    assert signup(client, confirm="something else").status_code == 400
    taken = signup(client, "ANN@example.com")
    assert taken.status_code == 409 and "already exists" in taken.text


def test_signups_are_limited_per_ip(client):
    assert [signup(client, f"user{i}@example.com").status_code for i in range(4)] == [303, 303, 303, 429]


def test_passwords_are_stored_hashed(user):
    with db.connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()[0]
    assert stored.startswith("scrypt$") and PASSWORD not in stored


# ---- Login and logout --------------------------------------------------------------------------


def test_login_redirects_back_only_to_this_site(client, user):
    assert login(client, password="wrong password").status_code == 401
    assert login(client, next="/dashboard").headers["location"] == "/dashboard"
    other = TestClient(app)
    assert login(other, next="//evil.example/").headers["location"] == "/app"
    assert login(TestClient(app), next="https://evil.example/").headers["location"] == "/app"


def test_logins_are_limited(client, user):
    codes = [login(client, password="wrong password").status_code for _ in range(5)]
    assert codes == [401] * 5
    assert login(client).status_code == 429  # even the right password, until the minute is over


def test_unknown_email_and_wrong_password_look_the_same(client, user):
    unknown = login(client, "nobody@example.com")
    wrong = login(client, password="wrong password")
    assert unknown.status_code == wrong.status_code == 401
    assert "Wrong email or password." in unknown.text and "Wrong email or password." in wrong.text


def test_logout_ends_the_session(user):
    client = logged_in(user)
    response = client.post("/logout", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login?next=/dashboard"


def test_pages_need_a_login(client):
    for page in ("/app", "/dashboard", "/account"):
        response = client.get(page, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == f"/login?next={page}"


def test_nav_shows_login_or_account_links(client, user):
    assert 'href="/signup"' in client.get("/").text and 'href="/dashboard"' not in client.get("/").text
    page = logged_in(user).get("/app").text
    assert (
        'href="/app"' in page
        and 'href="/dashboard"' in page
        and 'href="/account"' in page
        and 'action="/logout"' in page
    )


# ---- Forms can't be sent from other sites ------------------------------------------------------


def test_cross_site_and_tokenless_form_posts_are_refused(client, user):
    evil = {"origin": "https://evil.example"}
    assert client.post("/login", data={"email": user.email, "password": PASSWORD}, headers=evil).status_code == 403
    client = logged_in(user)
    token = csrf(client)
    assert client.post("/dashboard/keys", data={"csrf_token": token}, headers=evil).status_code == 403
    assert client.post("/dashboard/keys", data={"csrf_token": "wrong"}).status_code == 403
    assert client.post("/dashboard/keys", data={}).status_code == 403
    assert auth.list_keys(user.id) == []


# ---- API keys ----------------------------------------------------------------------------------


def test_keys_are_shown_once_and_stored_only_as_a_hash(user):
    client = logged_in(user)
    key = new_key(client)
    assert key.startswith("sk_") and len(key) > 40
    with db.connect() as conn:
        rows = [tuple(row) for row in conn.execute("SELECT * FROM api_keys")]
    assert key not in str(rows) and auth.hash_key(key) in str(rows)
    dashboard = client.get("/dashboard").text
    assert key not in dashboard and key[: auth.SHOWN_PREFIX_LENGTH] in dashboard


def test_at_most_three_keys(user):
    client = logged_in(user)
    for _ in range(3):
        new_key(client)
    refused = client.post("/dashboard/keys", data={"csrf_token": csrf(client)})
    assert refused.status_code == 409 and "Revoke one first" in refused.text
    assert len(auth.list_keys(user.id)) == 3


def test_revoked_keys_stop_working_right_away(client, fetched, user):
    owner = logged_in(user)
    key = new_key(owner)
    assert client.get("/scrape", params=SCRAPE, headers={"X-API-Key": key}).status_code == 200
    (record,) = auth.list_keys(user.id)
    owner.post(f"/dashboard/keys/{record.id}/revoke", data={"csrf_token": csrf(owner)})
    assert auth.list_keys(user.id) == []
    assert client.get("/scrape", params=SCRAPE, headers={"X-API-Key": key}).status_code == 401


def test_a_user_cannot_revoke_someone_elses_key(user, key):
    (record,) = auth.list_keys(user.id)
    users.create_user("bob@example.com", PASSWORD)
    bob = logged_in(users.authenticate("bob@example.com", PASSWORD))
    bob.post(f"/dashboard/keys/{record.id}/revoke", data={"csrf_token": csrf(bob)})
    assert len(auth.list_keys(user.id)) == 1


# ---- The quota: 10 a minute per user, shared by every key and the website ----------------------


def test_the_quota_is_shared_by_all_keys_of_a_user(client, fetched, user, key):
    second_key, _ = auth.create_key(user.id, "second")
    codes = [
        client.get("/scrape", params=SCRAPE, headers=key if i % 2 else {"X-API-Key": second_key}).status_code
        for i in range(10)
    ]
    assert codes == [200] * 10
    refused = client.get("/scrape", params=SCRAPE, headers={"X-API-Key": second_key})
    assert refused.status_code == 429 and refused.json() == {"error": "Rate limit exceeded: 10 per 1 minute"}
    assert refused.headers["retry-after"].isdigit()
    # Another user has a quota of their own.
    bob = users.create_user("bob@example.com", PASSWORD)
    bob_key, _ = auth.create_key(bob.id, "bob")
    assert client.get("/scrape", params=SCRAPE, headers={"X-API-Key": bob_key}).status_code == 200


def test_responses_say_how_much_quota_is_left(client, fetched, key):
    first = client.get("/scrape", params=SCRAPE, headers=key)
    second = client.get("/scrape", params=SCRAPE, headers=key)
    assert first.headers["x-ratelimit-limit"] == "10"
    assert (first.headers["x-ratelimit-remaining"], second.headers["x-ratelimit-remaining"]) == ("9", "8")
    assert 0 < int(second.headers["x-ratelimit-reset"]) <= 60


def test_usage_is_recorded_per_day_and_per_key(client, fetched, user, key):
    client.get("/scrape", params=SCRAPE, headers=key)
    client.post("/scrape/batch", json={"urls": ["https://example.com/a", "https://example.com/b"]}, headers=key)
    assert users.usage_by_day(user.id)[-1][1] == 3 and users.total_usage(user.id) == 3
    (record,) = auth.list_keys(user.id)
    assert record.requests == 3 and record.last_used_at is not None
    dashboard = logged_in(user).get("/dashboard").text
    assert "7<small> / 10</small>" in dashboard  # 3 of 10 used in the last minute


# ---- The website scrapes with the login --------------------------------------------------------


def test_the_website_scrapes_with_the_session_and_its_csrf_token(fetched, user):
    client = logged_in(user)
    assert client.get("/scrape", params=SCRAPE).status_code == 401  # the cookie alone isn't enough
    assert client.get("/scrape", params=SCRAPE, headers={"X-CSRF-Token": "wrong"}).status_code == 401
    response = client.get("/scrape", params=SCRAPE, headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 200 and response.headers["x-ratelimit-remaining"] == "9"
    assert users.total_usage(user.id) == 1  # same quota and usage as the API keys


def test_the_no_javascript_form_needs_a_login(client, fetched, user):
    refused = client.post("/app", data={"url": SCRAPE["url"]}, follow_redirects=False)
    assert refused.status_code == 303 and refused.headers["location"] == "/login?next=/app"
    client = logged_in(user)
    assert client.post("/app", data={"url": SCRAPE["url"], "csrf_token": "wrong"}).status_code == 403
    page = client.post("/app", data={"url": SCRAPE["url"], "csrf_token": csrf(client)})
    assert page.status_code == 200 and "Ann" in page.text


# ---- Landing page and workbench ----------------------------------------------------------------


def test_visitors_get_the_landing_page(client):
    page = client.get("/")
    assert page.status_code == 200 and 'href="/signup"' in page.text and 'href="/login"' in page.text
    assert 'id="scrape-form"' not in page.text  # the workbench is only for logged-in users
    assert 'id="sites"' in page.text and 'data-host="imdb.com"' in page.text


def test_logged_in_users_go_from_the_landing_page_to_the_workbench(user):
    client = logged_in(user)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/app"
    workbench = client.get("/app").text
    assert 'id="scrape-form"' in workbench and 'name="csrf_token"' in workbench
    assert 'name="api_key"' not in workbench  # the login replaced the key field


def test_the_workbench_sends_visitors_to_log_in_and_back(client, user):
    response = client.get("/app?url=https://quotes.toscrape.com/&mode=json", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/app%3Furl%3Dhttps%3A//quotes.toscrape.com/%26mode%3Djson"
    login_form = client.get(response.headers["location"]).text
    assert 'name="next" value="/app?url=https://quotes.toscrape.com/&amp;mode=json"' in login_form
    back = login(client, next="/app?url=https://quotes.toscrape.com/&mode=json")
    assert back.headers["location"] == "/app?url=https://quotes.toscrape.com/&mode=json"
    assert 'value="https://quotes.toscrape.com/"' in client.get(back.headers["location"]).text


def test_old_workbench_links_still_open_the_request(client):
    response = client.get("/?url=https://quotes.toscrape.com/&mode=json", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/app?url=https://quotes.toscrape.com/&mode=json"


def test_signup_and_login_land_on_the_workbench(client, user):
    assert signup(client).headers["location"] == "/app"
    no_next = client.post("/login", data={"email": user.email, "password": PASSWORD}, follow_redirects=False)
    assert no_next.headers["location"] == "/app"
    assert 'name="next" value="/app"' in TestClient(app).get("/login").text


# ---- Account page ------------------------------------------------------------------------------


def test_changing_the_password_logs_out_other_devices(user):
    client, other = logged_in(user), logged_in(user)
    token = csrf(client)
    wrong = client.post(
        "/account/password",
        data={"current": "nope", "password": "new password 1", "confirm": "new password 1", "csrf_token": token},
    )
    assert wrong.status_code == 400 and "current password is wrong" in wrong.text
    changed = client.post(
        "/account/password",
        data={"current": PASSWORD, "password": "new password 1", "confirm": "new password 1", "csrf_token": token},
    )
    assert changed.status_code == 200 and "Password changed" in changed.text
    assert client.get("/account").status_code == 200  # this browser stays logged in
    assert other.get("/account", follow_redirects=False).status_code == 303  # the other one doesn't
    assert users.authenticate(user.email, "new password 1") and not users.authenticate(user.email, PASSWORD)


def test_deleting_the_account_removes_its_keys(client, fetched, user, key):
    owner = logged_in(user)
    token = csrf(owner)
    assert owner.post("/account/delete", data={"password": "nope", "csrf_token": token}).status_code == 400
    deleted = owner.post("/account/delete", data={"password": PASSWORD, "csrf_token": token}, follow_redirects=False)
    assert deleted.status_code == 303
    assert client.get("/scrape", params=SCRAPE, headers=key).status_code == 401
    assert users.authenticate(user.email, PASSWORD) is None
    with db.connect() as conn:
        assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("api_keys", "sessions")] == [0, 0]


def test_every_supported_site_has_a_saved_json_example():
    # After adding or changing a parser, run scripts/refresh_examples.py.
    for card in SITE_CARDS:
        example = EXAMPLES.get(card["host"])
        assert example is not None, f"no example for {card['host']}"
        assert example["example"] == card["example"], f"the example for {card['host']} is out of date"
        assert example["parsed"], card["host"]


def test_the_landing_page_folds_out_each_sites_example(client):
    page = client.get("/").text
    assert page.count('<details class="site-ex"') == len(SITE_CARDS)
    assert '<span class="k">&#34;title&#34;</span>' in page  # highlighted, escaped JSON
    assert 'href="https://www.imdb.com/title/tt0111161/" target="_blank" rel="noopener noreferrer"' in page


def test_json_examples_are_escaped():
    html = str(highlight_json({"name": "</pre><script>alert(1)</script>"}))
    assert "<script>" not in html and "&lt;script&gt;" in html
