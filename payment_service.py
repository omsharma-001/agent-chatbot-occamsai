# payment_service.py
import json
import os
from typing import Optional, Dict, Any

# Stripe is optional at import-time so local dev won't crash if it's missing.
try:
    import stripe  # pip install stripe
except Exception:  # pragma: no cover
    stripe = None


class PaymentService:
    """
    Payment helpers (Stripe + State Filing Fees).

    Public methods used by the app:
      - state_fee_lookup(state, entity_type) -> dict
      - create_payment_link(product_name, price, billing_cycle, state_fee, total_due_now,
                            session_id, success_url=None, cancel_url=None) -> dict{id,url}
      - check_payment_status(session_id) -> 'completed' | 'pending' | 'failed' | 'unknown'
    """

    # ====== State + Fee Tables ======
    STATE_CODE_TO_NAME: Dict[str, str] = {
        "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado",
        "CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho",
        "IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana",
        "ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota",
        "MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada",
        "NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York",
        "NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon",
        "PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota",
        "TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia",
        "WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"Washington, DC"
    }

    # Canonical state names → fees (USD whole dollars)
    STATE_FEES: Dict[str, Dict[str, int]] = {
        "Alabama":        {"llc":200, "s-corp":208, "c-corp":208},
        "Alaska":         {"llc":250, "s-corp":250, "c-corp":250},
        "Arizona":        {"llc":50,  "s-corp":60,  "c-corp":60 },
        "Arkansas":       {"llc":45,  "s-corp":50,  "c-corp":50 },
        "California":     {"llc":70,  "s-corp":100, "c-corp":100},
        "Colorado":       {"llc":50,  "s-corp":50,  "c-corp":50 },
        "Connecticut":    {"llc":120, "s-corp":250, "c-corp":250},
        "Delaware":       {"llc":90,  "s-corp":89,  "c-corp":89 },
        "Florida":        {"llc":125, "s-corp":70,  "c-corp":70 },
        "Georgia":        {"llc":100, "s-corp":100, "c-corp":100},
        "Hawaii":         {"llc":50,  "s-corp":50,  "c-corp":50 },
        "Idaho":          {"llc":100, "s-corp":100, "c-corp":100},
        "Illinois":       {"llc":150, "s-corp":150, "c-corp":150},
        "Indiana":        {"llc":95,  "s-corp":90,  "c-corp":90 },
        "Iowa":           {"llc":50,  "s-corp":50,  "c-corp":50 },
        "Kansas":         {"llc":160, "s-corp":90,  "c-corp":90 },
        "Kentucky":       {"llc":40,  "s-corp":50,  "c-corp":50 },
        "Louisiana":      {"llc":100, "s-corp":75,  "c-corp":75 },
        "Maine":          {"llc":175, "s-corp":145, "c-corp":145},
        "Maryland":       {"llc":150, "s-corp":120, "c-corp":120},
        "Massachusetts":  {"llc":500, "s-corp":275, "c-corp":275},
        "Michigan":       {"llc":50,  "s-corp":60,  "c-corp":60 },
        "Minnesota":      {"llc":155, "s-corp":135, "c-corp":135},
        "Mississippi":    {"llc":50,  "s-corp":50,  "c-corp":50 },
        "Missouri":       {"llc":50,  "s-corp":58,  "c-corp":58 },
        "Montana":        {"llc":35,  "s-corp":70,  "c-corp":70 },
        "Nebraska":       {"llc":100, "s-corp":60,  "c-corp":60 },
        "Nevada":         {"llc":425, "s-corp":725, "c-corp":725},
        "New Hampshire":  {"llc":100, "s-corp":100, "c-corp":100},
        "New Jersey":     {"llc":125, "s-corp":125, "c-corp":125},
        "New Mexico":     {"llc":50,  "s-corp":100, "c-corp":100},
        "New York":       {"llc":200, "s-corp":125, "c-corp":125},
        "North Carolina": {"llc":125, "s-corp":125, "c-corp":125},
        "North Dakota":   {"llc":135, "s-corp":100, "c-corp":100},
        "Ohio":           {"llc":99,  "s-corp":99,  "c-corp":99 },
        "Oklahoma":       {"llc":100, "s-corp":50,  "c-corp":50 },
        "Oregon":         {"llc":100, "s-corp":100, "c-corp":100},
        "Pennsylvania":   {"llc":125, "s-corp":125, "c-corp":125},
        "Rhode Island":   {"llc":150, "s-corp":230, "c-corp":230},
        "South Carolina": {"llc":110, "s-corp":125, "c-corp":125},
        "South Dakota":   {"llc":150, "s-corp":150, "c-corp":150},
        "Tennessee":      {"llc":300, "s-corp":100, "c-corp":100},
        "Texas":          {"llc":300, "s-corp":300, "c-corp":300},
        "Utah":           {"llc":70,  "s-corp":70,  "c-corp":70 },
        "Vermont":        {"llc":125, "s-corp":125, "c-corp":125},
        "Virginia":       {"llc":100, "s-corp":25,  "c-corp":25 },
        "Washington":     {"llc":200, "s-corp":200, "c-corp":200},
        "West Virginia":  {"llc":100, "s-corp":50,  "c-corp":50 },
        "Wisconsin":      {"llc":130, "s-corp":100, "c-corp":100},
        "Wyoming":        {"llc":100, "s-corp":100, "c-corp":100},
        "Washington, DC": {"llc":99,  "s-corp":99,  "c-corp":99 }
    }

    # ====== Public: State fee lookup ======
    @classmethod
    def state_fee_lookup(cls, state: str, entity_type: str) -> Dict[str, Any]:
        """
        Returns a dict:
          {
            "state": "<Canonical State Name>",
            "entity_type": "<LLC|C-Corp|S-Corp>",
            "stateFilingFee": <int USD>,
            "stateFilingFeeCents": <int cents>
          }
        or {"error": "..."} on failure.
        """
        canonical_state = cls._resolve_state(state)
        normalized_entity = cls._normalize_entity(entity_type)

        if not canonical_state or not normalized_entity:
            return {"error": "missing_params", "state": state, "entity_type": entity_type}

        dollars = cls.get_state_filing_fee(canonical_state, normalized_entity)
        if dollars is None:
            return {"error": "fee_not_found", "state": canonical_state, "entity_type": normalized_entity}

        return {
            "state": canonical_state,
            "entity_type": normalized_entity,
            "stateFilingFee": int(dollars),
            "stateFilingFeeCents": int(dollars) * 100
        }

    # ====== Public: Create Stripe Checkout and return link ======
    @classmethod
    def create_payment_link(
        cls,
        product_name: str,
        price: float,
        billing_cycle: Optional[str],
        state_fee: float,
        total_due_now: float,
        session_id: Optional[str],
        *,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Returns {"id": "<checkout_session_id>", "url": "<checkout_url>"}.
        Creates a Stripe Checkout Session with:
          - Line item 1: Plan (e.g., "Classic Plan — yearly")
          - Line item 2: State filing fees
        Persists mapping: conversation session_id → checkout_session_id.
        """
        if stripe is None:
            raise RuntimeError("Stripe SDK is not installed. Run: pip install stripe")

        secret = os.getenv("STRIPE_SECRET_KEY", "")
        if not secret:
            raise RuntimeError("STRIPE_SECRET_KEY is not set in the environment.")

        stripe.api_key = secret

        # Where to return after success/cancel (your Gradio origin)
        site_url = os.getenv("SITE_URL", "http://localhost:7860").rstrip("/")

        # Compose display names
        cycle_label = f" — {billing_cycle}" if billing_cycle else ""
        plan_display = f"{product_name} Plan{cycle_label}"

        # Convert to cents (int)
        def to_cents(x: float) -> int:
            return int(round(float(x) * 100))

        plan_cents = to_cents(price)
        state_fee_cents = to_cents(state_fee)

        # Build line items
        line_items = [
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": plan_cents,
                    "product_data": {"name": plan_display},
                },
            },
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": state_fee_cents,
                    "product_data": {"name": "State filing fees"},
                },
            },
        ]

        # Respect explicit URLs if provided (so they match your process_url_params).
        # Fallback to defaults that ALSO use conv_id/status/session_id param names.
        if not success_url:
            success_url = f"{site_url}?conv_id={session_id or ''}&status=success&session_id={{CHECKOUT_SESSION_ID}}"
        if not cancel_url:
            cancel_url = f"{site_url}?conv_id={session_id or ''}&status=cancel"

        # Metadata is handy for dashboards and webhooks later
        metadata = {
            "conversation_id": session_id or "",
            "productName": product_name,
            "billingCycle": billing_cycle or "",
            "planPrice": str(price),
            "stateFilingFee": str(state_fee),
            "totalDueNow": str(total_due_now),
        }

        # Create Checkout Session
        cs = stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            allow_promotion_codes=True,
            invoice_creation={"enabled": False},
        )

        # Persist mapping for later status checks
        if session_id:
            cls._store_checkout_session_id(session_id, cs.id)

        return {"id": cs.id, "url": cs.url}

    # ====== Public: Check Stripe payment status ======
    @classmethod
    def check_payment_status(cls, session_id: Optional[str]) -> str:
        """
        Returns:
          - 'completed' when Checkout Session status = 'complete' and payment_status = 'paid'
          - 'pending'   when not complete/paid yet
          - 'failed'    when the session is 'expired'
          - 'unknown'   when no mapping / cannot retrieve / misconfig
        """
        if stripe is None:
            return "unknown"

        secret = os.getenv("STRIPE_SECRET_KEY", "")
        if not secret:
            return "unknown"
        stripe.api_key = secret

        if not session_id:
            return "unknown"

        checkout_id = cls._get_checkout_session_id(session_id)
        if not checkout_id:
            return "unknown"

        try:
            cs = stripe.checkout.Session.retrieve(checkout_id)
        except Exception as e:  # network/auth problems
            print(f"[PaymentService] ⚠️ Stripe retrieve failed: {e}")
            return "unknown"

        status = getattr(cs, "status", None)             # 'open' | 'complete' | 'expired'
        payment_status = getattr(cs, "payment_status", None)  # 'unpaid' | 'paid' | ...

        if status == "complete" and payment_status == "paid":
            return "completed"
        if status == "expired":
            return "failed"
        return "pending"

    # ====== Internals: Fee helpers ======
    @classmethod
    def _normalize_entity(cls, entity: Optional[str]) -> Optional[str]:
        if not entity:
            return None
        s = entity.strip().lower().replace('.', '').replace('-', ' ').replace('  ', ' ')
        if s == "llc":
            return "LLC"
        if s in {"c corp", "ccorp", "c corp ", "c  corp"}:
            return "C-Corp"
        if s in {"s corp", "scorp", "s ccorp", "s c corp", "s corp "}:
            return "S-Corp"
        return None

    @classmethod
    def _resolve_state(cls, state: str) -> str:
        raw = (state or "").strip()
        if not raw:
            return ""
        # 2-letter code?
        upper = raw.upper()
        canonical = cls.STATE_CODE_TO_NAME.get(upper)
        if canonical:
            return canonical

        # Normalize DC variants
        s = raw.lower().replace('.', '').replace(',', '').replace('  ', ' ').strip()
        if s in {"dc", "d c", "washington dc", "washington d c", "district of columbia"}:
            return "Washington, DC"

        # Exact full-name match
        for key in cls.STATE_FEES.keys():
            if key.lower() == raw.lower():
                return key
        return raw  # fall through (may miss in fees)

    @classmethod
    def get_state_filing_fee(cls, state: str, entity_type: str) -> Optional[int]:
        canonical_state = cls._resolve_state(state)
        normalized_entity = cls._normalize_entity(entity_type)
        if not canonical_state or not normalized_entity:
            return None
        fee_row = cls.STATE_FEES.get(canonical_state)
        if not fee_row:
            return None
        return fee_row.get(normalized_entity.lower())

    # ====== Internals: Mapping (conversation_id -> checkout_session_id) ======
    @staticmethod
    def _map_path() -> str:
        # You can override with PAYMENT_SESSIONS_PATH env var
        return os.getenv("PAYMENT_SESSIONS_PATH", "payment_sessions.json")

    @classmethod
    def _load_map(cls) -> Dict[str, str]:
        path = cls._map_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @classmethod
    def _save_map(cls, data: Dict[str, str]) -> None:
        path = cls._map_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
        except Exception as e:
            print(f"[PaymentService] ⚠️ Failed to save mapping file {path}: {e}")

    @classmethod
    def _store_checkout_session_id(cls, conversation_id: str, checkout_id: str) -> None:
        if not conversation_id or not checkout_id:
            return
        data = cls._load_map()
        data[conversation_id] = checkout_id
        cls._save_map(data)

    @classmethod
    def _get_checkout_session_id(cls, conversation_id: str) -> Optional[str]:
        if not conversation_id:
            return None
        data = cls._load_map()
        return data.get(conversation_id)
