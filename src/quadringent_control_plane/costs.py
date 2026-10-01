"""Conversion au tarif du site, sans accès supplémentaire à la facturation."""
from datetime import datetime
from decimal import Decimal
import math
import re

from quadringent.site_config import SiteConfig
from .model import ObservabilityProjection, SourceDescriptor


def project_costs(observation: ObservabilityProjection, source: SourceDescriptor,
                  now: datetime, *, site: SiteConfig) -> dict[str, object] | None:
    if source.environment != site.environment or source.id not in {
        site.site_id, site.pipeline_id, site.sidecar_pipeline_id, site.fleet_id,
    }:
        return None
    result: dict[str, object] = {
        "scope": "warehouse", "warehouse": site.warehouse_name,
        "price_per_credit": site.snowflake_credit_price, "currency": site.cost_currency,
        "amount": None,
    }
    check = next((item for item in observation.checks if item.id == "snowflake_credits"), None)
    if (check is None or check.status == "unobserved" or check.unit != "warehousecredits/delayed24h"
        or type(check.observed) not in (int, float) or not math.isfinite(check.observed)
        or check.observed < 0 or site.snowflake_credit_price is None
        or observation.quality.get("freshness") != "fresh"
        or observation.quality.get("evidence_kind") != "live" or source.evidence_kind != "live"):
        return result
    window = re.fullmatch(r"metering_window_(\d{10})_(\d{10})_([1-9]\d{0,3})", check.reason)
    if window is None:
        return result
    start, end, rows = map(int, window.groups())
    if end - start != 86400 or end > now.timestamp() - 21600 or rows > 1000:
        return result
    result["amount"] = format(Decimal(str(check.observed)) * Decimal(site.snowflake_credit_price), "f")
    return result
