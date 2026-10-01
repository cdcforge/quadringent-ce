"""Relevés de stockage et allocation OpenCost, distincts de la facture AWS."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping

FORMAT = "quadringent-infrastructure-costs-v1"
UNAVAILABLE = {"status": "unavailable"}


def _time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("horodatage sans fuseau")
    return result.astimezone(timezone.utc)


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("mesure absente")
    if len(str(value)) > 96:
        raise ValueError("mesure trop longue")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result > Decimal("1e18") or result.as_tuple().exponent < -50:
        raise ValueError("mesure invalide")
    return result


def storage_cost(points, price_list, *, region, now):
    """Run-rate Standard au tarif public relevé ; ni facture ni projection d'usage."""
    try:
        point = max(points, key=lambda item: _time(item["Timestamp"]))
        at = _time(point["Timestamp"])
        if not timedelta(0) <= now - at <= timedelta(hours=72) or point["Unit"] != "Bytes":
            raise ValueError("mesure de stockage périmée")
        size = _decimal(point["Average"])
        gib = size / Decimal(1024**3)
        matches = []
        for sku, product in price_list["products"].items():
            attributes = product.get("attributes", {})
            if (
                product.get("productFamily") != "Storage"
                or attributes.get("regionCode") != region
                or attributes.get("volumeType") != "Standard"
                or not attributes.get("usagetype", "").endswith("TimedStorage-ByteHrs")
            ):
                continue
            for term in price_list["terms"]["OnDemand"][sku].values():
                for rate in term["priceDimensions"].values():
                    if rate["unit"] == "GB-Mo" and rate["beginRange"] == "0":
                        if gib > _decimal(rate["endRange"]):
                            raise ValueError("volume au-delà de la tranche prise en charge")
                        matches.append(_decimal(rate["pricePerUnit"]["USD"]))
        if len(matches) != 1:
            raise ValueError("tarif absent ou ambigu")
        rate = matches[0]
        return {
            "status": "measured",
            "observed_at": at.isoformat(),
            "bytes": format(size, "f"),
            "price_per_gib_month": format(rate, "f"),
            "monthly_run_rate": format(gib * rate, "f"),
            "currency": "USD",
            "basis": "aws_public_standard_first_tier",
        }
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return dict(UNAVAILABLE)


def cluster_cost(allocation, assets, *, namespace, cluster_id, currency, start, end):
    """Namespace sans partage de l'inutilisé, avec actifs et inutilisé en contexte."""
    try:
        if not re.fullmatch(r"[A-Z]{3}", currency) or end - start != timedelta(days=1):
            raise ValueError("périmètre invalide")
        for response in (allocation, assets):
            if response.get("code") != 200 or response.get("warnings") or response.get("errors"):
                raise ValueError("réponse partielle")
        if len(allocation["data"]) != 1:
            raise ValueError("fenêtre non accumulée")
        rows = allocation["data"][0]
        selected = [
            v
            for v in rows.values()
            if v["properties"].get("namespace") == namespace and v["properties"].get("cluster") == cluster_id
        ]
        context = [v for v in assets["data"].values() if v["properties"].get("cluster") == cluster_id]
        idle = [v for k, v in rows.items() if k.endswith("__idle__") and v["properties"].get("cluster") == cluster_id]
        if len(selected) != 1 or not context:
            raise ValueError("namespace ou cluster absent")
        for item in selected + context + idle:
            if _time(item["window"]["start"]) != start or _time(item["window"]["end"]) != end:
                raise ValueError("fenêtres différentes")
        # Une absence d'inutilisé ne signifie pas un zéro mesuré.
        return {
            "status": "measured",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "allocated_amount": format(_decimal(selected[0]["totalCost"]), "f"),
            "cluster_amount": format(sum((_decimal(v["totalCost"]) for v in context), Decimal(0)), "f"),
            "idle_amount": format(sum((_decimal(v["totalCost"]) for v in idle), Decimal(0)), "f") if idle else None,
            "currency": currency,
            "basis": "opencost_no_idle_share",
            "cluster_id": cluster_id,
        }
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return dict(UNAVAILABLE)


def project_infrastructure_costs(proof, *, site, now, evidence_kind):
    """La lecture d'un autre site ou d'un document ancien ne confirme aucun coût."""
    try:
        if (
            not isinstance(proof, Mapping)
            or proof.get("format_version") != FORMAT
            or evidence_kind != "live"
            or proof.get("evidence_kind") != "live"
            or not site.cost_namespace
            or any(
                proof.get(key) != expected
                for key, expected in {
                    "site_id": site.site_id,
                    "environment": site.environment,
                    "namespace": site.cost_namespace,
                    "bucket": site.raw_bucket,
                    "region": site.aws_region,
                }.items()
            )
        ):
            raise ValueError("preuve hors périmètre")
        collected = _time(proof["collected_at"])
        if not timedelta(0) <= now - collected <= timedelta(hours=26):
            raise ValueError("collecte ancienne")
        return {
            "status": "available",
            "collected_at": collected.isoformat(),
            "namespace": site.cost_namespace,
            "storage": _project_storage(proof.get("storage"), now, collected),
            "cluster": _project_cluster(proof.get("cluster"), now, collected),
        }
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return {"status": "unavailable", "reason": "cost_evidence_unavailable"}


def _project_storage(storage, now, collected):
    try:
        if storage.get("status") == "measured":
            at = _time(storage["observed_at"])
            if not timedelta(0) <= now - at <= timedelta(hours=72) or at > collected:
                raise ValueError("stockage ancien")
            if storage["basis"] != "aws_public_standard_first_tier" or storage["currency"] != "USD":
                raise ValueError("tarif inconnu")
            size, price, amount = (
                _decimal(storage[key]) for key in ("bytes", "price_per_gib_month", "monthly_run_rate")
            )
            if size / Decimal(1024**3) * price != amount:
                raise ValueError("montant incohérent")
            storage = {
                "status": "measured",
                "observed_at": at.isoformat(),
                "bytes": format(size, "f"),
                "price_per_gib_month": format(price, "f"),
                "monthly_run_rate": format(amount, "f"),
                "currency": "USD",
                "basis": storage["basis"],
            }
        else:
            storage = dict(UNAVAILABLE)
        return storage
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return dict(UNAVAILABLE)


def _project_cluster(cluster, now, collected):
    try:
        if cluster.get("status") == "measured":
            start, end = _time(cluster["start"]), _time(cluster["end"])
            if end - start != timedelta(days=1) or end > collected or now - end > timedelta(hours=72):
                raise ValueError("fenêtre cluster invalide")
            if cluster["basis"] != "opencost_no_idle_share" or not re.fullmatch(r"[A-Z]{3}", cluster["currency"]):
                raise ValueError("modèle non déclaré")
            cluster = {
                "status": "measured",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "basis": cluster["basis"],
                "currency": cluster["currency"],
                **{key: format(_decimal(cluster[key]), "f") for key in ("allocated_amount", "cluster_amount")},
                "idle_amount": None
                if cluster.get("idle_amount") is None
                else format(_decimal(cluster["idle_amount"]), "f"),
            }
        else:
            cluster = dict(UNAVAILABLE)
        return cluster
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return dict(UNAVAILABLE)
