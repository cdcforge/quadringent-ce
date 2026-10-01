"""Le prix appartient au site ; une absence de mesure ne devient jamais zéro."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from site_fixture import build_test_site, TEST_SITE_ENV
from quadringent.site_config import from_environment, SiteConfigurationError
from quadringent_control_plane.costs import project_costs
from quadringent_control_plane.model import ObservabilityProjection, SloCheckProjection, SourceDescriptor

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
END = int(NOW.timestamp()) - 6 * 3600


def observation(value=2.5, *, freshness="fresh", kind="live", reason=None):
    return ObservabilityProjection("pass", {"freshness": freshness, "evidence_kind": kind}, NOW.isoformat(), "within_policy",
        (SloCheckProjection("snowflake_credits", "cost", "pass", value, 10, "warehousecredits/delayed24h",
         reason or f"metering_window_{END - 86400}_{END}_24"),), ())


def source(site):
    return SourceDescriptor(site.pipeline_id, "live", site.environment, "file:///example.json")


@pytest.mark.parametrize("price,currency", [("-1", "EUR"), ("NaN", "EUR"), ("Infinity", "USD"), ("2", ""), ("", "EUR"), ("2", "euro")])
def test_refuse_un_prix_ou_une_devise_incoherents(price, currency):
    with pytest.raises(SiteConfigurationError):
        from_environment({**TEST_SITE_ENV, "QUADRINGENT_SNOWFLAKE_CREDIT_PRICE": price, "QUADRINGENT_COST_CURRENCY": currency})


def test_convertit_uniquement_les_credits_mesures_avec_le_prix_declare():
    site = build_test_site(snowflake_credit_price="3.10", cost_currency="EUR")
    result = project_costs(observation(), source(site), NOW, site=site)
    assert result["amount"] == "7.750"
    assert result["warehouse"] == site.warehouse_name
    assert result["scope"] == "warehouse"
    assert project_costs(observation(0), source(site), NOW, site=site)["amount"] == "0.00"


@pytest.mark.parametrize("obs", [observation(None), observation(freshness="stale"), observation(kind="simulation"), observation(kind="historical"), observation(reason="within_threshold")])
def test_aucun_montant_sans_mesure_actuelle_et_fenetre(obs):
    site = build_test_site(snowflake_credit_price="2", cost_currency="USD")
    assert project_costs(obs, source(site), NOW, site=site)["amount"] is None


def test_prix_absent_et_zero_declare_sont_distincts():
    site = build_test_site()
    assert project_costs(observation(), source(site), NOW, site=site)["amount"] is None
    gratuit = replace(site, snowflake_credit_price="0", cost_currency="EUR")
    assert project_costs(observation(), source(gratuit), NOW, site=gratuit)["amount"] == "0.0"


def test_un_prix_ne_passe_pas_sur_une_autre_liaison():
    site = build_test_site(snowflake_credit_price="2", cost_currency="EUR")
    autre = SourceDescriptor("other-site", "live", site.environment, "file:///other.json")
    assert project_costs(observation(), autre, NOW, site=site) is None


def test_deux_contextes_concurrents_ne_partagent_pas_leur_tarif():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from quadringent.site_config import registry, use_site, current
    sites = [build_test_site(site_id="tarif-a", snowflake_credit_price="2", cost_currency="EUR"),
             build_test_site(site_id="tarif-b", snowflake_credit_price="4", cost_currency="USD")]
    barrier = Barrier(2)
    def project(site):
        with use_site(site.site_id):
            barrier.wait(timeout=5)
            resolved = current()
            return project_costs(observation(), source(resolved), NOW, site=resolved)
    try:
        for site in sites:
            registry().register(site)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(project, sites))
        assert [result["amount"] for result in results] == ["5.0", "10.0"]
        assert [result["currency"] for result in results] == ["EUR", "USD"]
    finally:
        for site in sites:
            registry().unregister(site.site_id)
