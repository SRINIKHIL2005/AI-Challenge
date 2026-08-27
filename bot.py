#!/usr/bin/env python3
"""magicpin AI Challenge bot server.

Zero-dependency HTTP server that implements the required judge contract:
- POST /v1/context
- POST /v1/tick
- POST /v1/reply
- GET /v1/healthz
- GET /v1/metadata

The bot is intentionally deterministic and rule-based so it is easy to
deploy, validate, and iterate on.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any, Optional


ALLOWED_SCOPES = {"category", "merchant", "customer", "trigger"}
ALLOWED_FROM_ROLES = {"merchant", "customer"}

PRIORITY_BY_KIND = {
    "renewal_due": 100,
    "customer_lapsed_soft": 95,
    "recall_due": 95,
    "appointment_tomorrow": 90,
    "trial_followup": 90,
    "active_planning_intent": 85,
    "research_digest": 80,
    "regulation_change": 80,
    "competitor_opened": 70,
    "category_trend_movement": 70,
    "perf_dip": 65,
    "perf_spike": 60,
    "milestone_reached": 55,
    "review_theme_emerged": 50,
    "festival_upcoming": 45,
    "curious_ask_due": 40,
    "dormant_with_vera": 35,
    "scheduled_recurring": 20,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def safe_lower(value: Any) -> str:
    return str(value or "").strip().lower()


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def first_nonempty(*values: Any, default: str = "") -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@dataclass
class StoredContext:
    version: int
    payload: dict[str, Any]
    delivered_at: str


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    trigger_id: Optional[str]
    trigger_kind: Optional[str]
    send_as: str
    last_outbound_at: Optional[str] = None
    last_inbound_at: Optional[str] = None
    last_outbound_body: str = ""
    last_outbound_kind: str = ""
    status: str = "open"
    turns: list[dict[str, Any]] = field(default_factory=list)


class BotStore:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.lock = Lock()
        self.contexts: dict[tuple[str, str], StoredContext] = {}
        self.conversations: dict[str, ConversationState] = {}
        self.suppressed: set[str] = set()

    def context_counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for (scope, _), _context in self.contexts.items():
            counts[scope] = counts.get(scope, 0) + 1
        return counts

    def get_context(self, scope: str, context_id: str) -> Optional[StoredContext]:
        return self.contexts.get((scope, context_id))

    def upsert_context(self, scope: str, context_id: str, version: int, payload: dict[str, Any], delivered_at: str) -> tuple[bool, int | None, str]:
        key = (scope, context_id)
        existing = self.contexts.get(key)
        if existing and existing.version > version:
            return False, existing.version, "stale_version"
        self.contexts[key] = StoredContext(version=version, payload=payload, delivered_at=delivered_at)
        return True, version, "accepted"


STORE = BotStore()


def config_from_env() -> dict[str, Any]:
    team_members = [member.strip() for member in os.getenv("TEAM_MEMBERS", "").split(",") if member.strip()]
    submitted_at = os.getenv("SUBMITTED_AT") or utc_now_iso()
    return {
        "team_name": os.getenv("TEAM_NAME", "Team Alpha"),
        "team_members": team_members or ["Your Name"],
        "model": os.getenv("MODEL_NAME", "rule-based composer"),
        "approach": os.getenv(
            "APPROACH",
            "deterministic trigger router with category-aware offer selection"
        ),
        "contact_email": os.getenv("CONTACT_EMAIL", "team@example.com"),
        "version": os.getenv("APP_VERSION", "1.0.0"),
        "submitted_at": submitted_at,
    }


def category_slug_from_context(category: dict[str, Any]) -> str:
    return safe_lower(category.get("slug"))


def merchant_name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity") or {}
    return first_nonempty(identity.get("name"), merchant.get("merchant_name"), default="merchant")


def merchant_owner_first_name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity") or {}
    return first_nonempty(identity.get("owner_first_name"), default="there")


def salutation_for_category(category_slug: str, merchant: dict[str, Any]) -> str:
    owner_name = merchant_owner_first_name(merchant)
    if category_slug == "dentists":
        return f"Dr. {owner_name}"
    return owner_name


def pick_active_offer(category: dict[str, Any], merchant: dict[str, Any], trigger_kind: str) -> dict[str, Any] | None:
    offers = [offer for offer in as_list(merchant.get("offers")) if safe_lower(offer.get("status", "active")) == "active"]
    if not offers:
        offers = as_list(category.get("offer_catalog"))

    if not offers:
        return None

    slug = category_slug_from_context(category)
    text_pool = " ".join([safe_lower(trigger_kind), safe_lower(merchant.get("signals")), safe_lower(merchant_name(merchant))])

    if slug == "dentists":
        preferred = ("cleaning", "whitening", "aligner", "consultation", "scan", "checkup", "family")
    elif slug == "salons":
        preferred = ("bridal", "hair spa", "haircut", "keratin", "balayage", "mani", "pedi")
    elif slug == "restaurants":
        preferred = ("match", "thali", "brunch", "combo", "pizza", "delivery", "starter")
    elif slug == "gyms":
        preferred = ("trial", "pt", "membership", "yoga", "body composition", "combo")
    elif slug == "pharmacies":
        preferred = ("delivery", "health card", "bp", "sugar", "generic", "consultation", "refill")
    else:
        preferred = ()

    for token in preferred:
        for offer in offers:
            if token in safe_lower(offer.get("title")):
                return offer

    if slug == "restaurants" and "ipl" in text_pool:
        for offer in offers:
            if "match-night" in safe_lower(offer.get("title")) or "combo" in safe_lower(offer.get("title")):
                return offer

    if slug == "salons" and ("bridal" in text_pool or "wedding" in text_pool):
        for offer in offers:
            if "bridal" in safe_lower(offer.get("title")):
                return offer

    if slug == "pharmacies" and ("refill" in text_pool or "repeat" in text_pool or "chronic" in text_pool):
        for offer in offers:
            if "delivery" in safe_lower(offer.get("title")) or "health card" in safe_lower(offer.get("title")):
                return offer

    return offers[0]


def lookup_digest_item(category: dict[str, Any], trigger_payload: dict[str, Any]) -> dict[str, Any] | None:
    top_item_id = trigger_payload.get("top_item_id") or trigger_payload.get("top_item", {}).get("id")
    if top_item_id:
        for item in as_list(category.get("digest")):
            if item.get("id") == top_item_id:
                return item

    top_item = trigger_payload.get("top_item")
    if isinstance(top_item, dict) and top_item:
        return top_item

    digest = as_list(category.get("digest"))
    return digest[0] if digest else None


def category_templates(category_slug: str) -> tuple[str, str]:
    if category_slug == "dentists":
        return "vera_research_digest_v1", ""
    if category_slug == "salons":
        return "vera_salon_opportunity_v1", ""
    if category_slug == "restaurants":
        return "vera_restaurant_opportunity_v1", ""
    if category_slug == "gyms":
        return "vera_gym_opportunity_v1", ""
    if category_slug == "pharmacies":
        return "vera_pharmacy_opportunity_v1", ""
    return "vera_generic_v1", "Hi {name}"


def format_pct(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return str(value)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.0%}" if abs(number) < 1.5 else f"{sign}{number:.1f}%"


def build_research_digest_body(salutation: str, category: dict[str, Any], trigger: dict[str, Any], offer: dict[str, Any] | None) -> str:
    payload = trigger.get("payload") or {}
    digest_item = lookup_digest_item(category, payload) or {}
    title = first_nonempty(digest_item.get("title"), payload.get("headline"), default="the latest category note")
    source = first_nonempty(digest_item.get("source"), default="the cited source")
    summary = first_nonempty(digest_item.get("summary"), default="")
    actionable = first_nonempty(digest_item.get("actionable"), default="")
    body = f"{salutation}, {title}. Source: {source}."
    if summary:
        body += f" {summary}"
    if actionable:
        body += f" Action: {actionable}."
    if offer and offer.get("title"):
        body += f" A natural angle here is {offer.get('title')}."
    return body


def build_regulation_body(salutation: str, trigger: dict[str, Any]) -> str:
    payload = trigger.get("payload") or {}
    title = first_nonempty(payload.get("title"), payload.get("top_item_id"), default="a compliance update")
    deadline = first_nonempty(payload.get("deadline_iso"), default="")
    body = f"{salutation}, {title}."
    if deadline:
        body += f" Deadline: {deadline}."
    body += " If useful, I can turn it into a 3-point checklist."
    return body


def build_customer_message(slug: str, merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], offer: dict[str, Any] | None) -> str:
    customer_identity = customer.get("identity") or {}
    customer_name = first_nonempty(customer_identity.get("name"), default="there")
    merchant_name_text = merchant_name(merchant)
    trigger_kind = safe_lower(trigger.get("kind"))
    payload = trigger.get("payload") or {}
    offer_title = offer.get("title") if isinstance(offer, dict) else None
    lang = safe_lower(customer_identity.get("language_pref"))

    if trigger_kind in {"recall_due", "customer_lapsed_soft"}:
        slots = as_list(payload.get("available_slots"))
        slot_text = ", ".join(slot.get("label", slot.get("iso", "")) for slot in slots[:2] if isinstance(slot, dict))
        if slug == "dentists":
            return f"Hi {customer_name}, {merchant_name_text} has your next cleaning window open. {slot_text or 'A couple of slots are available this week'}. Reply YES and I’ll keep it short."
        if slug == "salons":
            return f"Hi {customer_name}, {merchant_name_text} can fit you in this week. {slot_text or 'A couple of slots are available'} — reply YES if you want the next opening."
        if slug == "restaurants":
            return f"Hi {customer_name}, {merchant_name_text} has a good slot for {offer_title or 'a quick visit'} this week. Reply YES if you want the best opening window."
        if slug == "gyms":
            return f"Hi {customer_name}, {merchant_name_text} has a good time slot for your next session. Reply YES and I’ll share the easiest opening."
        if slug == "pharmacies":
            return f"Hi {customer_name}, {merchant_name_text} can help with your refill or delivery. Reply YES and I’ll keep the next step simple."

    if trigger_kind in {"appointment_tomorrow", "trial_followup"}:
        return f"Hi {customer_name}, this is a quick reminder from {merchant_name_text}. Your next step is ready; reply YES to confirm or STOP to opt out."

    if trigger_kind in {"chronic_refill_due", "customer_lapsed_hard", "customer_lapsed_soft"} and slug == "pharmacies":
        return f"Hi {customer_name}, your refill reminder from {merchant_name_text} is ready. Reply YES for a simple delivery / pickup update or STOP to opt out."

    if trigger_kind == "wedding_package_followup" and slug == "salons":
        return f"Hi {customer_name}, {merchant_name_text} can share the next bridal-prep step. Reply YES if you want the details or STOP if now isn’t the right time."

    if lang.startswith("hi") or "mix" in lang:
        return f"Hi {customer_name}, {merchant_name_text} se ek quick update hai. Reply YES for the next step or STOP to opt out."

    return f"Hi {customer_name}, quick update from {merchant_name_text}. Reply YES for the next step or STOP to opt out."


def trigger_priority(trigger: dict[str, Any]) -> int:
    kind = safe_lower(trigger.get("kind"))
    return PRIORITY_BY_KIND.get(kind, 10)


def conversation_for_trigger(trigger: dict[str, Any]) -> str:
    return f"conv_{trigger.get('id')}_{uuid.uuid4().hex[:8]}"


def should_dedupe(trigger: dict[str, Any]) -> bool:
    suppression_key = safe_lower(trigger.get("suppression_key"))
    return bool(suppression_key and suppression_key in STORE.suppressed)


def register_suppression(trigger: dict[str, Any]) -> None:
    suppression_key = safe_lower(trigger.get("suppression_key"))
    if suppression_key:
        STORE.suppressed.add(suppression_key)


def load_contexts_for_trigger(trigger: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    merchant_id = trigger.get("merchant_id")
    customer_id = trigger.get("customer_id")
    category_slug = None

    merchant_ctx = STORE.get_context("merchant", merchant_id) if merchant_id else None
    merchant = merchant_ctx.payload if merchant_ctx else None

    if merchant:
        category_slug = merchant.get("category_slug")

    category_ctx = STORE.get_context("category", category_slug) if category_slug else None
    customer_ctx = STORE.get_context("customer", customer_id) if customer_id else None
    category = category_ctx.payload if category_ctx else None
    customer = customer_ctx.payload if customer_ctx else None
    return category, merchant, customer


def record_conversation(action: dict[str, Any], trigger: dict[str, Any], merchant: dict[str, Any], customer: Optional[dict[str, Any]] = None) -> ConversationState:
    conversation_id = action.get("conversation_id") or conversation_for_trigger(trigger)
    now = utc_now_iso()
    convo = ConversationState(
        conversation_id=conversation_id,
        merchant_id=merchant.get("merchant_id"),
        customer_id=customer.get("customer_id") if customer else None,
        trigger_id=trigger.get("id"),
        trigger_kind=trigger.get("kind"),
        send_as=action.get("send_as", "vera"),
        last_outbound_at=now,
        last_outbound_body=action.get("body", ""),
        last_outbound_kind=safe_lower(trigger.get("kind")),
        turns=[{"direction": "outbound", "body": action.get("body", ""), "at": now}],
    )
    STORE.conversations[conversation_id] = convo
    register_suppression(trigger)
    return convo


def pick_tick_trigger(available_trigger_ids: list[str]) -> Optional[tuple[dict[str, Any], dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]]:
    candidates = []
    for trigger_id in available_trigger_ids:
        trigger_ctx = STORE.get_context("trigger", trigger_id)
        if not trigger_ctx:
            continue
        trigger = trigger_ctx.payload
        if should_dedupe(trigger):
            continue
        category, merchant, customer = load_contexts_for_trigger(trigger)
        if not category or not merchant:
            continue
        candidates.append((trigger_priority(trigger), trigger, category, merchant, customer))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item[0], safe_lower(item[1].get("id"))))
    _, trigger, category, merchant, customer = candidates[0]
    return trigger, category, merchant, customer


def classify_reply(message: str) -> str:
    text = safe_lower(message)
    if re.search(r"\b(stop|no thanks|not interested|dont want|don't want|unsubscribe|never mind|cancel)\b", text):
        return "end"
    if re.search(r"\b(later|tomorrow|busy|call later|back later|after lunch|this evening|tonight|next week|sometime)\b", text):
        return "wait"
    if re.search(r"\b(yes|send|share|details|abstract|okay|ok|sure|please)\b", text):
        return "send"
    if re.search(r"\b(what|why|how|explain|clarify|context|more)\b", text):
        return "send"
    return "send"


def rationale_for_reply(trigger_kind: str, message: str) -> str:
    text = safe_lower(message)
    if "abstract" in text or "details" in text or "send" in text or "share" in text:
        return "User accepted the prompt; giving the requested detail and a low-friction next step"
    if "what" in text or "why" in text or "how" in text or "more" in text:
        return "User asked for clarification; answering directly before asking for the next step"
    return f"Continuing the {trigger_kind} conversation with a category-fit follow-up"


def build_reply_body(trigger_kind: str, message: str, merchant: dict[str, Any], customer: Optional[dict[str, Any]], convo: Optional[ConversationState]) -> str:
    slug = merchant.get("category_slug", "")
    merchant_label = merchant_name(merchant)
    text = safe_lower(message)
    trigger_kind = safe_lower(trigger_kind)
    category_offer = None

    if convo and convo.trigger_id:
        trigger_ctx = STORE.get_context("trigger", convo.trigger_id)
        if trigger_ctx:
            category_ctx = STORE.get_context("category", slug)
            if category_ctx:
                category_offer = pick_active_offer(category_ctx.payload, merchant, trigger_ctx.payload.get("kind", ""))

    if trigger_kind == "research_digest":
        trigger_ctx = STORE.get_context("trigger", convo.trigger_id) if convo and convo.trigger_id else None
        category_ctx = STORE.get_context("category", slug) if slug else None
        digest_item = None
        if trigger_ctx and category_ctx:
            digest_item = lookup_digest_item(category_ctx.payload, trigger_ctx.payload.get("payload", {}))
        title = first_nonempty(digest_item.get("title") if digest_item else "", default="the abstract")
        source = first_nonempty(digest_item.get("source") if digest_item else "", default="the cited source")
        summary = first_nonempty(digest_item.get("summary") if digest_item else "", default="")
        body = f"Sure — here is the short version of {title} from {source}."
        if summary:
            body += f" {summary}"
        if category_offer and category_offer.get("title"):
            body += f" The clean next step for {merchant_label} is to pair it with {category_offer.get('title')}."
        body += " If you want, I can turn this into a WhatsApp post or a 3-line GBP note."
        return body

    if trigger_kind in {"renewal_due", "perf_dip", "perf_spike", "milestone_reached", "review_theme_emerged", "dormant_with_vera", "festival_upcoming", "category_trend_movement", "competitor_opened"}:
        if "yes" in text or "send" in text or "share" in text or "more" in text or "details" in text:
            offer_text = category_offer.get("title") if category_offer else "a relevant offer"
            return f"Done — for {merchant_label}, the cleanest next move is {offer_text}. I can also draft the exact wording if you want it."
        if "what" in text or "why" in text or "how" in text:
            return f"Because {merchant_label} is seeing the signal tied to this trigger, and {category_offer.get('title') if category_offer else 'the current offer'} is the simplest category-fit next step."
        return f"For {merchant_label}, I’d keep it simple and use {category_offer.get('title') if category_offer else 'a practical offer'} as the next step. Want me to draft the exact message?"

    if trigger_kind in {"active_planning_intent", "curious_ask_due", "scheduled_recurring"}:
        return f"Here’s a practical version for {merchant_label}: one concrete offer, one short line, and one clear CTA. If you want, I’ll turn that into copy now."

    if customer and trigger_kind in {"recall_due", "customer_lapsed_soft", "appointment_tomorrow", "trial_followup", "chronic_refill_due", "wedding_package_followup"}:
        return f"Understood — I’ll keep it short for the customer journey and only share the next step for {merchant_label}."

    return f"Thanks — the next step for {merchant_label} is to keep this message specific and category-fit. Want me to draft the exact text?"


def reply_action(trigger_kind: str, message: str, merchant: dict[str, Any], customer: Optional[dict[str, Any]], convo: Optional[ConversationState]) -> dict[str, Any]:
    classification = classify_reply(message)
    if classification == "end":
        if convo:
            convo.status = "ended"
        return {"action": "end", "rationale": "Merchant/customer opted out or said not interested; ending the conversation cleanly"}

    if classification == "wait":
        if convo:
            convo.status = "waiting"
        wait_seconds = 1800 if re.search(r"\b(later|after lunch|this evening|tonight)\b", safe_lower(message)) else 86400
        return {"action": "wait", "wait_seconds": wait_seconds, "rationale": "User asked for more time; backing off respectfully"}

    body = build_reply_body(trigger_kind, message, merchant, customer, convo)
    if convo:
        convo.status = "open"
        now = utc_now_iso()
        convo.last_inbound_at = now
        convo.turns.append({"direction": "inbound", "body": message, "at": now})
        convo.turns.append({"direction": "outbound", "body": body, "at": now})
    return {"action": "send", "body": body, "cta": "open_ended", "rationale": rationale_for_reply(trigger_kind, message)}


def build_merchant_message(slug: str, merchant: dict[str, Any], category: dict[str, Any], trigger: dict[str, Any], offer: dict[str, Any] | None) -> str:
    merchant_id = merchant.get("merchant_id", "")
    merchant_identity = merchant.get("identity") or {}
    city = first_nonempty(merchant_identity.get("city"), default="your city")
    locality = first_nonempty(merchant_identity.get("locality"), default="your locality")
    name = salutation_for_category(slug, merchant)
    kind = safe_lower(trigger.get("kind"))
    payload = trigger.get("payload") or {}
    offer_title = offer.get("title") if isinstance(offer, dict) else None

    if slug == "dentists":
        if kind == "renewal_due":
            days_remaining = payload.get("days_remaining")
            renewal_amount = payload.get("renewal_amount")
            return f"{name}, your Pro renewal is due in {days_remaining} days at ₹{renewal_amount}. If you want, I can draft a short renewal nudge for the owner and a GBP post to keep calls steady."
        if kind == "perf_dip":
            metric = first_nonempty(payload.get("metric"), default="calls")
            delta_pct = payload.get("delta_pct")
            return f"{name}, {metric} are down {format_pct(delta_pct)} vs the baseline. For dentists, a simple next step is to refresh the GBP post and anchor it to {offer_title or 'a clean, specific offer'} in {locality}."
        if kind == "perf_spike":
            return f"{name}, {trigger.get('payload', {}).get('metric', 'views')} are up this week. Good time to surface {offer_title or 'a clear service offer'} and keep the profile updated while intent is hot."
        if kind == "research_digest":
            return build_research_digest_body(name, category, trigger, offer)
        if kind == "regulation_change":
            return build_regulation_body(name, trigger)
        if kind == "review_theme_emerged":
            theme = first_nonempty(payload.get("theme"), default="a review theme")
            quote = first_nonempty(payload.get("common_quote"), default="")
            body = f"{name}, {theme} has surfaced in recent reviews."
            if quote:
                body += f" Example quote: \"{quote}\"."
            body += " I can draft a reply playbook and a one-line operational fix if you want."
            return body

    if slug == "salons":
        if kind in {"festival_upcoming", "category_trend_movement"}:
            festival = first_nonempty(payload.get("festival"), payload.get("query"), default="the current trend")
            return f"{name}, {festival} is a good window to push {offer_title or 'a practical offer'} in {locality}. If you want, I can turn it into a short WhatsApp post and a GBP caption."
        if kind == "perf_dip":
            return f"{name}, calls are soft this week in {city}. A category-fit move is to surface {offer_title or 'a low-friction booking offer'} and mention walk-ins or weekday slots."
        if kind == "active_planning_intent":
            topic = first_nonempty(payload.get("intent_topic"), default="the next campaign")
            return f"{name}, on {topic}, I can draft the offer, a 2-line caption, and a simple CTA in your tone."
        if kind == "research_digest":
            return build_research_digest_body(name, category, trigger, offer)

    if slug == "restaurants":
        if kind in {"festival_upcoming", "category_trend_movement", "perf_spike"}:
            headline = first_nonempty(payload.get("match"), payload.get("query"), default="the current traffic pattern")
            return f"{name}, {headline} is worth a quick push for {offer_title or 'a focused combo'} in {locality}. I can draft a short operator note and a customer-facing WhatsApp line."
        if kind == "review_theme_emerged":
            theme = first_nonempty(payload.get("theme"), default="a review theme")
            return f"{name}, {theme} is showing up in reviews. If you want, I can help write a small ops fix and a calm reply template."
        if kind == "research_digest":
            return build_research_digest_body(name, category, trigger, offer)

    if slug == "gyms":
        if kind in {"perf_dip", "category_trend_movement", "seasonal_perf_dip"}:
            return f"{name}, this looks like a retention window more than an acquisition one. {offer_title or 'A trial or PT offer'} in {locality} may work better than broad discounting."
        if kind == "active_planning_intent":
            topic = first_nonempty(payload.get("intent_topic"), default="the next class or program")
            return f"{name}, for {topic}, I can outline a clean program, price anchor, and CTA that matches the coach-to-member tone."
        if kind == "research_digest":
            return build_research_digest_body(name, category, trigger, offer)

    if slug == "pharmacies":
        if kind in {"recall_due", "chronic_refill_due", "alert", "supply"}:
            topic = first_nonempty(payload.get("molecule"), payload.get("title"), default="a shelf update")
            return f"{name}, {topic} is worth a quick check. {offer_title or 'A refill reminder / delivery offer'} can be paired with a precise pharmacist note and a simple follow-up list."
        if kind == "research_digest":
            return build_research_digest_body(name, category, trigger, offer)

    generic_signal = first_nonempty(payload.get("metric"), payload.get("topic"), payload.get("title"), default=kind or "an update")
    body = f"{name}, {generic_signal} is something I can help turn into a concrete next step for {offer_title or merchant_name(merchant)}."
    return body


def format_trigger_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    slug = category_slug_from_context(category)
    kind = safe_lower(trigger.get("kind"))
    trigger_payload = trigger.get("payload") or {}
    salutation = salutation_for_category(slug, merchant)
    template_name, template_prefix = category_templates(slug)
    offer = pick_active_offer(category, merchant, kind)
    offer_title = offer.get("title") if isinstance(offer, dict) else None

    if trigger.get("scope") == "customer" and customer:
        customer_identity = customer.get("identity") or {}
        customer_name = first_nonempty(customer_identity.get("name"), default="there")
        body = build_customer_message(slug, merchant, trigger, customer, offer)
        return {
            "send_as": "merchant_on_behalf",
            "template_name": template_name,
            "template_params": [merchant_name(merchant), customer_name, trigger.get("kind", "update")],
            "body": body,
            "cta": "YES/STOP",
            "rationale": f"Customer-facing {kind} trigger with consent-aware, category-correct reminder",
            "merchant_id": merchant.get("merchant_id"),
            "customer_id": customer.get("customer_id"),
        }

    if kind == "research_digest":
        digest_item = lookup_digest_item(category, trigger_payload) or {}
        title = first_nonempty(digest_item.get("title"), trigger_payload.get("headline"), default="a relevant update")
        source = first_nonempty(digest_item.get("source"), trigger_payload.get("source"), default="the latest digest")
        actionable = first_nonempty(digest_item.get("actionable"), default="")
        body_parts = [
            f"{salutation} — {title}.",
            f"Source: {source}.",
        ]
        if actionable:
            body_parts.append(f"Action: {actionable}.")
        if offer_title:
            body_parts.append(f"If you want, I can frame it around {offer_title} for GBP/WhatsApp.")
        return {
            "send_as": "vera",
            "template_name": template_name,
            "template_params": [merchant_name(merchant), title, source],
            "body": " ".join(body_parts),
            "cta": "open_ended",
            "rationale": "Category-specific research digest anchored to a verifiable source and a merchant-relevant offer",
            "merchant_id": merchant.get("merchant_id"),
            "customer_id": None,
        }

    if kind == "regulation_change":
        title = first_nonempty(trigger_payload.get("title"), trigger_payload.get("top_item_id"), default="a compliance update")
        deadline = first_nonempty(trigger_payload.get("deadline_iso"), default="")
        body = f"{salutation} — {title}."
        if deadline:
            body += f" Deadline: {deadline}."
        body += " If useful, I can turn this into a simple checklist for your team."
        return {
            "send_as": "vera",
            "template_name": template_name,
            "template_params": [merchant_name(merchant), title, deadline or "soon"],
            "body": body,
            "cta": "open_ended",
            "rationale": "Compliance update with one concrete deadline and a low-friction next step",
            "merchant_id": merchant.get("merchant_id"),
            "customer_id": None,
        }

    if kind in {"renewal_due", "perf_dip", "perf_spike", "milestone_reached", "review_theme_emerged", "dormant_with_vera", "curious_ask_due", "festival_upcoming", "competitor_opened", "category_trend_movement", "scheduled_recurring", "active_planning_intent"}:
        body = build_merchant_message(slug, merchant, category, trigger, offer)
        return {
            "send_as": "vera",
            "template_name": template_name,
            "template_params": [merchant_name(merchant), trigger.get("kind", "update"), offer_title or "a relevant offer"],
            "body": body,
            "cta": "open_ended" if kind in {"milestone_reached", "curious_ask_due", "scheduled_recurring", "active_planning_intent"} else "YES/STOP",
            "rationale": f"Merchant-facing {kind} trigger with category-fit offer and a specific business signal",
            "merchant_id": merchant.get("merchant_id"),
            "customer_id": None,
        }

    body = build_merchant_message(slug, merchant, category, trigger, offer)
    return {
        "send_as": "vera",
        "template_name": template_name,
        "template_params": [merchant_name(merchant), trigger.get("kind", "update"), offer_title or "a relevant offer"],
        "body": body,
        "cta": "open_ended",
        "rationale": "Generic category-aware message fallback",
        "merchant_id": merchant.get("merchant_id"),
        "customer_id": None,
    }


class BotHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VeraChallengeBot/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/healthz":
            self.handle_healthz()
            return
        if self.path == "/v1/metadata":
            self.handle_metadata()
            return
        json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/context":
            self.handle_context()
            return
        if self.path == "/v1/tick":
            self.handle_tick()
            return
        if self.path == "/v1/reply":
            self.handle_reply()
            return
        json_response(self, 404, {"error": "not_found"})

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def handle_healthz(self) -> None:
        with STORE.lock:
            payload = {
                "status": "ok",
                "uptime_seconds": int(time.time() - STORE.started_at),
                "contexts_loaded": STORE.context_counts(),
            }
        json_response(self, 200, payload)

    def handle_metadata(self) -> None:
        json_response(self, 200, config_from_env())

    def handle_context(self) -> None:
        try:
            body = self.read_json()
        except Exception:
            json_response(self, 400, {"accepted": False, "reason": "malformed_json"})
            return

        scope = safe_lower(body.get("scope"))
        context_id = first_nonempty(body.get("context_id"), default="")
        version = body.get("version")
        payload = body.get("payload")
        delivered_at = first_nonempty(body.get("delivered_at"), default=utc_now_iso())

        if scope not in ALLOWED_SCOPES:
            json_response(self, 400, {"accepted": False, "reason": "invalid_scope", "details": scope})
            return
        if not context_id or not isinstance(version, int) or not isinstance(payload, dict):
            json_response(self, 400, {"accepted": False, "reason": "invalid_payload"})
            return

        with STORE.lock:
            accepted, current_version, reason = STORE.upsert_context(scope, context_id, version, payload, delivered_at)
            if not accepted:
                json_response(self, 409, {"accepted": False, "reason": reason, "current_version": current_version})
                return

        json_response(self, 200, {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": utc_now_iso()})

    def handle_tick(self) -> None:
        try:
            body = self.read_json()
        except Exception:
            json_response(self, 400, {"error": "malformed_json"})
            return

        available_trigger_ids = [str(trigger_id) for trigger_id in as_list(body.get("available_triggers"))]
        actions: list[dict[str, Any]] = []

        with STORE.lock:
            picked = pick_tick_trigger(available_trigger_ids)
            if picked:
                trigger, category, merchant, customer = picked
                action = format_trigger_message(category, merchant, trigger, customer)
                action["conversation_id"] = conversation_for_trigger(trigger)
                action["trigger_id"] = trigger.get("id")
                action["merchant_id"] = merchant.get("merchant_id")
                action["customer_id"] = customer.get("customer_id") if customer else None
                record_conversation(action, trigger, merchant, customer)
                actions.append(action)

        json_response(self, 200, {"actions": actions})

    def handle_reply(self) -> None:
        try:
            body = self.read_json()
        except Exception:
            json_response(self, 400, {"error": "malformed_json"})
            return

        conversation_id = first_nonempty(body.get("conversation_id"), default="")
        merchant_id = first_nonempty(body.get("merchant_id"), default="")
        customer_id = body.get("customer_id")
        from_role = safe_lower(body.get("from_role"))
        message = first_nonempty(body.get("message"), default="")
        turn_number = body.get("turn_number")

        if from_role not in ALLOWED_FROM_ROLES:
            json_response(self, 400, {"error": "invalid_from_role"})
            return

        with STORE.lock:
            convo = STORE.conversations.get(conversation_id)
            merchant_ctx = STORE.get_context("merchant", merchant_id)
            merchant = merchant_ctx.payload if merchant_ctx else ({"merchant_id": merchant_id, "category_slug": ""} if merchant_id else None)
            customer = STORE.get_context("customer", customer_id).payload if customer_id else None

            if not merchant:
                json_response(self, 404, {"error": "merchant_context_missing"})
                return

            trigger_kind = convo.trigger_kind if convo and convo.trigger_kind else ""
            result = reply_action(trigger_kind, message, merchant, customer, convo)
            if convo is None:
                STORE.conversations[conversation_id] = ConversationState(
                    conversation_id=conversation_id,
                    merchant_id=merchant.get("merchant_id"),
                    customer_id=customer.get("customer_id") if customer else None,
                    trigger_id=None,
                    trigger_kind=trigger_kind,
                    send_as="vera",
                )
                convo = STORE.conversations[conversation_id]
            convo.turns.append({"direction": from_role, "body": message, "turn_number": turn_number, "at": utc_now_iso()})
            if result.get("action") == "send":
                now = utc_now_iso()
                convo.last_outbound_at = now
                convo.last_outbound_body = result.get("body", "")
                convo.turns.append({"direction": "outbound", "body": result.get("body", ""), "at": now})

        json_response(self, 200, result)


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port_candidates = []
    env_port = os.getenv("PORT")
    if env_port:
        port_candidates.append(int(env_port))
    else:
        port_candidates.extend([8000, 8080, 3000])

    last_error: Exception | None = None
    for port in port_candidates:
        try:
            server = ThreadingHTTPServer((host, port), BotHandler)
            print(f"Vera bot listening on http://{host}:{port}")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error


if __name__ == "__main__":
    main()