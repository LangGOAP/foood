"""One-command launch (issue #13): static app serving, export staleness and CORS."""

from __future__ import annotations

from pathlib import Path

from foood.launch import APPS, Site, export_is_current, write_stamp


def _site(tmp_path: Path) -> Site:
    root = tmp_path / "web"
    (root / "order").mkdir(parents=True)
    (root / "_expo").mkdir()
    (root / "index.html").write_text("<html>home</html>")
    (root / "login.html").write_text("<html>login</html>")
    (root / "order" / "[id].html").write_text("<html>order</html>")
    (root / "_expo" / "app.js").write_text("console.log(1)")
    (root / "+not-found.html").write_text("<html>404</html>")
    return Site(root)


def test_static_routes_resolve_like_expo_router(tmp_path: Path) -> None:
    site = _site(tmp_path)
    assert site.resolve("/").name == "index.html"
    assert site.resolve("/login").name == "login.html"
    assert site.resolve("/login?next=/cart").name == "login.html"
    assert site.resolve("/_expo/app.js").name == "app.js"
    assert site.resolve("/order/abc-123").name == "[id].html"
    assert site.resolve("/order/abc-123").parent.name == "order"
    assert site.resolve("/does/not/exist").name == "index.html"  # the SPA shell takes over
    assert site.resolve("/../../etc/passwd") is None
    assert site.content_type(Path("x.js")).startswith("text/javascript")
    assert site.content_type(Path("x.woff2")) == "font/woff2"


def test_export_stamp_tracks_the_inputs(tmp_path: Path) -> None:
    out = tmp_path / "customer"
    assert not export_is_current(out, "http://localhost:3000", {"A": "1"})
    out.mkdir()
    (out / "index.html").write_text("ok")
    write_stamp(out, "http://localhost:3000", {"A": "1"})
    assert export_is_current(out, "http://localhost:3000", {"A": "1"})
    assert not export_is_current(out, "http://localhost:3001", {"A": "1"})
    assert not export_is_current(out, "http://localhost:3000", {"A": "2"})
    (out / "index.html").unlink()
    assert not export_is_current(out, "http://localhost:3000", {"A": "1"})


def test_the_three_apps_have_fixed_ports() -> None:
    assert [a.port for a in APPS] == [8081, 8082, 8083]
    assert [a.name for a in APPS] == ["customer", "driver", "restaurant"]
    assert all(a.directory.name == a.name for a in APPS)


async def test_browser_origins_are_allowed(client) -> None:
    preflight = await client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:8081"
    assert "idempotency-key" in preflight.headers["access-control-allow-headers"].lower()
    plain = await client.get("/health", headers={"Origin": "http://127.0.0.1:8083"})
    assert plain.headers["access-control-allow-origin"] == "http://127.0.0.1:8083"
    elsewhere = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in elsewhere.headers


def test_every_app_is_built_in_english_and_dollars() -> None:
    from foood.launch import public_env

    for app in APPS:
        env = public_env(app, "http://localhost:3000")
        assert env["EXPO_PUBLIC_API_BASE_URL"] == "http://localhost:3000"
        assert env["EXPO_PUBLIC_DEFAULT_LOCALE"] == "en"
        assert env["EXPO_PUBLIC_CURRENCY"] == "USD"


def test_the_three_apps_come_from_the_forks() -> None:
    """Upstream formats each amount in the currency its response carries, EGP when none; our forks
    default to the build's EXPO_PUBLIC_CURRENCY."""
    from configparser import ConfigParser

    from foood.launch import ROOT

    modules = ConfigParser()
    modules.read(ROOT / ".gitmodules")
    for name in ("customer", "driver", "restaurant"):
        url = modules[f'submodule "apps/{name}"']["url"]
        assert url == f"https://github.com/LangGOAP/delivery-plus-{name}-app.git"


def test_the_checked_out_apps_carry_the_currency_switch() -> None:
    import pytest

    from foood.launch import ROOT

    for name in ("customer", "driver"):
        formatter = ROOT / "apps" / name / "src" / "utils" / "format.ts"
        if not formatter.exists():
            pytest.skip("the app submodules are not checked out here")
        assert "EXPO_PUBLIC_CURRENCY" in formatter.read_text(encoding="utf-8")


def test_every_app_gets_a_fixed_position_in_the_city() -> None:
    from foood.launch import public_env

    for app in APPS:
        lat, lon = (
            float(p) for p in public_env(app, "http://x")["EXPO_PUBLIC_FIXED_LOCATION"].split(",")
        )
        assert 37.70 <= lat <= 37.82 and -122.53 <= lon <= -122.35, app.name


def test_the_export_stamp_tracks_the_app_build(tmp_path: Path) -> None:
    """A changed fork (a new submodule commit) must re-export, not only a changed setting."""
    from foood.launch import app_commit, stamp_inputs

    app = APPS[0]
    inputs = stamp_inputs(app, "http://localhost:3000")
    assert inputs["EXPO_PUBLIC_CURRENCY"] == "USD"
    commit = app_commit(app)
    if commit is None:
        import pytest

        pytest.skip("the app submodules are not checked out here")
    assert inputs["appCommit"] == commit and len(commit) == 40


def test_make_up_takes_the_engine() -> None:
    from foood.launch import ROOT

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    up = makefile[makefile.index("\nup:") :].split("\n\n", 1)[0]
    assert "--engine $(ENGINE)" in up
