"""Provider registry + the source capability matrix (evidence-dated, 14 Oct 2026).

RAILVISION_MODE=demo  -> DemoProvider only (all output labelled DEMO)
RAILVISION_MODE=live  -> only configured authorized providers; demo is refused at load time
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import (MANUAL_LINK, NEEDS_APPROVAL, NEEDS_LICENSE, UNSUPPORTED, VERIFIED_NOW,
                   NotSupported, ProviderAdapter)

NTES_URL = "https://enquiry.indianrail.gov.in/mntes/"
IRCTC_CHARTS_URL = "https://www.irctc.co.in/online-charts/"
IRCTC_VACANT_URL = "https://www.irctc.co.in/online-charts/trainclassvacantberth"

SOURCE_MATRIX = [
    {
        "source": "NTES (enquiry.indianrail.gov.in/mntes)", "status": "OFFICIAL (CRIS, Ministry of Railways)",
        "fields": "Running status, schedule, station boards, exceptional trains (cancelled/diverted/rescheduled)",
        "coverage": "National", "update_frequency": "Near real-time (exact cadence not published)",
        "auth": "Public web UI; no documented public API found",
        "terms": "robots policy disallows automated access (our fetch returned ROBOTS_DISALLOWED, 14 Oct 2026). No published redistribution licence.",
        "cost_limits": "Unknown / none published",
        "approval": NEEDS_APPROVAL + " — formal data-sharing arrangement with CRIS required for automation",
        "tests": "14 Oct 2026: WebFetch refused (robots disallow); sandbox egress proxy denied CONNECT. No automated access attempted further.",
        "integration": MANUAL_LINK, "url": NTES_URL,
    },
    {
        "source": "IRCTC Online Charts / Vacant Berth", "status": "OFFICIAL (IRCTC)",
        "fields": "Post-chart vacant berths by train/date/station/class/coach, berth-wise segments",
        "coverage": "National (reserved trains after charting)", "update_frequency": "At 1st/2nd chart preparation",
        "auth": "Public web UI with bot protection; no documented public API found",
        "terms": "Automated access blocked (bot detection response, 14 Oct 2026). Booking only via authorised IRCTC channels.",
        "cost_limits": "Unknown", "approval": NEEDS_APPROVAL + " — IRCTC partnership/authorised agent route",
        "tests": "14 Oct 2026: WebFetch blocked by bot detection. Not bypassed (by policy).",
        "integration": MANUAL_LINK, "url": IRCTC_CHARTS_URL,
    },
    {
        "source": "Licensed third-party railway API (e.g. providers built on NTES data)", "status": "THIRD-PARTY",
        "fields": "Varies: live status, route, between-stations, PNR, sometimes station boards",
        "coverage": "Varies", "update_frequency": "Provider-specific (some document ~45 s caching)",
        "auth": "API key", "terms": "Must verify the provider's own right to redistribute Indian Railways data before production use",
        "cost_limits": "Provider plans / per-minute quotas", "approval": NEEDS_LICENSE,
        "tests": "Adapter plumbing tested against a local mock server; NO live provider tested (no key, sandbox egress blocked).",
        "integration": "GenericHttpProvider + config/provider.example.json", "url": None,
    },
    {
        "source": "Meta WhatsApp Cloud API — 1:1", "status": "OFFICIAL (Meta)",
        "fields": "Inbound webhooks, outbound text in 24h session, approved templates outside it",
        "coverage": "Global", "update_frequency": "Real-time", "auth": "System-user access token + app secret",
        "terms": "Opt-in required; templates for business-initiated messages; per-message pricing",
        "cost_limits": "Meta pricing + messaging tier limits", "approval": NEEDS_APPROVAL + " (Meta Business + WABA + number)",
        "tests": "Webhook verify, HMAC-SHA256 signature, parsing, opt-in/out, replay guard, payload building unit-tested; live send untested (no credentials).",
        "integration": "delivery/whatsapp.py", "url": "https://developers.facebook.com/docs/whatsapp/cloud-api",
    },
    {
        "source": "Meta WhatsApp Groups API", "status": "OFFICIAL (Meta)",
        "fields": "Create invite-only groups, send text/media/templates",
        "coverage": "Global", "update_frequency": "Real-time", "auth": "Cloud API + Official Business Account (OBA)",
        "terms": "Meta docs (updated 16 Jun 2026): OBA only, max 8 participants, cannot post into arbitrary existing groups, per-message pricing",
        "cost_limits": "Per message", "approval": NEEDS_APPROVAL + " (OBA status)",
        "tests": "Eligibility gate + fallback unit-tested; endpoint calls untested.", "integration": "delivery/whatsapp.py (gated)",
        "url": "https://developers.facebook.com/documentation/business-messaging/whatsapp/groups/",
    },
    {
        "source": "WhatsApp Channels", "status": "No official publishing API found",
        "fields": "-", "coverage": "-", "update_frequency": "-", "auth": "-",
        "terms": "Only unofficial client-emulation gateways advertise channel posting; these are excluded by policy.",
        "cost_limits": "-", "approval": UNSUPPORTED + " (automatic) — " + MANUAL_LINK + " copy-ready admin queue",
        "tests": "Admin copy queue unit-tested.", "integration": "delivery/whatsapp.py ChannelCopyQueue", "url": None,
    },
    {
        "source": "Telegram Bot API", "status": "OFFICIAL (Telegram)", "fields": "Bot messages to users, groups, channels",
        "coverage": "Global", "update_frequency": "Real-time", "auth": "Bot token", "terms": "Free; bot must be added to group/channel",
        "cost_limits": "Free; ~30 msg/s global, ~20 msg/min per group", "approval": VERIFIED_NOW + " (needs your bot token)",
        "tests": "Payload/rate-limit unit-tested; live send untested in sandbox.", "integration": "delivery/telegram.py",
        "url": "https://core.telegram.org/bots/api",
    },
    {
        "source": "Browser assistant on your own computer (NTES + IRCTC official pages)", "status": "OFFICIAL PAGES, PERSONAL USE",
        "fields": "Whatever the official page shows: running status, schedule, delay, platform; vacant berths; train-name search",
        "coverage": "National (same as the official pages)", "update_frequency": "Your interval (min 5 min per train), one page at a time",
        "auth": "None for NTES; IRCTC may show bot checks — handed to you, never solved",
        "terms": "Automating these pages goes against NTES's robots policy and IRCTC's terms. Keep it to personal, low-rate use; do not offer it as a public service or redistribute the data.",
        "cost_limits": "Free; tool limits: 10 trains, >=15-20 s between page loads, 400 pages/day, 30 min-4 h pause after a block",
        "approval": VERIFIED_NOW + " for personal use on your computer (page layout to be confirmed on first run)",
        "tests": "End-to-end against a local mock site (10 trains, berth chart, CAPTCHA, block, teach+replay). Real NTES/IRCTC pages unreachable from the build environment, so real layouts are NOT yet verified.",
        "integration": "railvision/assist + services/assist_service.py", "url": NTES_URL,
    },
    {
        "source": "PIB press releases RSS (pib.gov.in)", "status": "OFFICIAL (Press Information Bureau)",
        "fields": "Title + link per release; all ministries mixed (no ministry field)", "coverage": "National",
        "update_frequency": "As released; RAILVISION reads hourly", "auth": "None (public RSS)",
        "terms": "RSS is published for automated readers; robots.txt is checked before fetching",
        "cost_limits": "Free", "approval": VERIFIED_NOW, "url": "https://www.pib.gov.in/ViewRss.aspx",
        "tests": "15 Oct 2026: feed structure checked (title + link only); parser, railway filter and alerts unit-tested with fixtures",
        "integration": "news/service.py (pib_en, pib_hi)",
    },
    {
        "source": "X (Twitter) — @RailMinIndia, @IRCTCofficial, @RailwaySeva, zonal accounts", "status": "OFFICIAL ACCOUNTS via OFFICIAL X API",
        "fields": "Recent posts (replies and reposts excluded)", "coverage": "Whatever the accounts post",
        "update_frequency": "Your interval (≥30 min)", "auth": "Your X developer bearer token",
        "terms": "Official API only; no scraping of x.com", "cost_limits": "Pay-per-use: ≈$0.005 per post read, $0.010 per user lookup "
        "(checked Oct 2026); daily post budget enforced (default 60 ≈ $0.30/day)", "approval": NEEDS_APPROVAL + " (your X developer account)",
        "tests": "Client, since_id paging and budget stop unit-tested against a mock API; no live call (no key)",
        "integration": "news/fetch.py XClient", "url": "https://docs.x.com/",
    },
    {
        "source": "Zonal railway websites (xx.indianrailways.gov.in) — press releases + the notice itself (page or PDF)",
        "status": "OFFICIAL PAGES",
        "fields": "New press-release links, then the notice text (HTML, or PDF via pypdf)", "coverage": "16 zones built in; add any page",
        "update_frequency": "Every 2 h (zones of your trips/stations switch on automatically)", "auth": "None",
        "terms": "15 Oct 2026: robots.txt of the zone pages refused our automated fetch tool. RAILVISION reads directly only where "
                 "robots.txt allows; otherwise you can let your own browser assistant read them like a person (personal, low rate).",
        "cost_limits": "Free; 5 notice details per page check", "approval": VERIFIED_NOW + " (7 zone addresses found by search; 9 follow the same pattern, unconfirmed)",
        "url": "https://nr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268",
        "tests": "Menu filtering, detail text, PDF text, robots refusal, migration, assistant list+detail+PDF end-to-end on a mock portal",
        "integration": "news/official.py, news/service.py, assist web_page recipe",
    },
    {
        "source": "Demo synthetic fixtures", "status": "INTERNAL / SYNTHETIC", "fields": "All modules (fake)",
        "coverage": "4 synthetic trains", "update_frequency": "Deterministic simulation", "auth": "-",
        "terms": "Never shown as live; refused in live mode", "cost_limits": "-", "approval": VERIFIED_NOW + " (DEMO ONLY)",
        "tests": "Full test suite", "integration": "providers/demo.py", "url": None,
    },
]


class OfficialLinkProvider(ProviderAdapter):
    """Manual-link fallback: every call returns the official page for the user to open."""
    source_id = "official-links"
    display_name = "Official pages (manual)"
    official = True
    capabilities = {k: MANUAL_LINK for k in ("train_status", "station_board", "trains_between", "chart_vacancy")}

    def get_train_status(self, train_number, service_date):
        raise NotSupported("Automated NTES access is not authorised; open NTES directly.", NTES_URL)

    def get_train_route(self, train_number, service_date):
        raise NotSupported("Automated NTES access is not authorised; open NTES directly.", NTES_URL)

    def get_station_board(self, station_code, start, end):
        raise NotSupported("Open the NTES station board.", NTES_URL)

    def find_trains_between(self, a, b, on):
        raise NotSupported("Open NTES trains-between-stations.", NTES_URL)

    def get_chart_vacancy(self, *a, **k):
        raise NotSupported("Open IRCTC online charts to view vacant berths.", IRCTC_VACANT_URL)

    def get_source_health(self):
        return {"source_id": self.source_id, "status": "MANUAL"}


class ProviderRegistry:
    def __init__(self, mode: str = "demo", providers: list[ProviderAdapter] | None = None):
        if mode not in ("demo", "live"):
            raise ValueError("mode must be demo|live")
        self.mode = mode
        self.providers: list[ProviderAdapter] = []
        for p in providers or []:
            self.add(p)
        self.fallback = OfficialLinkProvider()

    def add(self, p: ProviderAdapter):
        if self.mode == "live" and p.is_demo:
            raise ValueError("Refusing to register a DEMO provider in live mode")
        if self.mode == "demo" and not p.is_demo:
            raise ValueError("Demo mode accepts only demo providers (never mix demo and live)")
        self.providers.append(p)

    def primary(self) -> ProviderAdapter:
        return self.providers[0] if self.providers else self.fallback

    def call(self, method: str, *args, **kwargs):
        """Try providers in priority order; fall through on NotSupported; raise last error."""
        last: Exception | None = None
        for p in self.providers + [self.fallback]:
            try:
                return p, getattr(p, method)(*args, **kwargs)
            except NotSupported as e:
                last = e
                continue
        raise last or NotSupported("no provider")

    def health(self) -> list[dict]:
        return [p.get_source_health() for p in self.providers + [self.fallback]]

    @classmethod
    def from_env(cls, clock=None) -> "ProviderRegistry":
        mode = os.environ.get("RAILVISION_MODE", "demo")
        reg = cls(mode)
        if mode == "demo":
            from .demo import DemoProvider
            reg.add(DemoProvider(clock=clock) if clock else DemoProvider())
        else:
            cfg_path = os.environ.get("RAILVISION_PROVIDER_CONFIG")
            if cfg_path and Path(cfg_path).exists():
                from .generic_http import GenericHttpProvider
                for c in json.loads(Path(cfg_path).read_text()).get("providers", []):
                    if c.get("enabled"):
                        reg.add(GenericHttpProvider(c))
        return reg
