"""Pure Stage 2 BASE-01 and EMAIL-01..04 evaluation of trusted snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from string import Formatter
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EMAIL_USES = {"acquisition_email", "transactional_email"}
REASONS = {
    "BASE-01": "evidence_missing_or_scope_unknown",
    "EMAIL-01": "email_header_or_subject_invalid",
    "EMAIL-02": "email_address_or_ad_disclosure_missing",
    "EMAIL-03": "email_optout_mechanism_invalid",
    "EMAIL-04": "email_optout_or_suppression_match",
}


class ComplianceContentError(ValueError):
    """A message cannot be rendered solely from approved template fields."""


@dataclass(frozen=True)
class ComplianceResult:
    eligible: bool
    evidence: dict[str, Any]


def evidence_hash(record: Any) -> str:
    return hashlib.sha256(json.dumps(
        record, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def registered_channel(config: dict[str, Any], requested_use: str | None) -> str | None:
    """Only an explicitly registered email use establishes email scope."""
    if isinstance(requested_use, str) and requested_use in config.get("desired_use", []) and requested_use in EMAIL_USES:
        return "email"
    return None


def is_email_integration(request: Any, registry: dict[str, Any]) -> bool:
    """True for any request naming an integration registered for an email use, in any phase."""
    if not isinstance(request, dict) or not isinstance(request.get("name"), str):
        return False
    config = registry["integrations"].get(request["name"], {})
    return any(use in EMAIL_USES for use in config.get("desired_use", []))


def is_email_execute(request: Any, registry: dict[str, Any]) -> bool:
    """Classify by registered capability, never let a requested use bypass the gate."""
    return is_email_integration(request, registry) and request.get("phase") == "execute"


def render_message(message: dict, template: dict, sender: dict, contact: dict) -> dict:
    """Render literal named placeholders, never format expressions or headers."""
    if not isinstance(sender, dict) or not isinstance(contact, dict):
        raise ComplianceContentError("sender and contact records must be objects")
    if not isinstance(message, dict) or set(message) != {"template_id", "fills"}:
        raise ComplianceContentError("template_id and fills are required")
    if not isinstance(template, dict) or message["template_id"] != template.get("template_id"):
        raise ComplianceContentError("unknown template")
    fills = message["fills"]
    placeholders = template.get("placeholders")
    if (not isinstance(fills, dict) or not isinstance(placeholders, list)
            or any(not isinstance(p, str) or not p.isidentifier() for p in placeholders)
            or set(fills) != set(placeholders)
            or any(not isinstance(v, str) for v in fills.values())):
        raise ComplianceContentError("fills must exactly match declared placeholders")
    body = template.get("body_template")
    if not isinstance(body, str):
        raise ComplianceContentError("template body missing")
    try:
        parts = list(Formatter().parse(body))
    except ValueError as exc:
        raise ComplianceContentError("invalid template placeholders") from exc
    if (any(name is not None and (name not in placeholders or spec or conversion)
            for _, name, spec, conversion in parts)
            or {name for _, name, _, _ in parts if name is not None} != set(placeholders)):
        raise ComplianceContentError("template contains undeclared or expressive placeholders")
    rendered_body = "".join(literal + (fills[name] if name is not None else "")
                            for literal, name, _, _ in parts)
    return {
        "template_id": message["template_id"], "fills": deepcopy(fills),
        "from_address": sender.get("from_address"), "to_address": contact.get("address"),
        "reply_to": sender.get("reply_to"), "sending_domain": sender.get("sending_domain"),
        "subject": template.get("subject"), "body": rendered_body,
        "ad_disclosure": template.get("ad_disclosure"),
        "postal_address": sender.get("postal_address"),
        "opt_out_route": deepcopy(template.get("opt_out_route")),
    }


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _enum(value: Any, options: frozenset[str]) -> str | None:
    """A malformed (non-string, e.g. list) fact must fail the predicate, never raise."""
    return value if isinstance(value, str) and value in options else None


def _record(value: Any) -> dict[str, Any]:
    """A nested trusted-record fact must be a mapping; any other shape is malformed."""
    return value if isinstance(value, dict) else {}


def _dispatch_method(value: Any) -> str | None:
    """A dispatch/consent method fact must be a known string; any other shape is malformed."""
    return _enum(value, frozenset({"manual", "automated"}))


RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:[0-5]\d:[0-5]\d(\.\d+)?(Z|[+-]\d{2}:[0-5]\d)")
# Pinned to the event schema's recipient_time_zone enum
# (tests/fixtures/company_os/acqivo/schemas/sandbox_event.schema.json), not
# derived from the host's live tzdata -- a host's available_timezones() is
# not guaranteed to match the schema's fixed contract.
CANONICAL_ZONES = frozenset({
    'Africa/Abidjan', 'Africa/Accra', 'Africa/Addis_Ababa', 'Africa/Algiers',
    'Africa/Asmara', 'Africa/Asmera', 'Africa/Bamako', 'Africa/Bangui',
    'Africa/Banjul', 'Africa/Bissau', 'Africa/Blantyre', 'Africa/Brazzaville',
    'Africa/Bujumbura', 'Africa/Cairo', 'Africa/Casablanca', 'Africa/Ceuta',
    'Africa/Conakry', 'Africa/Dakar', 'Africa/Dar_es_Salaam', 'Africa/Djibouti',
    'Africa/Douala', 'Africa/El_Aaiun', 'Africa/Freetown', 'Africa/Gaborone',
    'Africa/Harare', 'Africa/Johannesburg', 'Africa/Juba', 'Africa/Kampala',
    'Africa/Khartoum', 'Africa/Kigali', 'Africa/Kinshasa', 'Africa/Lagos',
    'Africa/Libreville', 'Africa/Lome', 'Africa/Luanda', 'Africa/Lubumbashi',
    'Africa/Lusaka', 'Africa/Malabo', 'Africa/Maputo', 'Africa/Maseru',
    'Africa/Mbabane', 'Africa/Mogadishu', 'Africa/Monrovia', 'Africa/Nairobi',
    'Africa/Ndjamena', 'Africa/Niamey', 'Africa/Nouakchott', 'Africa/Ouagadougou',
    'Africa/Porto-Novo', 'Africa/Sao_Tome', 'Africa/Timbuktu', 'Africa/Tripoli',
    'Africa/Tunis', 'Africa/Windhoek', 'America/Adak', 'America/Anchorage',
    'America/Anguilla', 'America/Antigua', 'America/Araguaina', 'America/Argentina/Buenos_Aires',
    'America/Argentina/Catamarca', 'America/Argentina/ComodRivadavia', 'America/Argentina/Cordoba', 'America/Argentina/Jujuy',
    'America/Argentina/La_Rioja', 'America/Argentina/Mendoza', 'America/Argentina/Rio_Gallegos', 'America/Argentina/Salta',
    'America/Argentina/San_Juan', 'America/Argentina/San_Luis', 'America/Argentina/Tucuman', 'America/Argentina/Ushuaia',
    'America/Aruba', 'America/Asuncion', 'America/Atikokan', 'America/Atka',
    'America/Bahia', 'America/Bahia_Banderas', 'America/Barbados', 'America/Belem',
    'America/Belize', 'America/Blanc-Sablon', 'America/Boa_Vista', 'America/Bogota',
    'America/Boise', 'America/Buenos_Aires', 'America/Cambridge_Bay', 'America/Campo_Grande',
    'America/Cancun', 'America/Caracas', 'America/Catamarca', 'America/Cayenne',
    'America/Cayman', 'America/Chicago', 'America/Chihuahua', 'America/Ciudad_Juarez',
    'America/Coral_Harbour', 'America/Cordoba', 'America/Costa_Rica', 'America/Coyhaique',
    'America/Creston', 'America/Cuiaba', 'America/Curacao', 'America/Danmarkshavn',
    'America/Dawson', 'America/Dawson_Creek', 'America/Denver', 'America/Detroit',
    'America/Dominica', 'America/Edmonton', 'America/Eirunepe', 'America/El_Salvador',
    'America/Ensenada', 'America/Fort_Nelson', 'America/Fort_Wayne', 'America/Fortaleza',
    'America/Glace_Bay', 'America/Godthab', 'America/Goose_Bay', 'America/Grand_Turk',
    'America/Grenada', 'America/Guadeloupe', 'America/Guatemala', 'America/Guayaquil',
    'America/Guyana', 'America/Halifax', 'America/Havana', 'America/Hermosillo',
    'America/Indiana/Indianapolis', 'America/Indiana/Knox', 'America/Indiana/Marengo', 'America/Indiana/Petersburg',
    'America/Indiana/Tell_City', 'America/Indiana/Vevay', 'America/Indiana/Vincennes', 'America/Indiana/Winamac',
    'America/Indianapolis', 'America/Inuvik', 'America/Iqaluit', 'America/Jamaica',
    'America/Jujuy', 'America/Juneau', 'America/Kentucky/Louisville', 'America/Kentucky/Monticello',
    'America/Knox_IN', 'America/Kralendijk', 'America/La_Paz', 'America/Lima',
    'America/Los_Angeles', 'America/Louisville', 'America/Lower_Princes', 'America/Maceio',
    'America/Managua', 'America/Manaus', 'America/Marigot', 'America/Martinique',
    'America/Matamoros', 'America/Mazatlan', 'America/Mendoza', 'America/Menominee',
    'America/Merida', 'America/Metlakatla', 'America/Mexico_City', 'America/Miquelon',
    'America/Moncton', 'America/Monterrey', 'America/Montevideo', 'America/Montreal',
    'America/Montserrat', 'America/Nassau', 'America/New_York', 'America/Nipigon',
    'America/Nome', 'America/Noronha', 'America/North_Dakota/Beulah', 'America/North_Dakota/Center',
    'America/North_Dakota/New_Salem', 'America/Nuuk', 'America/Ojinaga', 'America/Panama',
    'America/Pangnirtung', 'America/Paramaribo', 'America/Phoenix', 'America/Port-au-Prince',
    'America/Port_of_Spain', 'America/Porto_Acre', 'America/Porto_Velho', 'America/Puerto_Rico',
    'America/Punta_Arenas', 'America/Rainy_River', 'America/Rankin_Inlet', 'America/Recife',
    'America/Regina', 'America/Resolute', 'America/Rio_Branco', 'America/Rosario',
    'America/Santa_Isabel', 'America/Santarem', 'America/Santiago', 'America/Santo_Domingo',
    'America/Sao_Paulo', 'America/Scoresbysund', 'America/Shiprock', 'America/Sitka',
    'America/St_Barthelemy', 'America/St_Johns', 'America/St_Kitts', 'America/St_Lucia',
    'America/St_Thomas', 'America/St_Vincent', 'America/Swift_Current', 'America/Tegucigalpa',
    'America/Thule', 'America/Thunder_Bay', 'America/Tijuana', 'America/Toronto',
    'America/Tortola', 'America/Vancouver', 'America/Virgin', 'America/Whitehorse',
    'America/Winnipeg', 'America/Yakutat', 'America/Yellowknife', 'Antarctica/Casey',
    'Antarctica/Davis', 'Antarctica/DumontDUrville', 'Antarctica/Macquarie', 'Antarctica/Mawson',
    'Antarctica/McMurdo', 'Antarctica/Palmer', 'Antarctica/Rothera', 'Antarctica/South_Pole',
    'Antarctica/Syowa', 'Antarctica/Troll', 'Antarctica/Vostok', 'Arctic/Longyearbyen',
    'Asia/Aden', 'Asia/Almaty', 'Asia/Amman', 'Asia/Anadyr',
    'Asia/Aqtau', 'Asia/Aqtobe', 'Asia/Ashgabat', 'Asia/Ashkhabad',
    'Asia/Atyrau', 'Asia/Baghdad', 'Asia/Bahrain', 'Asia/Baku',
    'Asia/Bangkok', 'Asia/Barnaul', 'Asia/Beirut', 'Asia/Bishkek',
    'Asia/Brunei', 'Asia/Calcutta', 'Asia/Chita', 'Asia/Choibalsan',
    'Asia/Chongqing', 'Asia/Chungking', 'Asia/Colombo', 'Asia/Dacca',
    'Asia/Damascus', 'Asia/Dhaka', 'Asia/Dili', 'Asia/Dubai',
    'Asia/Dushanbe', 'Asia/Famagusta', 'Asia/Gaza', 'Asia/Harbin',
    'Asia/Hebron', 'Asia/Ho_Chi_Minh', 'Asia/Hong_Kong', 'Asia/Hovd',
    'Asia/Irkutsk', 'Asia/Istanbul', 'Asia/Jakarta', 'Asia/Jayapura',
    'Asia/Jerusalem', 'Asia/Kabul', 'Asia/Kamchatka', 'Asia/Karachi',
    'Asia/Kashgar', 'Asia/Kathmandu', 'Asia/Katmandu', 'Asia/Khandyga',
    'Asia/Kolkata', 'Asia/Krasnoyarsk', 'Asia/Kuala_Lumpur', 'Asia/Kuching',
    'Asia/Kuwait', 'Asia/Macao', 'Asia/Macau', 'Asia/Magadan',
    'Asia/Makassar', 'Asia/Manila', 'Asia/Muscat', 'Asia/Nicosia',
    'Asia/Novokuznetsk', 'Asia/Novosibirsk', 'Asia/Omsk', 'Asia/Oral',
    'Asia/Phnom_Penh', 'Asia/Pontianak', 'Asia/Pyongyang', 'Asia/Qatar',
    'Asia/Qostanay', 'Asia/Qyzylorda', 'Asia/Rangoon', 'Asia/Riyadh',
    'Asia/Saigon', 'Asia/Sakhalin', 'Asia/Samarkand', 'Asia/Seoul',
    'Asia/Shanghai', 'Asia/Singapore', 'Asia/Srednekolymsk', 'Asia/Taipei',
    'Asia/Tashkent', 'Asia/Tbilisi', 'Asia/Tehran', 'Asia/Tel_Aviv',
    'Asia/Thimbu', 'Asia/Thimphu', 'Asia/Tokyo', 'Asia/Tomsk',
    'Asia/Ujung_Pandang', 'Asia/Ulaanbaatar', 'Asia/Ulan_Bator', 'Asia/Urumqi',
    'Asia/Ust-Nera', 'Asia/Vientiane', 'Asia/Vladivostok', 'Asia/Yakutsk',
    'Asia/Yangon', 'Asia/Yekaterinburg', 'Asia/Yerevan', 'Atlantic/Azores',
    'Atlantic/Bermuda', 'Atlantic/Canary', 'Atlantic/Cape_Verde', 'Atlantic/Faeroe',
    'Atlantic/Faroe', 'Atlantic/Jan_Mayen', 'Atlantic/Madeira', 'Atlantic/Reykjavik',
    'Atlantic/South_Georgia', 'Atlantic/St_Helena', 'Atlantic/Stanley', 'Australia/ACT',
    'Australia/Adelaide', 'Australia/Brisbane', 'Australia/Broken_Hill', 'Australia/Canberra',
    'Australia/Currie', 'Australia/Darwin', 'Australia/Eucla', 'Australia/Hobart',
    'Australia/LHI', 'Australia/Lindeman', 'Australia/Lord_Howe', 'Australia/Melbourne',
    'Australia/NSW', 'Australia/North', 'Australia/Perth', 'Australia/Queensland',
    'Australia/South', 'Australia/Sydney', 'Australia/Tasmania', 'Australia/Victoria',
    'Australia/West', 'Australia/Yancowinna', 'Brazil/Acre', 'Brazil/DeNoronha',
    'Brazil/East', 'Brazil/West', 'CET', 'CST6CDT',
    'Canada/Atlantic', 'Canada/Central', 'Canada/Eastern', 'Canada/Mountain',
    'Canada/Newfoundland', 'Canada/Pacific', 'Canada/Saskatchewan', 'Canada/Yukon',
    'Chile/Continental', 'Chile/EasterIsland', 'Cuba', 'EET',
    'EST', 'EST5EDT', 'Egypt', 'Eire',
    'Etc/GMT', 'Etc/GMT+0', 'Etc/GMT+1', 'Etc/GMT+10',
    'Etc/GMT+11', 'Etc/GMT+12', 'Etc/GMT+2', 'Etc/GMT+3',
    'Etc/GMT+4', 'Etc/GMT+5', 'Etc/GMT+6', 'Etc/GMT+7',
    'Etc/GMT+8', 'Etc/GMT+9', 'Etc/GMT-0', 'Etc/GMT-1',
    'Etc/GMT-10', 'Etc/GMT-11', 'Etc/GMT-12', 'Etc/GMT-13',
    'Etc/GMT-14', 'Etc/GMT-2', 'Etc/GMT-3', 'Etc/GMT-4',
    'Etc/GMT-5', 'Etc/GMT-6', 'Etc/GMT-7', 'Etc/GMT-8',
    'Etc/GMT-9', 'Etc/GMT0', 'Etc/Greenwich', 'Etc/UCT',
    'Etc/UTC', 'Etc/Universal', 'Etc/Zulu', 'Europe/Amsterdam',
    'Europe/Andorra', 'Europe/Astrakhan', 'Europe/Athens', 'Europe/Belfast',
    'Europe/Belgrade', 'Europe/Berlin', 'Europe/Bratislava', 'Europe/Brussels',
    'Europe/Bucharest', 'Europe/Budapest', 'Europe/Busingen', 'Europe/Chisinau',
    'Europe/Copenhagen', 'Europe/Dublin', 'Europe/Gibraltar', 'Europe/Guernsey',
    'Europe/Helsinki', 'Europe/Isle_of_Man', 'Europe/Istanbul', 'Europe/Jersey',
    'Europe/Kaliningrad', 'Europe/Kiev', 'Europe/Kirov', 'Europe/Kyiv',
    'Europe/Lisbon', 'Europe/Ljubljana', 'Europe/London', 'Europe/Luxembourg',
    'Europe/Madrid', 'Europe/Malta', 'Europe/Mariehamn', 'Europe/Minsk',
    'Europe/Monaco', 'Europe/Moscow', 'Europe/Nicosia', 'Europe/Oslo',
    'Europe/Paris', 'Europe/Podgorica', 'Europe/Prague', 'Europe/Riga',
    'Europe/Rome', 'Europe/Samara', 'Europe/San_Marino', 'Europe/Sarajevo',
    'Europe/Saratov', 'Europe/Simferopol', 'Europe/Skopje', 'Europe/Sofia',
    'Europe/Stockholm', 'Europe/Tallinn', 'Europe/Tirane', 'Europe/Tiraspol',
    'Europe/Ulyanovsk', 'Europe/Uzhgorod', 'Europe/Vaduz', 'Europe/Vatican',
    'Europe/Vienna', 'Europe/Vilnius', 'Europe/Volgograd', 'Europe/Warsaw',
    'Europe/Zagreb', 'Europe/Zaporozhye', 'Europe/Zurich', 'Factory',
    'GB', 'GB-Eire', 'GMT', 'GMT+0',
    'GMT-0', 'GMT0', 'Greenwich', 'HST',
    'Hongkong', 'Iceland', 'Indian/Antananarivo', 'Indian/Chagos',
    'Indian/Christmas', 'Indian/Cocos', 'Indian/Comoro', 'Indian/Kerguelen',
    'Indian/Mahe', 'Indian/Maldives', 'Indian/Mauritius', 'Indian/Mayotte',
    'Indian/Reunion', 'Iran', 'Israel', 'Jamaica',
    'Japan', 'Kwajalein', 'Libya', 'MET',
    'MST', 'MST7MDT', 'Mexico/BajaNorte', 'Mexico/BajaSur',
    'Mexico/General', 'NZ', 'NZ-CHAT', 'Navajo',
    'PRC', 'PST8PDT', 'Pacific/Apia', 'Pacific/Auckland',
    'Pacific/Bougainville', 'Pacific/Chatham', 'Pacific/Chuuk', 'Pacific/Easter',
    'Pacific/Efate', 'Pacific/Enderbury', 'Pacific/Fakaofo', 'Pacific/Fiji',
    'Pacific/Funafuti', 'Pacific/Galapagos', 'Pacific/Gambier', 'Pacific/Guadalcanal',
    'Pacific/Guam', 'Pacific/Honolulu', 'Pacific/Johnston', 'Pacific/Kanton',
    'Pacific/Kiritimati', 'Pacific/Kosrae', 'Pacific/Kwajalein', 'Pacific/Majuro',
    'Pacific/Marquesas', 'Pacific/Midway', 'Pacific/Nauru', 'Pacific/Niue',
    'Pacific/Norfolk', 'Pacific/Noumea', 'Pacific/Pago_Pago', 'Pacific/Palau',
    'Pacific/Pitcairn', 'Pacific/Pohnpei', 'Pacific/Ponape', 'Pacific/Port_Moresby',
    'Pacific/Rarotonga', 'Pacific/Saipan', 'Pacific/Samoa', 'Pacific/Tahiti',
    'Pacific/Tarawa', 'Pacific/Tongatapu', 'Pacific/Truk', 'Pacific/Wake',
    'Pacific/Wallis', 'Pacific/Yap', 'Poland', 'Portugal',
    'ROC', 'ROK', 'Singapore', 'Turkey',
    'UCT', 'US/Alaska', 'US/Aleutian', 'US/Arizona',
    'US/Central', 'US/East-Indiana', 'US/Eastern', 'US/Hawaii',
    'US/Indiana-Starke', 'US/Michigan', 'US/Mountain', 'US/Pacific',
    'US/Samoa', 'UTC', 'Universal', 'W-SU',
    'WET', 'Zulu',
})


def _time(value: Any) -> datetime | None:
    try:
        parsed = (datetime.fromisoformat(value)
                  if isinstance(value, str) and RFC3339.fullmatch(value) else None)
        return parsed if parsed is not None and parsed.tzinfo is not None else None
    except ValueError:
        return None


def evaluate_outbound_eligibility(context: dict[str, Any]) -> ComplianceResult:
    """Evaluate without mutating input, reading a clock, or consulting services."""
    c = deepcopy(context)
    sender, contact = _record(c.get("sender")), _record(c.get("contact"))
    consent, suppression = _record(c.get("consent")), _record(c.get("suppression"))
    template, payload = _record(c.get("template")), _record(c.get("rendered_payload"))
    now = _time(c.get("evaluated_at"))
    zone = contact.get("recipient_time_zone")
    try:
        zone_info = ZoneInfo(zone) if isinstance(zone, str) and zone in CANONICAL_ZONES else None
    except (ZoneInfoNotFoundError, ValueError):
        zone_info = None
    zone = zone if zone_info is not None else None
    try:
        local_time = now.astimezone(zone_info).isoformat() if now and zone_info else None
    except (OverflowError, ValueError, OSError):
        local_time = None
    # A historical zone can have a non-round-minute UTC offset (seconds), which
    # isoformat emits but our RFC 3339 evidence contract (and the event schema)
    # does not accept; such a fact cannot be recorded and must fail closed.
    if local_time is not None and not RFC3339.fullmatch(local_time):
        local_time = None
    location = contact.get("recipient_location") or {}
    location = location if isinstance(location, dict) else {}
    def timestamp(value):
        parsed = _time(value)
        return now is not None and parsed is not None and parsed <= now

    consent_matches = (
        _text(c.get("recipient_id")) and consent.get("recipient_id") == c.get("recipient_id")
        and _text(consent.get("contact_email")) and _text(contact.get("address"))
        and consent["contact_email"].strip().casefold() == contact["address"].strip().casefold()
        and _dispatch_method(consent.get("method")) is not None
        and _dispatch_method(consent.get("method")) == _dispatch_method(sender.get("dispatch_method"))
    )
    suppressed = suppression.get("suppressed") is not False
    trusted = (
        c.get("channel") == "email" and c.get("purpose") == "commercial"
        and _dispatch_method(sender.get("dispatch_method")) is not None
        and all(_text(sender.get(k)) for k in (
            "sender_id", "from_address", "reply_to", "sending_domain", "postal_address", "seller_name"))
        and timestamp(sender.get("captured_at")) and timestamp(contact.get("captured_at"))
        and _text(c.get("recipient_id")) and contact.get("recipient_id") == c.get("recipient_id")
        and _text(contact.get("address")) and "@" in contact["address"]
        and all(_text(location.get(k)) for k in ("country", "state")) and local_time is not None
        and consent_matches and consent.get("seller") == sender.get("seller_name")
        and consent.get("channel") == "email" and consent.get("purpose") == "commercial"
        and timestamp(consent.get("captured_at")) and "revoked_at" in consent
        and consent["revoked_at"] is None and contact.get("consent_verified") is True
        and timestamp(suppression.get("checked_at")) and _text(suppression.get("version"))
        and not suppressed and bool(payload) and c.get("scope_valid", True) is True
        and c.get("policy_version") == "0.1.0"
        and isinstance(c.get("policy_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", c["policy_sha256"]) is not None
    )
    checks = {"BASE-01": bool(trusted)}
    if c.get("channel") == "email":
        try:
            expected = render_message(
                {"template_id": payload.get("template_id"), "fills": payload.get("fills")},
                template, sender, contact,
            )
        except ComplianceContentError:
            expected = None
        verified = _record(template.get("subject_accuracy_verified"))
        approved = (timestamp(template.get("approved_at")) and "revoked_at" in template
                    and template["revoked_at"] is None and _text(verified.get("by"))
                    and timestamp(verified.get("at")))
        checks["EMAIL-01"] = bool(
            expected is not None and payload == expected and approved
            and _text(payload.get("subject"))
            and all(_text(sender.get(k)) for k in ("from_address", "reply_to", "sending_domain"))
            and sender["from_address"].rsplit("@", 1)[-1].lower() == sender["sending_domain"].lower()
        )
        body = payload.get("body") if isinstance(payload.get("body"), str) else ""
        checks["EMAIL-02"] = bool(
            _text(sender.get("postal_address")) and sender["postal_address"] in body
            and payload.get("postal_address") == sender["postal_address"]
            and _text(template.get("ad_disclosure")) and template["ad_disclosure"] in body
            and payload.get("ad_disclosure") == template["ad_disclosure"]
        )
        route = _record(template.get("opt_out_route"))
        days = route.get("valid_days")
        route_type = _enum(route.get("type"), frozenset({"reply", "web"}))
        checks["EMAIL-03"] = bool(
            route_type is not None and _text(route.get("target"))
            and route["target"] in body and payload.get("opt_out_route") == route
            and type(days) is int and days >= 30 and route.get("covers_all_marketing") is True
            and route.get("fee_required") is False and route.get("extra_data_required") is False
            and timestamp(route.get("verified_at"))
            and (route_type != "reply" or route["target"] == sender.get("reply_to"))
        )
        checks["EMAIL-04"] = not suppressed

    failed = [rule for rule, passed in checks.items() if not passed]
    records = {
        "sender": sender, "recipient": contact, "consent": consent,
        "suppression": suppression, "template": template, "rendered_payload": payload,
    }
    # Evidence must always be recordable: malformed facts become null, and BASE-01 already fails for them.
    def text_or_none(value):
        return value if _text(value) else None

    def time_or_none(value):
        return value if _time(value) is not None else None

    sha = c.get("policy_sha256")
    evidence = {
        "policy_version": text_or_none(c.get("policy_version")),
        "policy_sha256": sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha) else None,
        "channel": text_or_none(c.get("channel")), "method": text_or_none(sender.get("dispatch_method")),
        "purpose": text_or_none(c.get("purpose")), "recipient_ref": text_or_none(c.get("recipient_id")),
        "sender_ref": text_or_none(sender.get("sender_id")),
        "suppression_version": text_or_none(suppression.get("version")),
        "jurisdiction": {k: text_or_none(location.get(k)) if isinstance(location, dict) else None
                         for k in ("country", "state")},
        "recipient_time_zone": zone, "recipient_local_time": local_time,
        "evaluated_at": time_or_none(c.get("evaluated_at")),
        "suppression_checked_at": time_or_none(suppression.get("checked_at")),
        "evidence_hashes": [
            {"record_type": kind, "record_id": kind + ":" + str(c.get("recipient_id")),
             "sha256": evidence_hash(record) if record else None}
            for kind, record in records.items()
        ],
        "rule_results": [
            {"rule_id": rule, "passed": passed, "reason_code": None if passed else REASONS[rule]}
            for rule, passed in checks.items()
        ],
        "failed_rule_ids": failed, "reason_codes": [REASONS[rule] for rule in failed],
        "proposed_remedy": "Provide verified current evidence and an approved template; honor all opt-outs.",
        "approvable": False,
    }
    # Unknown records must always be represented by a BASE-01 failure.
    if any(not record for record in records.values()) and "BASE-01" not in failed:
        evidence["failed_rule_ids"].insert(0, "BASE-01")
        evidence["reason_codes"].insert(0, REASONS["BASE-01"])
        evidence["rule_results"][0].update(passed=False, reason_code=REASONS["BASE-01"])
    return ComplianceResult(not evidence["failed_rule_ids"], evidence)
