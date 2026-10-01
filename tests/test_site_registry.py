"""Registre multi-site et résolution par contexte de ``site_config``.

Le contrat central : sans ``use_site`` actif, ``current()`` se comporte
exactement comme avant (singleton ``install``/``uninstall``, puis repli sur
l'environnement) — c'est ce qui protège les 14 appelants historiques. Le
registre et le contexte ne s'activent qu'à l'intérieur d'un bloc
``use_site``.
"""

from __future__ import annotations

import threading
import unittest

import site_fixture
from quadringent.site_config import (
    SiteConfigurationError,
    current,
    install,
    registry,
    uninstall,
    use_site,
)


SITE = site_fixture.build_test_site()


class LegacyResolutionUnchangedTests(unittest.TestCase):
    """Sans contexte actif, aucune régression sur le comportement historique."""

    def tearDown(self) -> None:
        uninstall()

    def test_current_falls_back_to_environment_without_registry_or_install(self) -> None:
        # Le fixture applique déjà les variables QUADRINGENT_* au processus.
        resolved = current()
        self.assertEqual(resolved.site_id, SITE.site_id)

    def test_current_returns_the_installed_singleton(self) -> None:
        other = site_fixture.build_test_site(site_id="other-site")
        install(other)
        self.assertIs(current(), other)

    def test_registering_a_site_does_not_affect_current_outside_a_context(self) -> None:
        site = site_fixture.build_test_site(site_id="registered-only")
        registry().register(site, replace=True)
        # Aucun use_site actif : current() ignore totalement le registre.
        resolved = current()
        self.assertEqual(resolved.site_id, SITE.site_id)


class SiteRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = registry()

    def test_register_then_get_returns_the_same_config(self) -> None:
        site = site_fixture.build_test_site(site_id="site-a")
        self.reg.register(site, replace=True)
        self.assertIs(self.reg.get("site-a"), site)

    def test_get_unknown_site_id_fails_closed(self) -> None:
        with self.assertRaises(SiteConfigurationError):
            self.reg.get("does-not-exist")

    def test_duplicate_registration_is_refused_without_replace(self) -> None:
        site = site_fixture.build_test_site(site_id="site-dup")
        self.reg.register(site, replace=True)
        with self.assertRaises(SiteConfigurationError):
            self.reg.register(site)

    def test_replace_true_overwrites_an_existing_entry(self) -> None:
        first = site_fixture.build_test_site(site_id="site-replace")
        second = site_fixture.build_test_site(site_id="site-replace", environment="int")
        self.reg.register(first, replace=True)
        self.reg.register(second, replace=True)
        self.assertIs(self.reg.get("site-replace"), second)

    def test_unregister_removes_the_entry(self) -> None:
        site = site_fixture.build_test_site(site_id="site-gone")
        self.reg.register(site, replace=True)
        self.reg.unregister("site-gone")
        with self.assertRaises(SiteConfigurationError):
            self.reg.get("site-gone")

    def test_contains_reflects_registration_state(self) -> None:
        site = site_fixture.build_test_site(site_id="site-contains")
        self.assertNotIn("site-contains", self.reg)
        self.reg.register(site, replace=True)
        self.assertIn("site-contains", self.reg)


class UseSiteContextTests(unittest.TestCase):
    def tearDown(self) -> None:
        uninstall()

    def test_current_resolves_the_context_site_inside_the_block(self) -> None:
        site = site_fixture.build_test_site(site_id="ctx-site")
        registry().register(site, replace=True)
        with use_site("ctx-site") as yielded:
            self.assertIs(yielded, site)
            self.assertIs(current(), site)

    def test_context_is_restored_after_the_block_exits(self) -> None:
        # Un site installé (pas la retombée environnement, non stable
        # d'identité) pour vérifier que le contexte restaure bien la même
        # référence à la sortie du bloc.
        installed = site_fixture.build_test_site(site_id="ctx-installed")
        install(installed)
        site = site_fixture.build_test_site(site_id="ctx-restore")
        registry().register(site, replace=True)
        with use_site("ctx-restore"):
            self.assertIs(current(), site)
        self.assertIs(current(), installed)

    def test_unknown_site_id_fails_before_entering_the_block(self) -> None:
        entered = False
        with self.assertRaises(SiteConfigurationError):
            with use_site("never-registered"):
                entered = True
        self.assertFalse(entered)

    def test_context_overrides_the_installed_singleton(self) -> None:
        singleton = site_fixture.build_test_site(site_id="singleton-site")
        install(singleton)
        context_site = site_fixture.build_test_site(site_id="context-site")
        registry().register(context_site, replace=True)
        with use_site("context-site"):
            self.assertIs(current(), context_site)
        self.assertIs(current(), singleton)

    def test_nested_use_site_resolves_the_innermost_site(self) -> None:
        outer = site_fixture.build_test_site(site_id="outer-site")
        inner = site_fixture.build_test_site(site_id="inner-site")
        registry().register(outer, replace=True)
        registry().register(inner, replace=True)
        with use_site("outer-site"):
            self.assertIs(current(), outer)
            with use_site("inner-site"):
                self.assertIs(current(), inner)
            self.assertIs(current(), outer)

    def test_context_does_not_leak_across_threads(self) -> None:
        site = site_fixture.build_test_site(site_id="thread-site")
        registry().register(site, replace=True)
        other_thread_saw: dict[str, object] = {}
        entered = threading.Event()
        release = threading.Event()

        def worker() -> None:
            other_thread_saw["site_id"] = current().site_id
            entered.set()

        with use_site("thread-site"):
            thread = threading.Thread(target=worker)
            thread.start()
            entered.wait(timeout=2)
            thread.join(timeout=2)

        self.assertNotEqual(other_thread_saw.get("site_id"), "thread-site")


if __name__ == "__main__":
    unittest.main()
