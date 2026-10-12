from django.db.models import Q, F, Sum, OuterRef, Subquery, Value, TextField
from django.db.models.functions import Coalesce
from django.db.models import ExpressionWrapper, DecimalField
import logging
import mimetypes
import os
import random
import uuid
import threading
from datetime import timedelta
import json
import re
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from .storage_helper import get_live_chat_dir, get_data_hub_dir

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    User,
    UserRole,
    UserMeta,
    Plan,
    Supplier,
    Product,
    Batch,
    ProductPurchase,
    ProductPurchaseItem,
    SupplierPayment,
    InventoryTransaction,
    Customer,
    CustomerCart,
    CustomerPurchase,
    CustomerPurchaseItem,
    CustomerPayment,
    Category,
    Bot,
    TeamMember,
    LiveChatSession,
    LiveChatMessage,
    OnlineOrder,
    OnlineOrderItem,
    WhatsAppCampaign,
    generate_public_order_no,
)
from .permissions import IsAdmin, IsSeller, IsSellerOrTeamManagerOrAgent
from .serializers import (
    BuyerRegistrationSerializer,
    LoginSerializer,
    SellerRegistrationSerializer,
    UserDetailSerializer,
    PlanSerializer,
    SupplierSerializer,
    ProductSerializer,
    BatchSerializer,
    BatchListSerializer,
    ProductPurchaseSerializer,
    ProductPurchaseItemSerializer,
    SupplierPaymentSerializer,
    InventoryTransactionSerializer,
    CustomerSerializer,
    ProductListSerializer,
    CustomerCartSerializer,
    CustomerPurchaseSerializer,
    CustomerPurchaseItemSerializer,
    CustomerPaymentSerializer,
    CategorySerializer,
    BotSerializer,
    LiveChatSessionSerializer,
    LiveChatSessionPublicSerializer,
    LiveChatMessageSerializer,
    LiveChatSupportSessionListSerializer,
    TeamMemberSerializer,
    TeamMemberCreateSerializer,
    TeamMemberUpdateSerializer,
)

from django.http import FileResponse, HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt

logger = logging.getLogger(__name__)


def _sanitize_chat_text(text: str) -> str:
    """
    MySQL charset/collation in this project may not support some emoji characters.
    Strip non-BMP characters (codepoints > U+FFFF) to prevent:
    "Incorrect string value" errors when inserting into TEXT columns.
    """
    if text is None:
        return ""
    s = str(text)
    return "".join(ch for ch in s if ord(ch) <= 0xFFFF)


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _extract_agent_assign_config(bot, node_id=None):
    flow = bot.flow if isinstance(bot.flow, dict) else {}

    def _to_int(value):
        if isinstance(value, dict):
            value = (
                value.get("id")
                or value.get("user_id")
                or value.get("value")
                or value.get("key")
            )
        raw = str(value or "").strip()
        return int(raw) if raw.isdigit() else None

    def _iter_dict_nodes(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from _iter_dict_nodes(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from _iter_dict_nodes(item)

    found_node = None
    if node_id:
        for node in _iter_dict_nodes(flow):
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            node_kind = (data.get("kind") or node.get("kind") or node.get("type") or "").strip()
            if node_kind == "agent_assign" and str(node.get("id")) == str(node_id):
                found_node = node
                break

    if not found_node:
        for node in _iter_dict_nodes(flow):
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            node_kind = (data.get("kind") or node.get("kind") or node.get("type") or "").strip()
            if node_kind == "agent_assign":
                found_node = node
                break

    if found_node:
        data = found_node.get("data") if isinstance(found_node.get("data"), dict) else {}
        auto_sequence_raw = (
            data.get("auto_sequence")
            if data.get("auto_sequence") is not None
            else (
                data.get("sequence_assign")
                if data.get("sequence_assign") is not None
                else data.get("round_robin")
            )
        )
        return {
            "enabled": True,
            "auto_sequence": _to_bool(auto_sequence_raw, default=True),
            "team_manager_user_id": _to_int(
                data.get("team_manager_user_id")
                or data.get("selected_team_manager_user_id")
                or data.get("team_manager_id")
                or data.get("selected_team")
            ),
            "agent_user_id": _to_int(
                data.get("agent_user_id")
                or data.get("selected_agent_user_id")
                or data.get("agent_id")
                or data.get("selected_agent")
            ),
        }
    return {"enabled": False}


def _resolve_agent_assignment(bot, node_id=None):
    cfg = _extract_agent_assign_config(bot, node_id)
    if not cfg.get("enabled"):
        return None, None

    seller = bot.seller_user
    team_managers_qs = TeamMember.objects.select_related("user").filter(
        seller_user=seller,
        member_type=TeamMember.MEMBER_TYPE_TEAM_MANAGER,
        user__status=User.STATUS_ACTIVE,
    )
    agents_qs = TeamMember.objects.select_related("user", "parent_team_manager").filter(
        seller_user=seller,
        member_type=TeamMember.MEMBER_TYPE_AGENT,
        user__status=User.STATUS_ACTIVE,
    ).order_by("team_member_id")

    selected_tm_id = cfg.get("team_manager_user_id")
    selected_agent_id = cfg.get("agent_user_id")
    auto_sequence = cfg.get("auto_sequence", True)

    team_managers_all = list(team_managers_qs)
    agents_all = list(agents_qs)
    valid_tm_ids = {tm.user_id for tm in team_managers_all}
    valid_agent_ids = {ag.user_id for ag in agents_all}

    # Resolve team manager
    chosen_tm = None
    if selected_tm_id and selected_tm_id in valid_tm_ids:
        chosen_tm = next((tm for tm in team_managers_all if tm.user_id == selected_tm_id), None)

    # Filter available agents under selected team manager if specified
    filtered_agents = agents_all
    if chosen_tm:
        scoped = [a for a in agents_all if a.parent_team_manager_id == chosen_tm.user_id]
        if scoped:
            filtered_agents = scoped

    # Sort agents stably by team_member_id for sequential Round Robin rotation
    filtered_agents = sorted(filtered_agents, key=lambda a: a.team_member_id)

    chosen_agent = None

    if auto_sequence and filtered_agents:
        # Round-robin mode enabled: rotate sequentially (1st request -> agent 1, 2nd -> agent 2, 3rd -> agent 3, 4th -> agent 1...)
        from django.db import transaction
        try:
            with transaction.atomic():
                b_fresh = Bot.objects.select_for_update().get(pk=bot.bot_id)
                cursor = b_fresh.assignment_agent_cursor
                idx = cursor % len(filtered_agents)
                chosen_agent = filtered_agents[idx]
                b_fresh.assignment_agent_cursor = cursor + 1
                b_fresh.save(update_fields=["assignment_agent_cursor"])
                bot.assignment_agent_cursor = cursor + 1
        except Exception:
            idx = bot.assignment_agent_cursor % len(filtered_agents)
            chosen_agent = filtered_agents[idx]
            bot.assignment_agent_cursor += 1
            try:
                bot.save(update_fields=["assignment_agent_cursor"])
            except Exception:
                pass
    else:
        # Round-robin disabled: pick specific agent if specified, else random
        if selected_agent_id and selected_agent_id in valid_agent_ids:
            chosen_agent = next((ag for ag in agents_all if ag.user_id == selected_agent_id), None)
        if not chosen_agent and filtered_agents:
            chosen_agent = random.choice(filtered_agents)

    # Keep team manager in sync with chosen agent's parent team manager
    if chosen_agent and chosen_agent.parent_team_manager_id:
        if not chosen_tm or chosen_tm.user_id != chosen_agent.parent_team_manager_id:
            parent_tm = next((tm for tm in team_managers_all if tm.user_id == chosen_agent.parent_team_manager_id), None)
            if parent_tm:
                chosen_tm = parent_tm

    return (chosen_tm.user if chosen_tm else None, chosen_agent.user if chosen_agent else None)


def _backfill_chat_assignments_for_seller(seller_user):
    """
    Ensure seller chat sessions have valid team assignment.
    Repairs missing/invalid assignment rows so team manager/agent panels can see chats.
    """
    if not seller_user:
        return 0
    valid_tm_ids = set(
        TeamMember.objects.filter(
            seller_user=seller_user,
            member_type=TeamMember.MEMBER_TYPE_TEAM_MANAGER,
            user__status=User.STATUS_ACTIVE,
        ).values_list("user_id", flat=True)
    )
    valid_agent_ids = set(
        TeamMember.objects.filter(
            seller_user=seller_user,
            member_type=TeamMember.MEMBER_TYPE_AGENT,
            user__status=User.STATUS_ACTIVE,
        ).values_list("user_id", flat=True)
    )

    pending = LiveChatSession.objects.select_related("source_bot").filter(seller_user=seller_user)
    updated = 0
    for session in pending:
        if not session.source_bot:
            continue
        current_tm_id = session.assigned_team_manager_id
        current_agent_id = session.assigned_agent_id
        current_valid = (
            (current_tm_id is not None and current_tm_id in valid_tm_ids)
            or (current_agent_id is not None and current_agent_id in valid_agent_ids)
        )
        if current_valid:
            continue

        tm_user, agent_user = _resolve_agent_assignment(session.source_bot)
        if not tm_user and not agent_user:
            continue
        session.assigned_team_manager = tm_user
        session.assigned_agent = agent_user
        session.save(update_fields=["assigned_team_manager", "assigned_agent", "updated_at"])
        updated += 1
    return updated


def _get_team_manager_agent_user_ids(team_manager_user):
    """
    Return active agent user IDs mapped under the given team manager.
    """
    profile = (
        TeamMember.objects.select_related("seller_user")
        .filter(user=team_manager_user, member_type=TeamMember.MEMBER_TYPE_TEAM_MANAGER)
        .first()
    )
    if not profile:
        return []
    return list(
        TeamMember.objects.filter(
            seller_user=profile.seller_user,
            member_type=TeamMember.MEMBER_TYPE_AGENT,
            parent_team_manager=team_manager_user,
            user__status=User.STATUS_ACTIVE,
        ).values_list("user_id", flat=True)
    )


# Default pipeline labels (docs / optional checks). PATCH accepts any status ≤ model max_length.
LIVE_CHAT_PIPELINE_STATUS_VALUES = frozenset({"New Leads", "Contacted", "Qualified", "Won", "Lost"})


def _resolve_support_live_chat_session(user, session_id_raw):
    """
    Session readable/writable by seller owner, assigned team manager, agents under that manager, or assigned agent.
    Raises LiveChatSession.DoesNotExist if forbidden or invalid id.
    """
    try:
        sid = int(str(session_id_raw).strip())
    except (ValueError, TypeError):
        raise LiveChatSession.DoesNotExist
    base_qs = LiveChatSession.objects.select_related("seller_user", "customer", "source_bot")
    role_id = user.role.role_id
    if role_id == UserRole.ROLE_SELLER:
        return base_qs.get(session_id=sid, seller_user=user)
    if role_id == UserRole.ROLE_TEAM_MANAGER:
        managed_agent_ids = _get_team_manager_agent_user_ids(user)
        return (
            base_qs.filter(session_id=sid)
            .filter(Q(assigned_team_manager=user) | Q(assigned_agent_id__in=managed_agent_ids))
            .get()
        )
    if role_id == UserRole.ROLE_AGENT:
        return base_qs.get(session_id=sid, assigned_agent=user)
    raise LiveChatSession.DoesNotExist


def _live_chat_cache_key(session_id: int) -> str:
    return f"live_chat_messages:{session_id}"


def _get_cached_live_chat_messages(session_id: int):
    return cache.get(_live_chat_cache_key(session_id)) or []


def _set_cached_live_chat_messages(session_id: int, messages, timeout_seconds: int = 60 * 60 * 24 * 7):
    # Store as plain dicts so the cache backend can serialize them.
    cache.set(_live_chat_cache_key(session_id), messages, timeout=timeout_seconds)


def _live_chat_store_dir() -> str:
    return get_live_chat_dir()



def _live_chat_session_file(session_id: int) -> str:
    sid = int(session_id)
    from django.conf import settings
    media_dir = os.path.join(settings.MEDIA_ROOT, "live_chat")
    os.makedirs(media_dir, exist_ok=True)
    return os.path.join(media_dir, f"{sid}.json")


def _get_persisted_live_chat_messages(session_id: int):
    sid = int(session_id)
    from django.conf import settings
    candidate_paths = [
        os.path.join(settings.MEDIA_ROOT, "live_chat", f"{sid}.json"),
        os.path.join(_live_chat_store_dir(), f"{sid}.json"),
    ]
    json_msgs = None
    target_found_path = None
    for file_path in candidate_paths:
        try:
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    json_msgs = data
                    target_found_path = file_path
                    break
        except Exception as ex:
            logger.error(f"Error reading live chat JSON file {file_path}: {ex}")

    if json_msgs is None:
        return None

    # Sync file to all candidate paths if missing in one of them
    for file_path in candidate_paths:
        if file_path != target_found_path and not os.path.exists(file_path):
            try:
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(json_msgs, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    return json_msgs


def _detect_bot_attachment_type(mime_type: str, file_name: str) -> str:
    mime = (mime_type or "").lower()
    name = (file_name or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/") or name.endswith(".txt"):
        return "text"
    return "document"


def _extract_bot_upload_rel_path(file_url_or_path: str) -> str:
    raw = (file_url_or_path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("bot_uploads/"):
        return raw
    if raw.startswith("media/bot_uploads/"):
        return raw[len("media/") :]
    if "/media/" in raw:
        return raw.split("/media/", 1)[-1].split("?")[0]
    return ""


def _flow_message_to_chat_row(m, idx: int, base_time):
    if not isinstance(m, dict):
        return None
    m_type = (m.get("type") or "").strip()
    text = (m.get("text") or "").strip()
    file_url = (m.get("file_url") or "").strip()
    file_name = (m.get("file_name") or "").strip()
    mime_type = (m.get("mime_type") or "").strip()
    attachment_type = (m.get("attachment_type") or "").strip()
    if not text and not file_url:
        return None
    sender_type = LiveChatMessage.SENDER_SELLER if m_type == "bot" else LiveChatMessage.SENDER_CUSTOMER
    row = {
        "message_id": uuid.uuid4().hex,
        "sender_type": sender_type,
        "message": text or (f"Uploaded: {file_name}" if file_name else "Uploaded file"),
        "created_at": (base_time + timedelta(milliseconds=idx * 50)).isoformat(),
    }
    file_path = (m.get("file_path") or "").strip()
    if not file_path:
        file_path = _extract_bot_upload_rel_path(file_url)
    if file_url:
        row["file_url"] = file_url
        if file_name:
            row["file_name"] = file_name
        if mime_type:
            row["mime_type"] = mime_type
        if attachment_type:
            row["attachment_type"] = attachment_type
        if file_path:
            row["file_path"] = file_path
    return row


def _set_persisted_live_chat_messages(session_id: int, messages):
    sid = int(session_id)
    from django.conf import settings
    candidate_paths = [
        os.path.join(settings.MEDIA_ROOT, "live_chat", f"{sid}.json"),
        os.path.join(_live_chat_store_dir(), f"{sid}.json"),
    ]
    for file_path in candidate_paths:
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            tmp_path = f"{file_path}.tmp_{uuid.uuid4().hex[:6]}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, file_path)
        except Exception as ex:
            logger.error(f"Failed writing live chat JSON file {file_path}: {ex}")


def _save_live_chat_message(session_or_id, sender_type: str, message_text: str, media_payload: dict = None):
    """
    Writes a chat message exclusively into the JSON session file store inside media/live_chat/<session_id>.json.
    (No database insertions into LiveChatMessage table, as requested).
    Accepts either a LiveChatSession instance or integer session_id.
    """
    from agro_app.models import LiveChatSession
    try:
        if isinstance(session_or_id, LiveChatSession):
            session = session_or_id
            sid = int(session.session_id)
        else:
            sid = int(session_or_id)
            session = LiveChatSession.objects.get(session_id=sid)

        msg_body = (message_text or "").strip()
        if not msg_body and not media_payload:
            return None

        display_msg = msg_body or (f"Uploaded file: {media_payload.get('file_name')}" if media_payload else "Attachment")

        cached = _get_persisted_live_chat_messages(sid) or []
        new_row = {
            "message_id": uuid.uuid4().hex,
            "sender_type": sender_type,
            "message": display_msg,
            "created_at": timezone.now().isoformat(),
        }
        if media_payload:
            new_row["file_url"] = media_payload.get("file_url")
            new_row["file_name"] = media_payload.get("file_name")
            new_row["mime_type"] = media_payload.get("mime_type")
            new_row["attachment_type"] = _detect_bot_attachment_type(media_payload.get("mime_type"), media_payload.get("file_name"))

        cached.append(new_row)
        _set_persisted_live_chat_messages(sid, cached)

        try:
            vp = dict(session.visitor_payload or {})
            st_clean = str(sender_type).strip().lower()
            if st_clean in ("customer", "user", "visitor"):
                vp["is_read"] = False
            elif st_clean == "seller":
                vp["is_read"] = True
                vp["last_read_message_id"] = str(new_row.get("message_id") or "")
                vp["seller_last_read_at"] = timezone.now().isoformat()
            session.visitor_payload = vp
            session.save(update_fields=["visitor_payload", "updated_at"])
        except Exception:
            pass

        return new_row
    except Exception as ex:
        logger.error(f"Failed in _save_live_chat_message for session {session_or_id}: {ex}")
        return None


def _get_or_create_live_chat_session_for_whatsapp(seller_user, customer_phone: str, customer_name: str = ""):
    """
    Gets or creates a Customer and LiveChatSession for WhatsApp phone number.
    Ensures that sent template messages or incoming WhatsApp messages create the live chat room properly.
    """
    from agro_app.models import LiveChatSession, Customer, Bot
    phone = str(customer_phone or "").strip()
    if not phone:
        return None, False

    customer = Customer.objects.filter(seller_user=seller_user, mobile_number=phone).first()
    if not customer:
        customer = Customer.objects.filter(seller_user=seller_user, whatsapp_number=phone).first()

    if not customer:
        c_name = customer_name or f"WA Customer {phone[-4:]}"
        bot = Bot.objects.filter(seller_user=seller_user, is_published=True).first()
        customer = Customer.objects.create(
            seller_user=seller_user,
            full_name=c_name,
            mobile_number=phone,
            whatsapp_number=phone,
            created_via_bot=True,
            source_bot=bot
        )
    elif customer_name and (not customer.full_name or customer.full_name.startswith("WA Customer")):
        customer.full_name = customer_name
        customer.save(update_fields=["full_name"])

    session = LiveChatSession.objects.filter(seller_user=seller_user, customer=customer).first()
    created = False
    if not session:
        bot = Bot.objects.filter(seller_user=seller_user, is_published=True).first()
        session = LiveChatSession.objects.create(
            seller_user=seller_user,
            customer=customer,
            source_bot=bot,
            visitor_name=customer.full_name or customer_name,
            visitor_mobile_number=phone,
            lead_pipeline_status="New Leads",
            visitor_payload={"lead_source": "WhatsApp Campaign/Template"}
        )
        created = True
    return session, created


def _clear_live_chat_session_storage(session_id: int) -> None:
    """Drop cache + file-backed transcript after session delete."""
    sid = int(session_id)
    try:
        cache.delete(_live_chat_cache_key(sid))
    except Exception:
        logger.exception("Failed to clear live chat cache for session %s", sid)
    fp = _live_chat_session_file(sid)
    try:
        if os.path.exists(fp):
            os.remove(fp)
    except Exception:
        logger.exception("Failed to remove live chat transcript file for session %s", sid)


# -----------------------------
# Data Hub (JSON-file storage)
# -----------------------------

def _data_hub_store_dir(seller_user_id: int) -> str:
    return get_data_hub_dir(seller_user_id)



def _data_hub_tables_file(seller_user_id: int) -> str:
    return os.path.join(_data_hub_store_dir(seller_user_id), "tables.json")


def _data_hub_table_rows_file(seller_user_id: int, table_id: str) -> str:
    safe_table_id = str(table_id).strip()
    return os.path.join(_data_hub_store_dir(seller_user_id), f"table_{safe_table_id}.json")


def _read_json_file(path: str, default):
    try:
        if not os.path.exists(path):
            # Check legacy paths and auto-migrate
            old_candidates = [
                path.replace(os.path.join("media", "data_hub"), os.path.join("persistent_data", "data_hub")),
                path.replace(os.path.join("media", "data_hub"), os.path.join("/var/tmp/agrobridge_data", "data_hub")),
            ]
            for old_p in old_candidates:
                if os.path.exists(old_p):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    shutil.copy2(old_p, path)
                    logger.info(f"DataHub: Auto-migrated JSON file from {old_p} to {path}")
                    break
            else:
                return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as ex:
        logger.error(f"Failed reading JSON file {path}: {ex}")
        return default


def _write_json_file_atomic(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, path)


def _normalize_data_hub_col_key(s: str) -> str:
    """Align UI labels like 'Order no.' with JSON keys like order_no."""
    if not s:
        return ""
    t = str(s).strip().lower()
    t = re.sub(r"[^a-z0-9]+", "_", t)
    t = re.sub(r"_+", "_", t).strip("_")
    return t


def _order_no_from_catalogue_row(row):
    if not isinstance(row, dict):
        return ""
    for key in ("order_no", "orderNo", "OrderNo", "ORDER_NO", "order_number"):
        raw = row.get(key)
        if raw is None:
            continue
        s = str(raw).strip()
        if s:
            return s
    return ""


def _create_pending_online_order_for_catalogue(bot, table_id, record, seller_user_id):
    """
    Catalogue checkout: create MySQL online_order row (pending) before/with Data Hub JSON row.
    Caller must ensure `record` includes cart_items and row_id.
    """
    row_id = str(record.get("row_id") or "").strip()
    if not row_id:
        raise ValueError("record.row_id is required")

    order_no = _order_no_from_catalogue_row(record)
    if not order_no:
        order_no = generate_public_order_no()
    for _ in range(24):
        if not OnlineOrder.objects.filter(order_no=order_no).exists():
            break
        order_no = generate_public_order_no()
    else:
        order_no = f"ORD-{uuid.uuid4().hex[:12].upper()}"

    try:
        total_amt = float(record.get("total_amount") or 0)
    except (TypeError, ValueError):
        total_amt = 0.0

    return OnlineOrder.objects.create(
        seller_user_id=seller_user_id,
        source_bot=bot,
        data_hub_table_id=str(table_id).strip(),
        data_hub_row_id=row_id,
        customer_name=str(record.get("customer_name") or record.get("name") or ""),
        customer_phone=str(record.get("customer_phone") or record.get("phone") or ""),
        customer_email=str(record.get("customer_email") or record.get("email") or ""),
        customer_address=str(record.get("customer_address") or record.get("address") or ""),
        total_amount=total_amt,
        status=OnlineOrder.STATUS_PENDING,
        order_no=order_no,
    )


def _fulfill_catalogue_order_line_items(order, items):
    """
    Stock deduction + OnlineOrderItem rows. Returns computed total_amount.
    Raises ValueError on stock / batch issues.
    """
    total_amount = 0.0
    for it in items:
        pid = it["product_id"]
        requested_batch_id = it.get("batch_id")
        need = it["qty"]
        unit_price = it["unit_price"]

        if requested_batch_id:
            try:
                b = (
                    Batch.objects.select_for_update()
                    .get(
                        batch_id=requested_batch_id,
                        product_id=pid,
                        is_active=True,
                    )
                )
            except Batch.DoesNotExist:
                raise ValueError(
                    f"Selected batch {requested_batch_id} not found for product_id={pid}."
                )

            available = int(b.available_quantity or 0)
            if available < need:
                raise ValueError(
                    f"Insufficient stock in selected batch {requested_batch_id} for product_id={pid}. "
                    f"Needed {need}, available {available}."
                )

            b.available_quantity = available - need
            b.save(update_fields=["available_quantity"])
            InventoryTransaction.objects.create(
                product_id=pid,
                batch=b,
                batch_name=b.batch_name or "",
                quantity=need,
                transaction_type=InventoryTransaction.TRANSACTION_TYPE_OUT,
                reason="Online order approval",
                reference_id=f"ONLINE_ORDER-{order.order_id}",
                date=timezone.now().date(),
                expiry_date=b.expiry_date,
                manufacturing_date=b.manufacturing_date,
            )
        else:
            batches_qs = (
                Batch.objects.select_for_update()
                .filter(product_id=pid, is_active=True, available_quantity__gt=0)
                .order_by("expiry_date", "batch_id")
            )
            available_total = sum(int(b.available_quantity or 0) for b in batches_qs)
            if available_total < need:
                raise ValueError(f"Insufficient stock for product_id={pid}. Needed {need}, available {available_total}.")

            remaining = need
            for b in batches_qs:
                if remaining <= 0:
                    break
                take = min(int(b.available_quantity or 0), remaining)
                if take <= 0:
                    continue
                b.available_quantity = int(b.available_quantity or 0) - take
                b.save(update_fields=["available_quantity"])
                InventoryTransaction.objects.create(
                    product_id=pid,
                    batch=b,
                    batch_name=b.batch_name or "",
                    quantity=take,
                    transaction_type=InventoryTransaction.TRANSACTION_TYPE_OUT,
                    reason="Online order approval",
                    reference_id=f"ONLINE_ORDER-{order.order_id}",
                    date=timezone.now().date(),
                    expiry_date=b.expiry_date,
                    manufacturing_date=b.manufacturing_date,
                )
                remaining -= take

        line_total = float(unit_price) * float(need)
        total_amount += line_total
        OnlineOrderItem.objects.create(
            order=order,
            product_id=pid,
            quantity=need,
            unit_price=unit_price,
            line_total=line_total,
        )

    return total_amount


def _normalize_table_name(name: str) -> str:
    return (name or "").strip()


def _normalize_column_name(name: str) -> str:
    return (name or "").strip()


class DataHubTablesAPIView(APIView):
    """
    Seller Data Hub tables stored as JSON on disk (NOT in DB).

    GET  -> list tables
    POST -> create table with columns
    PUT  -> update table schema (add/edit columns)
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def _resolve_seller_user_id(self, request) -> int:
        if request.user.role.role_id == UserRole.ROLE_SELLER:
            return int(request.user.id)
        profile = TeamMember.objects.filter(user=request.user).select_related("seller_user").first()
        if profile and profile.seller_user_id:
            return int(profile.seller_user_id)
        return 0

    def get(self, request):
        seller_id = self._resolve_seller_user_id(request)
        if not seller_id:
            return Response({"success": False, "message": "Seller profile not found."}, status=status.HTTP_400_BAD_REQUEST)
        tables = _read_json_file(_data_hub_tables_file(seller_id), default=[])
        if not isinstance(tables, list):
            tables = []
        return Response({"success": True, "data": tables}, status=status.HTTP_200_OK)

    def post(self, request):
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can write to this endpoint."}, status=status.HTTP_403_FORBIDDEN)
        seller_id = int(request.user.id)
        body = request.data or {}
        table_name = _normalize_table_name(body.get("table_name"))
        columns = body.get("columns") or []

        if not table_name:
            return Response({"success": False, "message": "table_name is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(columns, list) or len(columns) == 0:
            return Response({"success": False, "message": "columns must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

        normalized_cols = []
        for c in columns:
            if not isinstance(c, dict):
                continue
            col_name = _normalize_column_name(c.get("name") or c.get("column_name"))
            col_type = (c.get("type") or "STRING").strip().upper()
            searchable = bool(c.get("searchable", False))
            masked = bool(c.get("masked", False))
            field_type = (c.get("field_type") or c.get("fields") or "OTHERS").strip().upper()
            if not col_name:
                continue
            normalized_cols.append(
                {
                    "name": col_name,
                    "type": col_type,
                    "searchable": searchable,
                    "masked": masked,
                    "field_type": field_type,
                }
            )

        if len(normalized_cols) == 0:
            return Response({"success": False, "message": "At least one valid column is required."}, status=status.HTTP_400_BAD_REQUEST)

        tables_path = _data_hub_tables_file(seller_id)
        tables = _read_json_file(tables_path, default=[])
        if not isinstance(tables, list):
            tables = []

        # Prevent duplicate table names per seller
        if any((t.get("table_name") or "").strip().lower() == table_name.lower() for t in tables if isinstance(t, dict)):
            return Response({"success": False, "message": "Table with this name already exists."}, status=status.HTTP_400_BAD_REQUEST)

        table_id = uuid.uuid4().hex
        now = timezone.now().isoformat()
        table_obj = {
            "table_id": table_id,
            "table_name": table_name,
            "columns": normalized_cols,
            "created_at": now,
            "updated_at": now,
        }
        tables.insert(0, table_obj)
        _write_json_file_atomic(tables_path, tables)

        # Initialize rows file
        rows_path = _data_hub_table_rows_file(seller_id, table_id)
        _write_json_file_atomic(rows_path, {"rows": []})

        return Response({"success": True, "data": table_obj}, status=status.HTTP_201_CREATED)

    def put(self, request):
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can write to this endpoint."}, status=status.HTTP_403_FORBIDDEN)
        seller_id = int(request.user.id)
        body = request.data or {}
        table_id = (body.get("table_id") or "").strip()
        table_name = _normalize_table_name(body.get("table_name"))
        columns = body.get("columns") or []

        if not table_id:
            return Response({"success": False, "message": "table_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not table_name:
            return Response({"success": False, "message": "table_name is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(columns, list) or len(columns) == 0:
            return Response({"success": False, "message": "columns must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

        normalized_cols = []
        for c in columns:
            if not isinstance(c, dict):
                continue
            col_name = _normalize_column_name(c.get("name") or c.get("column_name"))
            col_type = (c.get("type") or "STRING").strip().upper()
            searchable = bool(c.get("searchable", False))
            masked = bool(c.get("masked", False))
            field_type = (c.get("field_type") or c.get("fields") or "OTHERS").strip().upper()
            if not col_name:
                continue
            normalized_cols.append(
                {
                    "name": col_name,
                    "type": col_type,
                    "searchable": searchable,
                    "masked": masked,
                    "field_type": field_type,
                }
            )

        if len(normalized_cols) == 0:
            return Response({"success": False, "message": "At least one valid column is required."}, status=status.HTTP_400_BAD_REQUEST)

        tables_path = _data_hub_tables_file(seller_id)
        tables = _read_json_file(tables_path, default=[])
        if not isinstance(tables, list):
            tables = []

        idx = -1
        for i, t in enumerate(tables):
            if isinstance(t, dict) and str(t.get("table_id")) == table_id:
                idx = i
                break

        if idx < 0:
            return Response({"success": False, "message": "Table not found."}, status=status.HTTP_404_NOT_FOUND)

        # Prevent duplicate name with other tables
        for i, t in enumerate(tables):
            if i == idx or not isinstance(t, dict):
                continue
            if (t.get("table_name") or "").strip().lower() == table_name.lower():
                return Response({"success": False, "message": "Table with this name already exists."}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now().isoformat()
        existing = tables[idx]
        tables[idx] = {
            **existing,
            "table_id": table_id,
            "table_name": table_name,
            "columns": normalized_cols,
            "updated_at": now,
        }
        _write_json_file_atomic(tables_path, tables)
        return Response({"success": True, "data": tables[idx]}, status=status.HTTP_200_OK)

    def delete(self, request):
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can write to this endpoint."}, status=status.HTTP_403_FORBIDDEN)
        seller_id = int(request.user.id)
        table_id = (request.query_params.get("table_id") or "").strip()
        if not table_id:
            return Response({"success": False, "message": "table_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        tables_path = _data_hub_tables_file(seller_id)
        tables = _read_json_file(tables_path, default=[])
        if not isinstance(tables, list):
            tables = []

        idx = -1
        for i, t in enumerate(tables):
            if isinstance(t, dict) and str(t.get("table_id")) == table_id:
                idx = i
                break

        if idx < 0:
            return Response({"success": False, "message": "Table not found."}, status=status.HTTP_404_NOT_FOUND)

        tables.pop(idx)
        _write_json_file_atomic(tables_path, tables)

        # Also delete the rows file for this table if present.
        rows_path = _data_hub_table_rows_file(seller_id, table_id)
        try:
            if os.path.exists(rows_path):
                os.remove(rows_path)
        except Exception:
            # Non-fatal: table entry already removed; keep API success.
            pass

        return Response({"success": True}, status=status.HTTP_200_OK)


class DataHubTableRowsAPIView(APIView):
    """
    Rows for a specific Data Hub table (JSON-file storage).

    GET  -> list rows
    POST -> insert row (dict: {colName: value})
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def _resolve_seller_user_id(self, request) -> int:
        if request.user.role.role_id == UserRole.ROLE_SELLER:
            return int(request.user.id)
        profile = TeamMember.objects.filter(user=request.user).select_related("seller_user").first()
        if profile and profile.seller_user_id:
            return int(profile.seller_user_id)
        return 0

    def get(self, request):
        seller_id = self._resolve_seller_user_id(request)
        if not seller_id:
            return Response({"success": False, "message": "Seller profile not found."}, status=status.HTTP_400_BAD_REQUEST)
        table_id = (request.query_params.get("table_id") or "").strip()
        if not table_id:
            return Response({"success": False, "message": "table_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        rows_path = _data_hub_table_rows_file(seller_id, table_id)
        payload = _read_json_file(rows_path, default={"rows": []})
        rows = payload.get("rows") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []

        role_id = request.user.role.role_id
        if role_id == UserRole.ROLE_SELLER:
            filtered_rows = rows
        elif role_id == UserRole.ROLE_TEAM_MANAGER:
            managed_agent_ids = set(_get_team_manager_agent_user_ids(request.user))
            filtered_rows = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                mgr_id = r.get("assigned_team_manager_id")
                agt_id = r.get("assigned_agent_id")
                if (mgr_id and int(mgr_id) == int(request.user.id)) or (agt_id and int(agt_id) in managed_agent_ids):
                    filtered_rows.append(r)
        elif role_id == UserRole.ROLE_AGENT:
            filtered_rows = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                agt_id = r.get("assigned_agent_id")
                if agt_id and int(agt_id) == int(request.user.id):
                    filtered_rows.append(r)
        else:
            filtered_rows = []

        return Response({"success": True, "data": filtered_rows}, status=status.HTTP_200_OK)

    def post(self, request):
        if request.user.role.role_id not in [UserRole.ROLE_SELLER, UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            return Response({"success": False, "message": "Only seller/team users can write to this endpoint."}, status=status.HTTP_403_FORBIDDEN)
        seller_id = self._resolve_seller_user_id(request)
        if not seller_id:
            return Response({"success": False, "message": "Seller profile not found."}, status=status.HTTP_400_BAD_REQUEST)
        body = request.data or {}
        table_id = (body.get("table_id") or "").strip()
        row = body.get("row")
        if not table_id:
            return Response({"success": False, "message": "table_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(row, dict):
            return Response({"success": False, "message": "row must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        rows_path = _data_hub_table_rows_file(seller_id, table_id)
        payload = _read_json_file(rows_path, default={"rows": []})
        if not isinstance(payload, dict):
            payload = {"rows": []}
        rows = payload.get("rows")
        if not isinstance(rows, list):
            rows = []

        # Get table definition to identify phone and email fields
        tables_path = _data_hub_tables_file(seller_id)
        tables = _read_json_file(tables_path, default=[])
        table_columns = []
        if isinstance(tables, list):
            for t in tables:
                if isinstance(t, dict) and str(t.get("table_id")) == str(table_id):
                    table_columns = t.get("columns") or []
                    break

        if _row_contains_duplicate_phone_or_email(rows, row, table_columns):
            return Response(
                {"success": False, "message": "A customer with this phone number or email already exists in this collection."},
                status=status.HTTP_400_BAD_REQUEST
            )

        now = timezone.now().isoformat()
        row_id = uuid.uuid4().hex
        record = {"row_id": row_id, "inserted_on": now, "updated_on": now, **row}
        
        # Inherit assigned agent and team manager if a matching live chat session exists
        session = _find_matching_live_chat_session(seller_id, record)
        if session:
            record["assigned_team_manager_id"] = int(session.assigned_team_manager.id) if session.assigned_team_manager else None
            record["assigned_team_manager_name"] = str(session.assigned_team_manager.full_name or "").strip() if session.assigned_team_manager else ""
            record["assigned_agent_id"] = int(session.assigned_agent.id) if session.assigned_agent else None
            record["assigned_agent_name"] = str(session.assigned_agent.full_name or "").strip() if session.assigned_agent else ""
            if not record["assigned_team_manager_name"] and session.visitor_payload:
                record["assigned_team_manager_name"] = str(session.visitor_payload.get("manual_team_name") or "").strip()
            if not record["assigned_agent_name"] and session.visitor_payload:
                record["assigned_agent_name"] = str(session.visitor_payload.get("manual_agent_name") or "").strip()
        else:
            # Fallback default assignments based on the logged-in user's role
            role_id = request.user.role.role_id
            if role_id == UserRole.ROLE_TEAM_MANAGER:
                record["assigned_team_manager_id"] = int(request.user.id)
                record["assigned_team_manager_name"] = str(request.user.full_name or "").strip()
                record["assigned_agent_id"] = None
                record["assigned_agent_name"] = ""
            elif role_id == UserRole.ROLE_AGENT:
                record["assigned_agent_id"] = int(request.user.id)
                record["assigned_agent_name"] = str(request.user.full_name or "").strip()
                # Get the agent's manager
                agent_profile = TeamMember.objects.filter(user=request.user, member_type=TeamMember.MEMBER_TYPE_AGENT).select_related("parent_team_manager").first()
                if agent_profile and agent_profile.parent_team_manager:
                    record["assigned_team_manager_id"] = int(agent_profile.parent_team_manager.id)
                    record["assigned_team_manager_name"] = str(agent_profile.parent_team_manager.full_name or "").strip()
                else:
                    record["assigned_team_manager_id"] = None
                    record["assigned_team_manager_name"] = ""

        rows.insert(0, record)
        payload["rows"] = rows
        _write_json_file_atomic(rows_path, payload)
        return Response({"success": True, "data": record}, status=status.HTTP_201_CREATED)


class DataHubTableRowDetailAPIView(APIView):
    """
    PUT    -> update row
    DELETE -> delete row
    """

    permission_classes = [IsAuthenticated, IsSellerOrTeamManagerOrAgent]
    parser_classes = [JSONParser]

    def _resolve_seller_user_id(self, request) -> int:
        if request.user.role.role_id == UserRole.ROLE_SELLER:
            return int(request.user.id)
        profile = TeamMember.objects.filter(user=request.user).select_related("seller_user").first()
        if profile and profile.seller_user_id:
            return int(profile.seller_user_id)
        return 0

    def put(self, request):
        seller_id = self._resolve_seller_user_id(request)
        if not seller_id:
            return Response({"success": False, "message": "Seller profile not found."}, status=status.HTTP_400_BAD_REQUEST)
        body = request.data or {}
        table_id = (body.get("table_id") or "").strip()
        row_id = (body.get("row_id") or "").strip()
        patch = body.get("patch")
        if not table_id or not row_id:
            return Response({"success": False, "message": "table_id and row_id are required."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(patch, dict):
            return Response({"success": False, "message": "patch must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        rows_path = _data_hub_table_rows_file(seller_id, table_id)
        payload = _read_json_file(rows_path, default={"rows": []})
        if not isinstance(payload, dict):
            payload = {"rows": []}
        rows = payload.get("rows")
        if not isinstance(rows, list):
            rows = []

        found = None
        now = timezone.now().isoformat()
        for idx, r in enumerate(rows):
            if isinstance(r, dict) and str(r.get("row_id")) == row_id:
                updated = {**r, **patch, "updated_on": now}
                rows[idx] = updated
                found = updated
                break

        if not found:
            return Response({"success": False, "message": "Row not found."}, status=status.HTTP_404_NOT_FOUND)

        payload["rows"] = rows
        _write_json_file_atomic(rows_path, payload)

        _sync_data_hub_assignment_to_session(seller_id, found)

        return Response({"success": True, "data": found}, status=status.HTTP_200_OK)

    def delete(self, request):
        seller_id = self._resolve_seller_user_id(request)
        if not seller_id:
            return Response({"success": False, "message": "Seller profile not found."}, status=status.HTTP_400_BAD_REQUEST)
        table_id = (request.query_params.get("table_id") or "").strip()
        row_id = (request.query_params.get("row_id") or "").strip()
        if not table_id or not row_id:
            return Response({"success": False, "message": "table_id and row_id are required."}, status=status.HTTP_400_BAD_REQUEST)

        rows_path = _data_hub_table_rows_file(seller_id, table_id)
        payload = _read_json_file(rows_path, default={"rows": []})
        if not isinstance(payload, dict):
            payload = {"rows": []}
        rows = payload.get("rows")
        if not isinstance(rows, list):
            rows = []

        new_rows = [r for r in rows if not (isinstance(r, dict) and str(r.get("row_id")) == row_id)]
        if len(new_rows) == len(rows):
            return Response({"success": False, "message": "Row not found."}, status=status.HTTP_404_NOT_FOUND)

        payload["rows"] = new_rows
        _write_json_file_atomic(rows_path, payload)
        return Response({"success": True}, status=status.HTTP_200_OK)


class DataHubPublicRowsAPIView(APIView):
    """
    Public endpoint for bot widget execution.

    POST body:
      {
        "bot_id": "<bot_id>",
        "table_id": "<table_id>",
        "operation": "INSERT" | "UPDATE" | "DELETE" | "SEARCH",
        "row": {...},            # for INSERT
        "row_id": "<row_id>",    # for UPDATE/DELETE
        "patch": {...},        # for UPDATE
        "filters": [            # for SEARCH: AND semantics
          {"column": "name", "operator": "IS", "value": "..."},
        ]
      }
    """

    permission_classes = [AllowAny]
    parser_classes = [JSONParser]

    def post(self, request):
        payload = request.data or {}
        bot_id = (payload.get("bot_id") or "").strip()
        table_id = (payload.get("table_id") or "").strip()
        operation = (payload.get("operation") or "INSERT").strip().upper()

        if not bot_id or not table_id:
            return Response({"success": False, "message": "bot_id and table_id are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.select_related("seller_user").get(bot_id=bot_id, is_published=True)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found or not published."}, status=status.HTTP_404_NOT_FOUND)

        seller_id = int(bot.seller_user_id)

        if operation == "INSERT":
            row = payload.get("row")
            if not isinstance(row, dict):
                return Response({"success": False, "message": "row must be an object for INSERT."}, status=status.HTTP_400_BAD_REQUEST)
            rows_path = _data_hub_table_rows_file(seller_id, table_id)
            store = _read_json_file(rows_path, default={"rows": []})
            if not isinstance(store, dict):
                store = {"rows": []}
            rows = store.get("rows")
            if not isinstance(rows, list):
                rows = []
            now = timezone.now().isoformat()
            row_id = uuid.uuid4().hex
            record = {"row_id": row_id, "inserted_on": now, "updated_on": now, **row}
            
            # Inherit assigned agent and team manager if a matching live chat session exists
            session = _find_matching_live_chat_session(seller_id, record)
            if session:
                record["assigned_team_manager_id"] = int(session.assigned_team_manager.id) if session.assigned_team_manager else None
                record["assigned_team_manager_name"] = str(session.assigned_team_manager.full_name or "").strip() if session.assigned_team_manager else ""
                record["assigned_agent_id"] = int(session.assigned_agent.id) if session.assigned_agent else None
                record["assigned_agent_name"] = str(session.assigned_agent.full_name or "").strip() if session.assigned_agent else ""
                if not record["assigned_team_manager_name"] and session.visitor_payload:
                    record["assigned_team_manager_name"] = str(session.visitor_payload.get("manual_team_name") or "").strip()
                if not record["assigned_agent_name"] and session.visitor_payload:
                    record["assigned_agent_name"] = str(session.visitor_payload.get("manual_agent_name") or "").strip()
            cart_probe = row.get("cart_items") or row.get("items") or row.get("cart")
            is_catalogue_checkout = isinstance(cart_probe, list) and len(cart_probe) > 0

            if is_catalogue_checkout:
                pending_order = None
                try:
                    pending_order = _create_pending_online_order_for_catalogue(bot, table_id, record, seller_id)
                    if pending_order.order_no:
                        record["order_no"] = pending_order.order_no
                    rows.insert(0, record)
                    store["rows"] = rows
                    _write_json_file_atomic(rows_path, store)
                except Exception:
                    if pending_order is not None:
                        OnlineOrder.objects.filter(pk=pending_order.pk).delete()
                    logging.getLogger(__name__).exception("Catalogue checkout: pending OnlineOrder or Data Hub write failed")
                    return Response(
                        {"success": False, "message": "Could not complete checkout. Please try again."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
            else:
                rows.insert(0, record)
                store["rows"] = rows
                _write_json_file_atomic(rows_path, store)

            return Response({"success": True, "data": record}, status=status.HTTP_201_CREATED)

        if operation == "UPDATE":
            row_id = (payload.get("row_id") or "").strip()
            patch = payload.get("patch")
            if not row_id or not isinstance(patch, dict):
                return Response({"success": False, "message": "row_id and patch are required for UPDATE."}, status=status.HTTP_400_BAD_REQUEST)
            rows_path = _data_hub_table_rows_file(seller_id, table_id)
            store = _read_json_file(rows_path, default={"rows": []})
            if not isinstance(store, dict):
                store = {"rows": []}
            rows = store.get("rows")
            if not isinstance(rows, list):
                rows = []
            now = timezone.now().isoformat()
            found = None
            for idx, r in enumerate(rows):
                if isinstance(r, dict) and str(r.get("row_id")) == row_id:
                    updated = {**r, **patch, "updated_on": now}
                    rows[idx] = updated
                    found = updated
                    break
            if not found:
                return Response({"success": False, "message": "Row not found."}, status=status.HTTP_404_NOT_FOUND)
            store["rows"] = rows
            _write_json_file_atomic(rows_path, store)

            # Sync update back to session
            _sync_data_hub_assignment_to_session(seller_id, found)

            return Response({"success": True, "data": found}, status=status.HTTP_200_OK)

        if operation == "DELETE":
            row_id = (payload.get("row_id") or "").strip()
            if not row_id:
                return Response({"success": False, "message": "row_id is required for DELETE."}, status=status.HTTP_400_BAD_REQUEST)
            rows_path = _data_hub_table_rows_file(seller_id, table_id)
            store = _read_json_file(rows_path, default={"rows": []})
            if not isinstance(store, dict):
                store = {"rows": []}
            rows = store.get("rows")
            if not isinstance(rows, list):
                rows = []
            new_rows = [r for r in rows if not (isinstance(r, dict) and str(r.get("row_id")) == row_id)]
            if len(new_rows) == len(rows):
                return Response({"success": False, "message": "Row not found."}, status=status.HTTP_404_NOT_FOUND)
            store["rows"] = new_rows
            _write_json_file_atomic(rows_path, store)
            return Response({"success": True}, status=status.HTTP_200_OK)

        if operation == "SEARCH":
            raw_filters = payload.get("filters")
            if not isinstance(raw_filters, list):
                return Response({"success": False, "message": "filters must be an array for SEARCH."}, status=status.HTTP_400_BAD_REQUEST)
            normalized = []
            for f in raw_filters:
                if not isinstance(f, dict):
                    continue
                col = (f.get("column") or "").strip()
                if not col:
                    continue
                op = (f.get("operator") or "IS").strip().upper()
                val = f.get("value")
                normalized.append({"column": col, "operator": op, "value": val})
            if not normalized:
                return Response(
                    {"success": False, "message": "At least one filter with column is required for SEARCH."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            rows_path = _data_hub_table_rows_file(seller_id, table_id)
            store = _read_json_file(rows_path, default={"rows": []})
            if not isinstance(store, dict):
                store = {"rows": []}
            rows = store.get("rows")
            if not isinstance(rows, list):
                rows = []

            def _get_row_value_ci(row: dict, key: str):
                """Match Data Hub column names case-insensitively; also 'Order no.' vs order_no."""
                if not isinstance(row, dict) or not key:
                    return None
                if _normalize_data_hub_col_key(key) == "status":
                    direct_status = (
                        row.get("status")
                        or row.get("Status")
                        or row.get("STATUS")
                        or row.get("order_status")
                        or row.get("orderStatus")
                        or row.get("Order Status")
                    )
                    if direct_status is not None and str(direct_status).strip():
                        return direct_status
                    if isinstance(row.get("cart_items"), list) and len(row.get("cart_items")) > 0:
                        return "Pending"
                if key in row:
                    return row[key]
                kl = str(key).strip().lower()
                want = _normalize_data_hub_col_key(key)
                for rk, rv in row.items():
                    try:
                        if str(rk).strip().lower() == kl:
                            return rv
                        if want and _normalize_data_hub_col_key(str(rk)) == want:
                            return rv
                    except Exception:
                        continue
                return None

            def _cell_str(row, key):
                if not isinstance(row, dict):
                    return ""
                cell = _get_row_value_ci(row, key)
                if cell is None:
                    return ""
                return str(cell).strip()

            def _row_matches(row):
                if not isinstance(row, dict):
                    return False
                for flt in normalized:
                    col = flt["column"]
                    op = flt["operator"]
                    want = flt["value"]
                    s_cell = _cell_str(row, col)
                    s_want = "" if want is None else str(want).strip()
                    if op in ("IS", "EQUALS", "="):
                        if s_cell.lower() != s_want.lower():
                            return False
                    elif op in ("IS_NOT", "NOT_EQUALS", "!="):
                        if s_cell.lower() == s_want.lower():
                            return False
                    elif op == "CONTAINS":
                        if s_want.lower() not in s_cell.lower():
                            return False
                    else:
                        return False
                return True

            found = next((r for r in rows if _row_matches(r)), None)
            if not isinstance(found, dict):
                # Fallback for order-tracking flows:
                # if Data Hub row is missing but Order No filter is provided, resolve from MySQL online_order.
                order_filter = next(
                    (
                        flt
                        for flt in normalized
                        if _normalize_data_hub_col_key(flt.get("column")) in ("order_no", "orderno", "order_number")
                    ),
                    None,
                )
                order_value = ""
                if isinstance(order_filter, dict):
                    order_value = str(order_filter.get("value") or "").strip()
                if order_value:
                    online = (
                        OnlineOrder.objects.filter(seller_user_id=seller_id, order_no__iexact=order_value)
                        .order_by("-created_at", "-order_id")
                        .first()
                    )
                    if online is not None:
                        s = str(online.status or "").strip().lower()
                        if s == OnlineOrder.STATUS_APPROVED:
                            status_text = "Accept"
                        elif s == OnlineOrder.STATUS_REJECTED:
                            status_text = "Reject"
                        else:
                            status_text = "Pending"
                        found = {
                            "order_no": str(online.order_no or ""),
                            "status": status_text,
                            "name": str(online.customer_name or ""),
                            "phone": str(online.customer_phone or ""),
                            "email": str(online.customer_email or ""),
                            "address": str(online.customer_address or ""),
                        }
            return Response({"success": True, "data": found}, status=status.HTTP_200_OK)

        return Response({"success": False, "message": "Invalid operation."}, status=status.HTTP_400_BAD_REQUEST)


class PublicBotProductsAPIView(APIView):
    """
    Public endpoint used by the bot widget catalogue to list seller products + available qty.

    GET ?bot_id=<published_bot_id>
    """

    permission_classes = [AllowAny]
    parser_classes = [JSONParser]

    def get(self, request):
        bot_id = (request.query_params.get("bot_id") or "").strip()
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.select_related("seller_user").get(bot_id=bot_id, is_published=True)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found or not published."}, status=status.HTTP_404_NOT_FOUND)

        seller = bot.seller_user
        products = Product.objects.filter(seller_user=seller).order_by("-created_at")

        batches = (
            Batch.objects.filter(product__in=products, is_active=True)
            .select_related("product")
            .order_by("expiry_date", "batch_id")
        )
        by_product = {}
        for b in batches:
            pid = int(b.product_id)
            agg = by_product.get(pid)
            if not agg:
                agg = {"available_qty": 0, "selling_price": None}
                by_product[pid] = agg
            agg["available_qty"] += int(b.available_quantity or 0)
            if agg["selling_price"] is None and b.selling_price is not None:
                agg["selling_price"] = str(b.selling_price)

        data = []
        for p in products:
            pid = int(p.product_id)
            agg = by_product.get(pid) or {"available_qty": 0, "selling_price": None}
            product_batches = [
                {
                    "batch_id": int(b.batch_id),
                    "batch_name": b.batch_name or "",
                    "available_quantity": int(b.available_quantity or 0),
                    "selling_price": str(b.selling_price) if b.selling_price is not None else "",
                    "expiry_date": b.expiry_date,
                }
                for b in batches
                if int(b.product_id) == pid
            ]
            data.append(
                {
                    "product_id": pid,
                    "product_name": p.product_name,
                    "description": p.description or "",
                    "selling_price": agg["selling_price"] or (str(p.sale_price) if p.sale_price is not None else ""),
                    "available_quantity": int(agg["available_qty"] or 0),
                    "gst": str(p.gst) if p.gst is not None else "",
                    "batches": product_batches,
                }
            )

        return Response({"success": True, "data": data}, status=status.HTTP_200_OK)


def _hub_status_accept(note: str) -> str:
    """Data Hub row `status` text after seller approves (shown in Collections UI)."""
    n = (note or "").strip()
    if len(n) > 500:
        n = n[:500]
    return f"Accept - {n}" if n else "Accept"


def _hub_status_reject(note: str) -> str:
    """Data Hub row `status` text after seller rejects."""
    n = (note or "").strip()
    if len(n) > 500:
        n = n[:500]
    return f"Reject - {n}" if n else "Reject"


class CatalogueApproveOrderAPIView(APIView):
    """
    Seller approves a catalogue order from Data Hub.

    Normal flow: checkout already created `online_order` with status=pending; this sets status=approved,
    writes `online_order_item` rows, and deducts stock. Legacy rows without a pending MySQL order are still supported.

    POST body:
      { "table_id": "<data_hub_table_id>", "row_id": "<row_id>", "note": "<optional remark>" }
    """

    permission_classes = [IsAuthenticated, IsSeller]
    parser_classes = [JSONParser]

    def post(self, request):
        seller = request.user
        body = request.data or {}
        table_id = (body.get("table_id") or "").strip()
        row_id = (body.get("row_id") or "").strip()
        if not table_id or not row_id:
            return Response({"success": False, "message": "table_id and row_id are required."}, status=status.HTTP_400_BAD_REQUEST)

        note_raw = body.get("note")
        if note_raw is None:
            note_raw = body.get("remark")
        seller_note = str(note_raw or "").strip()[:500]
        hub_status = _hub_status_accept(seller_note)

        rows_path = _data_hub_table_rows_file(int(seller.id), table_id)
        payload = _read_json_file(rows_path, default={"rows": []})
        rows = payload.get("rows") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []

        found = None
        found_idx = -1
        for i, r in enumerate(rows):
            if isinstance(r, dict) and str(r.get("row_id")) == row_id:
                found = r
                found_idx = i
                break
        if not found:
            return Response({"success": False, "message": "Row not found."}, status=status.HTTP_404_NOT_FOUND)

        cart_items = found.get("cart_items") or found.get("items") or found.get("cart") or []
        if not isinstance(cart_items, list) or len(cart_items) == 0:
            return Response({"success": False, "message": "cart_items are missing for this order."}, status=status.HTTP_400_BAD_REQUEST)

        items = []
        for it in cart_items:
            if not isinstance(it, dict):
                continue
            pid = it.get("product_id") or it.get("id") or it.get("product")
            batch_id = it.get("batch_id") or it.get("batch") or None
            qty = it.get("qty") or it.get("quantity") or 0
            price = it.get("price") or it.get("unit_price") or it.get("selling_price") or 0
            try:
                pid = int(pid)
                qty = int(qty)
                price = float(price)
                if batch_id not in (None, ""):
                    batch_id = int(batch_id)
                else:
                    batch_id = None
            except Exception:
                continue
            if pid <= 0 or qty <= 0:
                continue
            items.append({"product_id": pid, "batch_id": batch_id, "qty": qty, "unit_price": price})

        if not items:
            return Response({"success": False, "message": "No valid items in cart_items."}, status=status.HTTP_400_BAD_REQUEST)

        from django.db import transaction

        try:
            with transaction.atomic():
                existing = (
                    OnlineOrder.objects.select_for_update()
                    .filter(
                        seller_user=seller,
                        data_hub_table_id=table_id,
                        data_hub_row_id=row_id,
                    )
                    .first()
                )
                if existing and existing.status != OnlineOrder.STATUS_PENDING:
                    return Response(
                        {"success": False, "message": "This order was already approved or rejected."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if existing and existing.status == OnlineOrder.STATUS_PENDING:
                    order = existing
                    total_amount = _fulfill_catalogue_order_line_items(order, items)
                    order.total_amount = total_amount
                    order.status = OnlineOrder.STATUS_APPROVED
                    order.save(update_fields=["total_amount", "status", "order_no", "updated_at"])
                else:
                    order_no = _order_no_from_catalogue_row(found)
                    if not order_no:
                        order_no = generate_public_order_no()
                    for _ in range(24):
                        if not OnlineOrder.objects.filter(order_no=order_no).exists():
                            break
                        order_no = generate_public_order_no()
                    else:
                        order_no = f"ORD-{uuid.uuid4().hex[:12].upper()}"

                    order = OnlineOrder.objects.create(
                        seller_user=seller,
                        source_bot=None,
                        data_hub_table_id=table_id,
                        data_hub_row_id=row_id,
                        customer_name=str(found.get("customer_name") or found.get("name") or ""),
                        customer_phone=str(found.get("customer_phone") or found.get("phone") or ""),
                        customer_email=str(found.get("customer_email") or found.get("email") or ""),
                        customer_address=str(found.get("customer_address") or found.get("address") or ""),
                        status=OnlineOrder.STATUS_APPROVED,
                        order_no=order_no,
                        total_amount=0,
                    )
                    total_amount = _fulfill_catalogue_order_line_items(order, items)
                    order.total_amount = total_amount
                    order.save(update_fields=["total_amount", "order_no", "updated_at"])

                now = timezone.now().isoformat()
                canonical_no = (order.order_no or "").strip() or _order_no_from_catalogue_row(found)
                updated = {**found, "status": hub_status, "order_no": canonical_no, "updated_on": now}
                rows[found_idx] = updated
                if isinstance(payload, dict):
                    payload["rows"] = rows
                else:
                    payload = {"rows": rows}
                _write_json_file_atomic(rows_path, payload)

            return Response(
                {"success": True, "data": {"order_id": order.order_id, "order_no": order.order_no or ""}},
                status=status.HTTP_200_OK,
            )
        except ValueError as e:
            return Response({"success": False, "message": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class CatalogueSellerOrdersAPIView(APIView):
    """
    Seller online orders list (MySQL source of truth).
    Includes Pending / Accept / Reject status and cart line items.
    """

    permission_classes = [IsAuthenticated, IsSeller]

    def get(self, request):
        seller = request.user
        qs = (
            OnlineOrder.objects.filter(seller_user=seller)
            .select_related("source_bot")
            .prefetch_related("items__product")
            .order_by("-created_at", "-order_id")
        )

        # Cache Data Hub rows per table for status text (Accept - note / Reject - note), if available.
        table_row_cache = {}

        def hub_row_status_text(table_id: str, row_id: str) -> str:
            t = str(table_id or "").strip()
            r = str(row_id or "").strip()
            if not t or not r:
                return ""
            if t not in table_row_cache:
                rows_path = _data_hub_table_rows_file(int(seller.id), t)
                payload = _read_json_file(rows_path, default={"rows": []})
                rows = payload.get("rows") if isinstance(payload, dict) else []
                by_row_id = {}
                if isinstance(rows, list):
                    for rr in rows:
                        if isinstance(rr, dict):
                            rid = str(rr.get("row_id") or "").strip()
                            if rid:
                                by_row_id[rid] = rr
                table_row_cache[t] = by_row_id
            row = table_row_cache[t].get(r)
            if not isinstance(row, dict):
                return ""
            s = str(row.get("status") or "").strip()
            return s

        data = []
        for o in qs:
            db_status = str(o.status or "").strip().lower()
            if db_status == OnlineOrder.STATUS_PENDING:
                fallback_status = "Pending"
            elif db_status == OnlineOrder.STATUS_APPROVED:
                fallback_status = "Accept"
            elif db_status == OnlineOrder.STATUS_REJECTED:
                fallback_status = "Reject"
            else:
                fallback_status = db_status.title() if db_status else ""

            display_status = (
                hub_row_status_text(o.data_hub_table_id, o.data_hub_row_id) or fallback_status
            )

            cart_items = []
            for it in o.items.all():
                qty = int(it.quantity or 0)
                unit = float(it.unit_price or 0)
                line_total = float(it.line_total or (unit * qty))
                cart_items.append(
                    {
                        "item_id": int(it.item_id),
                        "product_id": int(it.product_id),
                        "product_name": str(getattr(it.product, "product_name", "") or ""),
                        "qty": qty,
                        "price": unit,
                        "line_total": line_total,
                    }
                )

            data.append(
                {
                    "order_id": int(o.order_id),
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                    "updated_at": o.updated_at.isoformat() if o.updated_at else None,
                    "name": str(o.customer_name or ""),
                    "phone": str(o.customer_phone or ""),
                    "email": str(o.customer_email or ""),
                    "address": str(o.customer_address or ""),
                    "status": display_status,
                    "order_no": str(o.order_no or ""),
                    "total_amount": float(o.total_amount or 0),
                    "source_bot_id": int(o.source_bot_id) if o.source_bot_id else None,
                    "source_bot_name": str(o.source_bot.name or "") if o.source_bot_id else "",
                    "cart_items": cart_items,
                }
            )

        return Response({"success": True, "data": data}, status=status.HTTP_200_OK)


class CatalogueRejectOrderAPIView(APIView):
    """
    Seller rejects an online catalogue order stored in Data Hub JSON.

    POST body:
      { "table_id": "<data_hub_table_id>", "row_id": "<row_id>", "note": "<optional remark>" }
    """

    permission_classes = [IsAuthenticated, IsSeller]
    parser_classes = [JSONParser]

    def post(self, request):
        seller = request.user
        body = request.data or {}
        table_id = (body.get("table_id") or "").strip()
        row_id = (body.get("row_id") or "").strip()
        if not table_id or not row_id:
            return Response({"success": False, "message": "table_id and row_id are required."}, status=status.HTTP_400_BAD_REQUEST)

        note_raw = body.get("note")
        if note_raw is None:
            note_raw = body.get("remark")
        seller_note = str(note_raw or "").strip()[:500]
        hub_status = _hub_status_reject(seller_note)

        rows_path = _data_hub_table_rows_file(int(seller.id), table_id)
        payload = _read_json_file(rows_path, default={"rows": []})
        rows = payload.get("rows") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []

        found = None
        found_idx = -1
        for i, r in enumerate(rows):
            if isinstance(r, dict) and str(r.get("row_id")) == row_id:
                found = r
                found_idx = i
                break
        if not found:
            return Response({"success": False, "message": "Row not found."}, status=status.HTTP_404_NOT_FOUND)

        OnlineOrder.objects.filter(
            seller_user=seller,
            data_hub_table_id=table_id,
            data_hub_row_id=row_id,
            status=OnlineOrder.STATUS_PENDING,
        ).update(status=OnlineOrder.STATUS_REJECTED, updated_at=timezone.now())

        now = timezone.now().isoformat()
        updated = {**found, "status": hub_status, "updated_on": now}
        rows[found_idx] = updated
        if isinstance(payload, dict):
            payload["rows"] = rows
        else:
            payload = {"rows": rows}
        _write_json_file_atomic(rows_path, payload)
        return Response({"success": True}, status=status.HTTP_200_OK)


class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            user = serializer.validated_data["user"]
            refresh = RefreshToken.for_user(user)

            # IMPORTANT: Keep login user payload consistent with other endpoints.
            # Previously we returned only a subset of fields (missing company_name/gst_number/address),
            # which caused Seller Profile business info to "disappear" after logout/login.
            user_data = UserDetailSerializer(user).data

            response_payload = {
                "success": True,
                "message": "Login successful.",
                "token_type": "Bearer",
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": user_data,
            }

            return Response(response_payload, status=status.HTTP_200_OK)

        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:  # pragma: no cover - safeguard
            logger.exception("Unexpected error during login")
            err_msg = str(exc) if getattr(settings, "DEBUG", False) else "Unexpected error occurred. Please try again later."
            return Response(
                {
                    "success": False,
                    "message": err_msg,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class BuyerAPIView(APIView):
    """
    POST: register buyer (public)
    GET: list buyers (admin only)
    """

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAdmin()]
        return [AllowAny()]

    def post(self, request):
        serializer = BuyerRegistrationSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            user = serializer.save()
            return Response(
                {
                    "success": True,
                    "message": "Buyer registered successfully.",
                    "user_id": user.id,
                },
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Buyer registration failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to register buyer at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        buyers = (
            User.objects.select_related("role")
            .filter(role__role_id=UserRole.ROLE_BUYER)
            .order_by("-created_at")
        )
        data = UserDetailSerializer(buyers, many=True).data
        return Response(
            {"success": True, "count": len(data), "data": data},
            status=status.HTTP_200_OK,
        )


class BotAPIView(APIView):
    """
    New feature: Seller Bot Builder

    GET: list bots for authenticated seller
    POST: create bot (seller)
    PUT: update bot (seller) ?bot_id=
    """

    permission_classes = [IsAuthenticated, IsSeller]

    def get(self, request):
        bot_id = (request.query_params.get("bot_id") or "").strip()

        # Detail mode: return full payload (including flow/design) for one bot.
        if bot_id:
            try:
                bot = Bot.objects.get(bot_id=bot_id, seller_user_id=request.user.id)
            except Bot.DoesNotExist:
                return Response({"success": False, "message": "Bot not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response({"success": True, "count": 1, "data": [BotSerializer(bot).data]}, status=status.HTTP_200_OK)

        # List mode: return lightweight data only to avoid heavy JSON sorting/memory pressure.
        bots = (
            Bot.objects.filter(seller_user_id=request.user.id)
            .only("bot_id", "seller_user_id", "name", "is_published", "created_at", "updated_at")
            .order_by("-bot_id")
        )
        data = [
            {
                "bot_id": b.bot_id,
                "seller_user": b.seller_user_id,
                "name": b.name,
                "is_published": b.is_published,
                "created_at": b.created_at,
                "updated_at": b.updated_at,
            }
            for b in bots
        ]
        return Response({"success": True, "count": len(data), "data": data}, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = BotSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            bot = serializer.save(seller_user=request.user)
            return Response(
                {"success": True, "message": "Bot created successfully.", "data": BotSerializer(bot).data},
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            return Response({"success": False, "errors": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Bot creation failed")
            return Response({"success": False, "message": "Unable to create bot."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request):
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.get(bot_id=bot_id, seller_user_id=request.user.id)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found."}, status=status.HTTP_404_NOT_FOUND)

        # Allow partial updates
        serializer = BotSerializer(bot, data=request.data, partial=True)
        try:
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response({"success": True, "message": "Bot updated successfully.", "data": serializer.data}, status=status.HTTP_200_OK)
        except serializers.ValidationError as exc:
            return Response({"success": False, "errors": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Bot update failed")
            return Response({"success": False, "message": "Unable to update bot."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BotPublishAPIView(APIView):
    """
    POST: publish a bot (seller) ?bot_id=
    """

    permission_classes = [IsAuthenticated, IsSeller]

    def post(self, request):
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.get(bot_id=bot_id, seller_user_id=request.user.id)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found."}, status=status.HTTP_404_NOT_FOUND)

        bot.is_published = True
        bot.save(update_fields=["is_published", "updated_at"])
        return Response({"success": True, "message": "Bot published successfully.", "data": BotSerializer(bot).data}, status=status.HTTP_200_OK)


class BotUnpublishAPIView(APIView):
    """
    POST: unpublish a bot (seller) ?bot_id=
    """

    permission_classes = [IsAuthenticated, IsSeller]

    def post(self, request):
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.get(bot_id=bot_id, seller_user_id=request.user.id)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found."}, status=status.HTTP_404_NOT_FOUND)

        bot.is_published = False
        bot.save(update_fields=["is_published", "updated_at"])
        return Response({"success": True, "message": "Bot unpublished successfully.", "data": BotSerializer(bot).data}, status=status.HTTP_200_OK)


class BotPublicAPIView(APIView):
    """
    GET: public bot fetch (no auth) ?bot_id=
    Only returns published bots.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            bot = Bot.objects.select_related("seller_user").get(bot_id=bot_id, is_published=True)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found or not published."}, status=status.HTTP_404_NOT_FOUND)

        seller = bot.seller_user
        biz = (getattr(seller, "company_name", None) or "").strip() or (seller.full_name or "").strip()

        # Only expose necessary fields (+ seller contact for public invoice header)
        return Response(
            {
                "success": True,
                "data": {
                    "bot_id": bot.bot_id,
                    "name": bot.name,
                    "flow": bot.flow,
                    "design": bot.design,
                    "seller": {
                        "business_name": biz,
                        "phone": (seller.phone or "").strip(),
                        "email": (seller.email or "").strip(),
                    },
                },
            },
            status=status.HTTP_200_OK,
        )


class BotWidgetJSAPIView(APIView):
    """
    GET: returns a JS snippet that embeds the bot widget via an iframe.
    This enables the generated script to be embedded on any HTML page.

    /api/bots/widget.js?bot_id=123
    """

    permission_classes = [AllowAny]

    def get(self, request):
        bot_id = request.query_params.get("bot_id", "")
        # IMPORTANT:
        # Do NOT hardcode 127.0.0.1 / backend host here, because the script may be served via a proxy
        # (e.g. React dev server at localhost:3000). If we generate an iframe URL pointing to 127.0.0.1,
        # browsers will show "127.0.0.1 refused to connect" when that host isn't reachable from the page.
        # Instead, compute widget origin at runtime from the script URL (document.currentScript.src).
        js = """
(function (w, d) {
  try {
    var existingBtn = d.getElementById('agrobridge-bot-button');
    var existingIframe = d.getElementById('agrobridge-bot-iframe');
    if (existingBtn || existingIframe) return;

    // Resolve origin from the currently executing script tag.
    // This keeps widget + script on the same origin (works with proxies and production domains).
    var current = d.currentScript;
    var scriptSrc = current && current.src ? current.src : '';
    var origin = '';
    try {
      origin = scriptSrc ? new URL(scriptSrc).origin : '';
    } catch (e) {
      origin = '';
    }
    if (!origin) {
      // Fallback: try window.location origin
      origin = w.location && w.location.origin ? w.location.origin : '';
    }

    // Auto-detect frontend URL from backend URL
    var frontendUrl = origin;
    if (origin.indexOf('api.qa.syntrrix.com') !== -1) {
      frontendUrl = origin.replace('api.qa.syntrrix.com', 'qa.syntrrix.com');
    } else if (origin.indexOf('localhost:8000') !== -1) {
      frontendUrl = origin.replace('localhost:8000', 'localhost:3000');
    } else if (origin.indexOf('127.0.0.1:8000') !== -1) {
      frontendUrl = origin.replace('127.0.0.1:8000', '127.0.0.1:3000');
    } else if (origin.indexOf('://api.') !== -1) {
      frontendUrl = origin.replace('://api.', '://');
    }

    var widgetUrl = frontendUrl + '/widget/bot?bot_id=""" + str(bot_id) + """';

    // Floating button
    var btn = d.createElement('button');
    btn.id = 'agrobridge-bot-button';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Open chatbot');
    btn.style.position = 'fixed';
    btn.style.bottom = '16px';
    btn.style.right = '16px';
    btn.style.width = '64px';
    btn.style.height = '64px';
    btn.style.borderRadius = '999px';
    btn.style.border = '0';
    btn.style.cursor = 'pointer';
    btn.style.zIndex = '2147483647';
    btn.style.background = '#0ea5e9';
    btn.style.boxShadow = '0 12px 30px rgba(14,165,233,0.35)';
    btn.style.display = 'flex';
    btn.style.alignItems = 'center';
    btn.style.justifyContent = 'center';
    btn.style.padding = '0';

    // Simple chat icon (SVG)
    btn.innerHTML = '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 4H20V16H7L4 19V4Z" stroke="white" stroke-width="2" stroke-linejoin="round"/><path d="M7 8H17" stroke="white" stroke-width="2" stroke-linecap="round"/><path d="M7 12H13" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>';

    // Widget iframe (hidden until opened)
    var iframe = d.createElement('iframe');
    iframe.id = 'agrobridge-bot-iframe';
    iframe.src = widgetUrl;
    iframe.style.position = 'fixed';
    iframe.style.bottom = '96px';
    iframe.style.right = '16px';
    iframe.style.width = '420px';
    iframe.style.height = '640px';
    iframe.style.border = '0';
    iframe.style.zIndex = '2147483647';
    iframe.style.borderRadius = '16px';
    iframe.style.boxShadow = '0 12px 35px rgba(0,0,0,0.25)';
    iframe.style.overflow = 'hidden';
    iframe.style.display = 'none';
    iframe.setAttribute('allow', 'clipboard-read; clipboard-write');

    function applyResponsiveSize() {
      try {
        var pad = 32; // left+right safe padding
        var bottomPad = 120; // space for button + breathing room
        var maxW = Math.max(280, (w.innerWidth || 420) - pad);
        var maxH = Math.max(360, (w.innerHeight || 640) - bottomPad);
        var targetW = Math.min(420, maxW);
        var targetH = Math.min(640, maxH);
        iframe.style.width = targetW + 'px';
        iframe.style.height = targetH + 'px';
      } catch (e) {}
    }

    function toggle() {
      var open = iframe.style.display !== 'none';
      if (open) {
        iframe.style.display = 'none';
      } else {
        applyResponsiveSize();
        iframe.style.display = 'block';
      }
    }

    btn.addEventListener('click', toggle);
    w.addEventListener('resize', function () {
      if (iframe.style.display !== 'none') applyResponsiveSize();
    });

    // Close on ESC
    d.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        iframe.style.display = 'none';
      }
    });

    d.body.appendChild(btn);
    d.body.appendChild(iframe);
  } catch (e) {
    console && console.error && console.error(e);
  }
})(window, document);
"""
        return HttpResponse(js, content_type="application/javascript")


class BotEntryAPIView(APIView):
    """
    Public endpoint: create a Customer entry from a published bot widget run.

    POST /api/bots/entry/?bot_id=123
    body:
      {
        "full_name": "...",
        "mobile_number": "...",
        "email": "...",
        "extra": { ...any extra fields... }
      }
    """

    permission_classes = [AllowAny]

    def post(self, request):
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.select_related("seller_user").get(bot_id=bot_id, is_published=True)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found or not published."}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data or {}
        full_name = (payload.get("full_name") or "").strip()
        mobile_number = (payload.get("mobile_number") or "").strip()
        email = (payload.get("email") or "").strip()
        flow_messages = payload.get("flow_messages")
        if not isinstance(flow_messages, list):
            flow_messages = []
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}

        if not full_name or not mobile_number or not email:
            return Response(
                {"success": False, "message": "full_name, mobile_number and email are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            node_id = payload.get("last_agent_assign_node_id") or extra.get("last_agent_assign_node_id")
            assigned_team_manager, assigned_agent = _resolve_agent_assignment(bot, node_id=node_id)
            customer = Customer.objects.create(
                seller_user=bot.seller_user,
                full_name=full_name,
                mobile_number=mobile_number,
                email=email,
                created_via_bot=True,
                source_bot=bot,
                bot_payload=extra,
            )
            # Create (or reuse) a live chat session for this bot-captured customer.
            session, _created = LiveChatSession.objects.get_or_create(
                customer=customer,
                defaults={
                    "seller_user": bot.seller_user,
                    "source_bot": bot,
                    "assigned_team_manager": assigned_team_manager,
                    "assigned_agent": assigned_agent,
                },
            )
            # If an older session row is reused, keep assignment in sync with current bot routing.
            assignment_updates = []
            if session.assigned_team_manager_id != (assigned_team_manager.id if assigned_team_manager else None):
                session.assigned_team_manager = assigned_team_manager
                assignment_updates.append("assigned_team_manager")
            if session.assigned_agent_id != (assigned_agent.id if assigned_agent else None):
                session.assigned_agent = assigned_agent
                assignment_updates.append("assigned_agent")
            if assignment_updates:
                assignment_updates.append("updated_at")
                session.save(update_fields=assignment_updates)

            if _created or assignment_updates:
                _sync_session_assignment_to_data_hub(session)

            # Persist the bot widget flow transcript dynamically (no DB inserts into LiveChatMessage).
            # This lets Live Chat Support show the "pure" conversation first.
            if flow_messages:
                existing = _get_persisted_live_chat_messages(int(session.session_id))
                if not existing or (isinstance(existing, list) and len(existing) == 0):
                    base_time = timezone.now()
                    chat_rows = []
                    for idx, m in enumerate(flow_messages):
                        row = _flow_message_to_chat_row(m, idx, base_time)
                        if row:
                            chat_rows.append(row)
                    if chat_rows:
                        _set_persisted_live_chat_messages(int(session.session_id), chat_rows)
            return Response(
                {
                    "success": True,
                    "message": "Entry saved.",
                    "data": CustomerSerializer(customer).data,
                    "live_chat_session": LiveChatSessionPublicSerializer(session).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except Exception as exc:
            logger.error(f"Error saving bot entry: {exc}")
            return Response(
                {"success": False, "message": "Failed to save entry."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class LiveChatSessionAPIView(APIView):
    """
    Seller endpoint:
    GET /api/live-chat/sessions/?customer_id=<id>

    Returns the session for a bot-captured customer owned by the authenticated seller.
    Creates it on-demand if missing (for older bot entries).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response(
                {"success": False, "message": "Only sellers can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        customer_id = request.query_params.get("customer_id")
        if not customer_id:
            return Response(
                {"success": False, "message": "customer_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            customer = Customer.objects.select_related("source_bot").get(
                customer_id=customer_id,
                seller_user=request.user,
            )
        except Customer.DoesNotExist:
            return Response(
                {"success": False, "message": "Customer not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        session, _created = LiveChatSession.objects.get_or_create(
            customer=customer,
            defaults={
                "seller_user": request.user,
                "source_bot": customer.source_bot,
            },
        )
        if customer.source_bot:
            node_id = None
            if isinstance(customer.bot_payload, dict):
                node_id = customer.bot_payload.get("last_agent_assign_node_id")
            assigned_team_manager, assigned_agent = _resolve_agent_assignment(customer.source_bot, node_id=node_id)
            assignment_updates = []
            if session.assigned_team_manager_id != (assigned_team_manager.id if assigned_team_manager else None):
                session.assigned_team_manager = assigned_team_manager
                assignment_updates.append("assigned_team_manager")
            if session.assigned_agent_id != (assigned_agent.id if assigned_agent else None):
                session.assigned_agent = assigned_agent
                assignment_updates.append("assigned_agent")
            if assignment_updates:
                assignment_updates.append("updated_at")
                session.save(update_fields=assignment_updates)

        if _created or (customer.source_bot and assignment_updates):
            _sync_session_assignment_to_data_hub(session)

        return Response(
            {"success": True, "data": LiveChatSessionSerializer(session).data},
            status=status.HTTP_200_OK,
        )


class LiveChatMessagesAPIView(APIView):
    """
    Seller endpoints:
    GET  /api/live-chat/messages/?session_id=<id>&limit=50
    POST /api/live-chat/messages/?session_id=<id>
      body: { "message": "..." }
    """

    permission_classes = [IsAuthenticated]

    def _get_session_for_user(self, request, session_id: str):
        return _resolve_support_live_chat_session(request.user, session_id)

    def get(self, request):
        if request.user.role.role_id not in [UserRole.ROLE_SELLER, UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            return Response(
                {"success": False, "message": "Only seller/team users can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        session_id = request.query_params.get("session_id")
        limit_raw = request.query_params.get("limit", "50")
        try:
            limit = int(limit_raw)
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 200))

        if not session_id:
            return Response(
                {"success": False, "message": "session_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            session = self._get_session_for_user(request, session_id)
            persisted = _get_persisted_live_chat_messages(int(session_id)) or []

            from django.utils import timezone
            now_iso = timezone.now().isoformat()
            last_msg_id = str(persisted[-1].get("message_id") or "") if persisted else ""

            # Find all sessions belonging to the same customer under this seller to clear unread counts consistently
            c_phone = session.customer.mobile_number if session.customer else session.visitor_mobile_number
            c_email = session.customer.email if session.customer else session.visitor_email
            phone_clean = "".join(c for c in str(c_phone or "") if c.isdigit())
            email_clean = str(c_email or "").strip().lower()

            related = LiveChatSession.objects.filter(seller_user=session.seller_user)
            if session.customer_id:
                related = related.filter(Q(customer_id=session.customer_id) | Q(session_id=session.session_id))
            elif phone_clean and len(phone_clean) >= 10:
                related = related.filter(Q(visitor_mobile_number__contains=phone_clean[-10:]) | Q(session_id=session.session_id))
            elif email_clean:
                related = related.filter(Q(visitor_email__iexact=email_clean) | Q(session_id=session.session_id))
            else:
                related = related.filter(session_id=session.session_id)

            for rs in related:
                vp = dict(rs.visitor_payload or {})
                vp["seller_last_read_at"] = now_iso
                vp["is_read"] = True
                if last_msg_id:
                    vp["last_read_message_id"] = last_msg_id
                rs.visitor_payload = vp
                rs.save(update_fields=["visitor_payload", "updated_at"])

            return Response(
                {"success": True, "data": persisted[-limit:]},
                status=status.HTTP_200_OK,
            )
        except LiveChatSession.DoesNotExist:
            return Response(
                {"success": False, "message": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

    def post(self, request):
        if request.user.role.role_id not in [UserRole.ROLE_SELLER, UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            return Response(
                {"success": False, "message": "Only seller/team users can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        session_id = request.query_params.get("session_id") or request.data.get("session_id")
        if not session_id:
            return Response(
                {"success": False, "message": "session_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message_text = (request.data.get("message") or "").strip()
        uploaded_file = request.FILES.get("file") or request.FILES.get("media") or request.FILES.get("attachment")

        if not message_text and not uploaded_file:
            return Response(
                {"success": False, "message": "message or file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        media_payload = None
        if uploaded_file:
            import os, uuid
            from django.core.files.storage import default_storage
            from django.core.files.base import ContentFile

            original_name = getattr(uploaded_file, "name", "file") or "file"
            mime_type = (getattr(uploaded_file, "content_type", "") or "").lower()
            ext = os.path.splitext(original_name)[1].lower()
            safe_name = f"{uuid.uuid4().hex[:10]}_{original_name.replace(' ', '_')}"

            rel_dir = f"live_chat_uploads/{session_id}"
            saved_path = default_storage.save(f"{rel_dir}/{safe_name}", ContentFile(uploaded_file.read()))
            media_url = f"/media/{saved_path}"

            media_payload = {
                "file_url": media_url,
                "file_name": original_name,
                "mime_type": mime_type or ("image/jpeg" if ext in ['.jpg', '.jpeg', '.png', '.webp'] else "application/pdf"),
            }

        try:
            session = self._get_session_for_user(request, session_id)
            new_msg = _save_live_chat_message(session, LiveChatMessage.SENDER_SELLER, message_text, media_payload=media_payload)

            from django.utils import timezone
            now_iso = timezone.now().isoformat()
            new_msg_id = str(new_msg.get("message_id")) if new_msg and isinstance(new_msg, dict) and new_msg.get("message_id") else ""

            c_phone = session.customer.mobile_number if session.customer else session.visitor_mobile_number
            c_email = session.customer.email if session.customer else session.visitor_email
            phone_clean = "".join(c for c in str(c_phone or "") if c.isdigit())
            email_clean = str(c_email or "").strip().lower()

            related = LiveChatSession.objects.filter(seller_user=session.seller_user)
            if session.customer_id:
                related = related.filter(Q(customer_id=session.customer_id) | Q(session_id=session.session_id))
            elif phone_clean and len(phone_clean) >= 10:
                related = related.filter(Q(visitor_mobile_number__contains=phone_clean[-10:]) | Q(session_id=session.session_id))
            elif email_clean:
                related = related.filter(Q(visitor_email__iexact=email_clean) | Q(session_id=session.session_id))
            else:
                related = related.filter(session_id=session.session_id)

            for rs in related:
                vp = dict(rs.visitor_payload or {})
                vp["seller_last_read_at"] = now_iso
                vp["is_read"] = True
                if new_msg_id:
                    vp["last_read_message_id"] = new_msg_id
                rs.visitor_payload = vp
                rs.save(update_fields=["visitor_payload", "updated_at"])

            # If the session is linked to a WhatsApp customer, send the agent reply/media back to customer WhatsApp handset!
            try:
                from agro_app.models import WhatsAppChatSession, WhatsAppConfig
                wa_session = WhatsAppChatSession.objects.filter(live_chat_session=session).order_by("-updated_at").first()
                customer_phone = None
                if wa_session and wa_session.customer_phone:
                    customer_phone = wa_session.customer_phone
                elif session.customer and session.customer.whatsapp_number:
                    customer_phone = session.customer.whatsapp_number
                elif session.customer and session.customer.mobile_number:
                    customer_phone = session.customer.mobile_number

                if customer_phone:
                    config = WhatsAppConfig.objects.filter(seller_user=session.seller_user, is_connected=True).order_by("-updated_at").first()
                    if config and config.is_connected:
                        domain = request.META.get("HTTP_HOST") or "api.qa.syntrrix.com"
                        scheme = "https" if (request.is_secure() or "api.qa.syntrrix.com" in domain) else "http"

                        if media_payload and media_payload.get("file_url"):
                            raw_link = media_payload["file_url"]
                            full_link = f"{scheme}://{domain}{raw_link}" if not raw_link.startswith("http") else raw_link
                            sanitized_link = _sanitize_meta_media_url(full_link, domain)

                            mime = (media_payload.get("mime_type") or "").lower()
                            fname = media_payload.get("file_name") or "file"
                            ext = os.path.splitext(fname)[1].lower()

                            if mime.startswith("image/") or ext in [".jpg", ".jpeg", ".png", ".webp"]:
                                payload = {
                                    "type": "image",
                                    "image": {
                                        "link": sanitized_link
                                    }
                                }
                                if message_text:
                                    payload["image"]["caption"] = message_text
                            elif mime.startswith("video/") or ext in [".mp4", ".mov", ".avi", ".webm", ".3gp"]:
                                payload = {
                                    "type": "video",
                                    "video": {
                                        "link": sanitized_link
                                    }
                                }
                                if message_text:
                                    payload["video"]["caption"] = message_text
                            else:
                                # PDF / Document
                                payload = {
                                    "type": "document",
                                    "document": {
                                        "link": sanitized_link,
                                        "filename": fname
                                    }
                                }
                                if message_text:
                                    payload["document"]["caption"] = message_text
                        else:
                            payload = {
                                "type": "text",
                                "text": {"preview_url": False, "body": message_text}
                            }

                        ok, err_msg = _send_whatsapp_message_raw(config, customer_phone, payload)
                        if not ok:
                            logger.error(f"Failed to send LiveChat reply/media to WhatsApp {customer_phone}: {err_msg}")
                        else:
                            logger.info(f"LiveChat reply/media sent to WhatsApp {customer_phone} successfully via channel '{config.channel_name}'")
                    else:
                        logger.warning(f"LiveChat reply to WhatsApp skipped: No active connected WhatsAppConfig for seller '{session.seller_user.email}'")
            except Exception as wa_ex:
                logger.error(f"Failed to forward LiveChat message to WhatsApp: {wa_ex}")

            return Response(
                {"success": True, "data": new_msg},
                status=status.HTTP_200_OK,
            )

        except LiveChatSession.DoesNotExist:
            return Response(
                {"success": False, "message": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "success": True,
                "message": "Message sent.",
                "data": new_msg,
            },
            status=status.HTTP_201_CREATED,
        )


def _row_contains_duplicate_phone_or_email(rows, incoming_row, columns):
    """
    Checks if `incoming_row` has a duplicate phone number or email compared to existing `rows`.
    Matching rules:
    - Phone number (clean digit-only string compared using last 10 digits if length >= 10, or exact digits if shorter).
    - Email (case-insensitive comparison).
    - Allow duplicate names.
    """
    # 1. Identify phone and email column keys from schema
    phone_keys = []
    email_keys = []
    for col in columns:
        if not isinstance(col, dict):
            continue
        col_name = col.get("name")
        if not col_name:
            continue
        col_name_lower = str(col_name).strip().lower()
        field_type = str(col.get("field_type") or "").strip().upper()
        if field_type == "PHONE" or "phone" in col_name_lower or "mobile" in col_name_lower or "contact" in col_name_lower:
            phone_keys.append(col_name)
        elif field_type == "EMAIL" or "email" in col_name_lower or "mail" in col_name_lower:
            email_keys.append(col_name)

    # Fallback to inspecting keys in incoming_row if schema didn't yield columns
    if not phone_keys:
        for k in incoming_row.keys():
            k_lower = k.lower()
            if "phone" in k_lower or "mobile" in k_lower or "contact" in k_lower:
                phone_keys.append(k)
    if not email_keys:
        for k in incoming_row.keys():
            k_lower = k.lower()
            if "email" in k_lower or "mail" in k_lower:
                email_keys.append(k)

    # 2. Extract phone values and email values from incoming_row (Only from designated keys)
    incoming_phones = []
    incoming_emails = []
    
    for pk in phone_keys:
        v = incoming_row.get(pk)
        if v is not None:
            v_str = str(v).strip()
            if v_str:
                digits = "".join(c for c in v_str if c.isdigit())
                if len(digits) >= 10:
                    incoming_phones.append(digits[-10:])
                elif digits:
                    incoming_phones.append(digits)

    for ek in email_keys:
        v = incoming_row.get(ek)
        if v is not None:
            v_str = str(v).strip().lower()
            if v_str and "@" in v_str:
                incoming_emails.append(v_str)

    if not incoming_phones and not incoming_emails:
        return False

    # 3. Compare with existing rows (Only check matching keys)
    for r in rows:
        if not isinstance(r, dict):
            continue
        
        # Check phone fields
        for pk in phone_keys:
            v = r.get(pk)
            if v is not None:
                v_str = str(v).strip()
                if v_str:
                    v_digits = "".join(c for c in v_str if c.isdigit())
                    if v_digits:
                        v_phone_comp = v_digits[-10:] if len(v_digits) >= 10 else v_digits
                        if v_phone_comp in incoming_phones:
                            return True
                            
        # Check email fields
        for ek in email_keys:
            v = r.get(ek)
            if v is not None:
                v_str = str(v).strip().lower()
                if v_str and v_str in incoming_emails:
                    return True

    return False



def _find_matching_live_chat_session(seller_id, row):
    """
    Finds a matching LiveChatSession for a Data Hub row based on phone, email, or name.
    """
    if not seller_id or not isinstance(row, dict):
        return None

    # Get search fields from the row
    phone_vals = []
    email_vals = []
    name_vals = []

    for k, v in row.items():
        if k in ("row_id", "inserted_on", "updated_on", "assigned_team_manager_id", "assigned_agent_id", "assigned_team_manager_name", "assigned_agent_name"):
            continue
        v_str = str(v).strip()
        if not v_str:
            continue
        
        # Check if it looks like email
        if "@" in v_str:
            email_vals.append(v_str.lower())
        # Check if it looks like phone
        digits = "".join(c for c in v_str if c.isdigit())
        if digits and len(digits) >= 10:
            phone_vals.append(digits)
        # Check if it's name or other text (longer than 2 chars)
        if len(v_str) > 2 and "@" not in v_str and not any(c.isdigit() for c in v_str):
            name_vals.append(v_str.lower())

    from django.db.models import Q
    from .models import LiveChatSession

    # Query sessions for this seller
    sessions = LiveChatSession.objects.filter(seller_user_id=seller_id).select_related("customer", "assigned_team_manager", "assigned_agent")

    # Match by email first
    for email in email_vals:
        session = sessions.filter(Q(customer__email__iexact=email) | Q(visitor_email__iexact=email)).first()
        if session:
            return session

    # Match by phone
    for phone in phone_vals:
        for s in sessions:
            s_phone = s.customer.mobile_number if s.customer else s.visitor_mobile_number
            s_phone_clean = "".join(c for c in str(s_phone) if c.isdigit())
            if s_phone_clean and (phone in s_phone_clean or s_phone_clean in phone) and len(s_phone_clean) >= 10:
                return s

    # Match by name
    for name in name_vals:
        session = sessions.filter(Q(customer__full_name__iexact=name) | Q(visitor_name__iexact=name)).first()
        if session:
            return session

    return None


def _sync_data_hub_assignment_to_session(seller_id, row):
    """
    Syncs the assigned agent and team manager of a Data Hub row back to its matching
    LiveChatSession in the database.
    """
    if not seller_id or not isinstance(row, dict):
        return

    # Find the matching session
    session = _find_matching_live_chat_session(seller_id, row)
    if not session:
        return

    # Extract assignments from the row
    mgr_id = row.get("assigned_team_manager_id")
    agent_id = row.get("assigned_agent_id")
    mgr_name = str(row.get("assigned_team_manager_name") or "").strip()
    agent_name = str(row.get("assigned_agent_name") or "").strip()

    update_fields = []

    # Update assigned team manager
    if mgr_id:
        try:
            tm_user = User.objects.get(id=int(mgr_id), role_id=UserRole.ROLE_TEAM_MANAGER)
            if session.assigned_team_manager != tm_user:
                session.assigned_team_manager = tm_user
                update_fields.append("assigned_team_manager")
        except Exception:
            pass
    else:
        if session.assigned_team_manager is not None:
            session.assigned_team_manager = None
            update_fields.append("assigned_team_manager")

    # Update assigned agent
    if agent_id:
        try:
            agent_user = User.objects.get(id=int(agent_id), role_id=UserRole.ROLE_AGENT)
            if session.assigned_agent != agent_user:
                session.assigned_agent = agent_user
                update_fields.append("assigned_agent")
        except Exception:
            pass
    else:
        if session.assigned_agent is not None:
            session.assigned_agent = None
            update_fields.append("assigned_agent")

    # Also update visitor payload fallback fields
    vp = dict(session.visitor_payload or {})
    if mgr_name:
        vp["manual_team_name"] = mgr_name
    else:
        vp.pop("manual_team_name", None)

    if agent_name:
        vp["manual_agent_name"] = agent_name
    else:
        vp.pop("manual_agent_name", None)

    if session.visitor_payload != vp:
        session.visitor_payload = vp
        update_fields.append("visitor_payload")

    if update_fields:
        update_fields.append("updated_at")
        session.save(update_fields=update_fields)


def _sync_session_assignment_to_data_hub(session):
    """
    Syncs the assigned agent and team manager of a LiveChatSession to matching rows in
    the seller's Data Hub collections.
    """
    seller_id = int(session.seller_user_id) if session.seller_user_id else 0
    if not seller_id:
        return

    # 1. Resolve customer details
    phone = ""
    email = ""
    name = ""
    if session.customer:
        phone = session.customer.mobile_number
        email = session.customer.email
        name = session.customer.full_name
    else:
        phone = session.visitor_mobile_number
        email = session.visitor_email
        name = session.visitor_name

    phone_clean = "".join(c for c in str(phone) if c.isdigit()) if phone else ""
    email_clean = str(email).strip().lower() if email else ""
    name_clean = str(name).strip().lower() if name else ""

    if not phone_clean and not email_clean and not name_clean:
        return

    # 2. Get assignment values to sync
    mgr_id = int(session.assigned_team_manager.id) if session.assigned_team_manager else None
    mgr_name = str(session.assigned_team_manager.full_name or "").strip() if session.assigned_team_manager else ""
    agent_id = int(session.assigned_agent.id) if session.assigned_agent else None
    agent_name = str(session.assigned_agent.full_name or "").strip() if session.assigned_agent else ""

    # If no team manager is assigned in DB, check if they exist in visitor payload
    if not mgr_name and session.visitor_payload:
        mgr_name = str(session.visitor_payload.get("manual_team_name") or "").strip()
    if not agent_name and session.visitor_payload:
        agent_name = str(session.visitor_payload.get("manual_agent_name") or "").strip()

    # 3. Read tables
    tables_path = _data_hub_tables_file(seller_id)
    tables = _read_json_file(tables_path, default=[])
    if not isinstance(tables, list):
        return

    for t in tables:
        if not isinstance(t, dict):
            continue
        table_id = t.get("table_id")
        if not table_id:
            continue

        rows_path = _data_hub_table_rows_file(seller_id, table_id)
        payload = _read_json_file(rows_path, default={"rows": []})
        if not isinstance(payload, dict):
            continue
        rows = payload.get("rows")
        if not isinstance(rows, list):
            continue

        updated_any = False
        for idx, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            
            # Check if this row matches the customer
            matched = False
            for k, v in r.items():
                if k in ("row_id", "inserted_on", "updated_on", "assigned_team_manager_id", "assigned_agent_id", "assigned_team_manager_name", "assigned_agent_name"):
                    continue
                v_str = str(v).strip()
                if not v_str:
                    continue
                
                # Compare email
                if email_clean and "@" in v_str and v_str.lower() == email_clean:
                    matched = True
                    break
                
                # Compare phone
                v_digits = "".join(c for c in v_str if c.isdigit())
                if phone_clean and v_digits and (phone_clean in v_digits or v_digits in phone_clean) and len(v_digits) >= 10:
                    matched = True
                    break
                
                # Compare name
                if name_clean and v_str.lower() == name_clean:
                    matched = True
                    break
            
            if matched:
                r["assigned_team_manager_id"] = mgr_id
                r["assigned_team_manager_name"] = mgr_name
                r["assigned_agent_id"] = agent_id
                r["assigned_agent_name"] = agent_name
                r["updated_on"] = timezone.now().isoformat()
                rows[idx] = r
                updated_any = True

        if updated_any:
            payload["rows"] = rows
            _write_json_file_atomic(rows_path, payload)


def _live_chat_support_session_row(s):
    """Serialize one LiveChatSession for support inbox / lead pipeline lists."""
    persisted = _get_persisted_live_chat_messages(int(s.session_id)) or []
    last = persisted[-1] if persisted else None
    if not last:
        last_msg = s.messages.order_by("-created_at").first()
        if last_msg:
            last = {
                "message_id": last_msg.message_id,
                "sender_type": last_msg.sender_type,
                "message": last_msg.message,
                "created_at": last_msg.created_at.isoformat() if last_msg.created_at else None,
            }

    vp = s.visitor_payload or {}
    is_read_flag = bool(vp.get("is_read", False))
    last_read_msg_id = str(vp.get("last_read_message_id") or "")
    seller_last_read_at_str = vp.get("seller_last_read_at")
    seller_last_read_dt = None
    if seller_last_read_at_str:
        try:
            from django.utils.dateparse import parse_datetime
            seller_last_read_dt = parse_datetime(str(seller_last_read_at_str))
        except Exception:
            seller_last_read_dt = None

    unread_count = 0
    if persisted:
        last_sender = str(persisted[-1].get("sender_type") or "").strip().lower()
        if last_sender == "seller":
            unread_count = 0
        elif is_read_flag:
            if last_read_msg_id:
                new_count = 0
                for m in reversed(persisted):
                    m_id_str = str(m.get("message_id") or "")
                    if m_id_str == last_read_msg_id:
                        break
                    st = str(m.get("sender_type") or "").strip().lower()
                    if st in ("customer", "user", "visitor"):
                        new_count += 1
                unread_count = new_count
            else:
                unread_count = 0
        else:
            for m in reversed(persisted):
                st = str(m.get("sender_type") or "").strip().lower()
                if st == "seller":
                    break
                if st in ("customer", "user", "visitor"):
                    m_dt_str = m.get("created_at")
                    if seller_last_read_dt and m_dt_str:
                        try:
                            from django.utils.dateparse import parse_datetime
                            m_dt = parse_datetime(str(m_dt_str))
                            if m_dt and m_dt <= seller_last_read_dt:
                                break
                        except Exception:
                            pass
                    unread_count += 1
    else:
        last_sender = str(last.get("sender_type") if last else "").strip().lower()
        if last_sender == "seller" or is_read_flag:
            unread_count = 0
        else:
            unread_count = 1 if last else 0

    mgr_name = (s.assigned_team_manager.full_name if s.assigned_team_manager else str(vp.get("manual_team_name") or "").strip())
    agent_name = (s.assigned_agent.full_name if s.assigned_agent else str(vp.get("manual_agent_name") or "").strip())
    return {
        "session_id": s.session_id,
        "customer_id": s.customer.customer_id if s.customer_id else None,
        "customer_name": (s.customer.full_name if s.customer_id else s.visitor_name) or "Customer",
        "mobile_number": (s.customer.mobile_number if s.customer_id else s.visitor_mobile_number) or "",
        "whatsapp_number": (s.customer.whatsapp_number if s.customer_id else "") or "",
        "email": (s.customer.email if s.customer_id else s.visitor_email) or "",
        "address": (s.customer.address if s.customer_id else ((s.visitor_payload or {}).get("address") or "")) or "",
        "bot_payload": s.customer.bot_payload if s.customer_id else (s.visitor_payload or {}),
        "last_message_text": last.get("message") if last else "",
        "last_message_created_at": last.get("created_at") if last else None,
        "last_message_sender_type": last.get("sender_type") if last else "customer",
        "unread_count": unread_count,
        "is_read": is_read_flag if unread_count == 0 else False,
        "assigned_team_manager_id": s.assigned_team_manager_id,
        "assigned_team_manager_name": mgr_name or "",
        "assigned_agent_id": s.assigned_agent_id,
        "assigned_agent_name": agent_name or "",
        "lead_pipeline_status": (s.lead_pipeline_status or "").strip() or None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "is_manual_pipeline_lead": bool(getattr(s, "is_manual_pipeline_lead", False)),
    }



def _apply_session_assignment_from_names(session, team_manager_name: str, agent_name: str):
    """Resolve agent + team manager by display names under session.seller_user. Clears FKs if agent_name empty."""
    seller = session.seller_user
    ag = (agent_name or "").strip()
    tm_label = (team_manager_name or "").strip()
    if not ag:
        session.assigned_agent = None
        session.assigned_team_manager = None
        vp = dict(session.visitor_payload or {})
        vp.pop("manual_team_name", None)
        vp.pop("manual_agent_name", None)
        session.visitor_payload = vp
        return
    qs = TeamMember.objects.filter(
        seller_user=seller,
        member_type=TeamMember.MEMBER_TYPE_AGENT,
        user__status=User.STATUS_ACTIVE,
    ).select_related("user", "parent_team_manager")
    matches = []
    for row in qs:
        if (row.user.full_name or "").strip() != ag:
            continue
        if tm_label:
            pt = row.parent_team_manager
            if not pt or (pt.full_name or "").strip() != tm_label:
                continue
        matches.append(row)
    if len(matches) != 1:
        raise ValueError("Agent and team combination could not be resolved uniquely.")
    m = matches[0]
    session.assigned_agent = m.user
    session.assigned_team_manager = m.parent_team_manager
    vp = dict(session.visitor_payload or {})
    vp.pop("manual_team_name", None)
    vp.pop("manual_agent_name", None)
    session.visitor_payload = vp


class LiveChatSupportSessionsAPIView(APIView):
    """
    Seller-side inbox:
    GET /api/live-chat/support/sessions/

    Returns one row per LiveChatSession with last message preview.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        role_id = request.user.role.role_id
        if role_id not in [UserRole.ROLE_SELLER, UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            return Response(
                {"success": False, "message": "Only seller/team users can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        for_lead_pipeline = str(request.query_params.get("for_lead_pipeline") or "").lower() in ("1", "true", "yes")

        # Backfill old unassigned rows so team users can see existing chats too.
        if role_id == UserRole.ROLE_SELLER:
            _backfill_chat_assignments_for_seller(request.user)
        else:
            team_profile = TeamMember.objects.filter(user=request.user).select_related("seller_user").first()
            if team_profile:
                _backfill_chat_assignments_for_seller(team_profile.seller_user)

        sessions_qs = LiveChatSession.objects.select_related("customer", "source_bot", "assigned_team_manager", "assigned_agent")

        if for_lead_pipeline:
            # Lead Pipeline: all sessions for the org seller (includes manual-only leads).
            if role_id == UserRole.ROLE_SELLER:
                sessions_qs = sessions_qs.filter(seller_user=request.user)
            else:
                team_profile = TeamMember.objects.filter(user=request.user).select_related("seller_user").first()
                if not team_profile or not team_profile.seller_user_id:
                    return Response(
                        {"success": False, "message": "No seller linked to this account."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                sessions_qs = sessions_qs.filter(seller_user=team_profile.seller_user)
                if role_id == UserRole.ROLE_TEAM_MANAGER:
                    managed_agent_ids = _get_team_manager_agent_user_ids(request.user)
                    sessions_qs = sessions_qs.filter(
                        Q(assigned_team_manager=request.user)
                        | Q(assigned_agent_id__in=managed_agent_ids)
                    )
                elif role_id == UserRole.ROLE_AGENT:
                    sessions_qs = sessions_qs.filter(assigned_agent=request.user)
        elif role_id == UserRole.ROLE_SELLER:
            sessions_qs = sessions_qs.filter(seller_user=request.user, is_manual_pipeline_lead=False)
        elif role_id == UserRole.ROLE_TEAM_MANAGER:
            managed_agent_ids = _get_team_manager_agent_user_ids(request.user)
            sessions_qs = sessions_qs.filter(
                Q(assigned_team_manager=request.user)
                | Q(assigned_agent_id__in=managed_agent_ids),
                is_manual_pipeline_lead=False,
            )
        else:
            sessions_qs = sessions_qs.filter(assigned_agent=request.user, is_manual_pipeline_lead=False)

        sessions_qs = (
            sessions_qs.select_related("customer", "source_bot").order_by("-updated_at", "-created_at")
        )

        data = [_live_chat_support_session_row(s) for s in sessions_qs]

        return Response({"success": True, "count": len(data), "data": data}, status=status.HTTP_200_OK)

    def post(self, request):
        """Create a manual pipeline-only lead (visitor session; hidden from default inbox GET)."""
        role_id = request.user.role.role_id
        if role_id not in [UserRole.ROLE_SELLER, UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            return Response(
                {"success": False, "message": "Only seller/team users can create leads."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if role_id == UserRole.ROLE_SELLER:
            seller_user = request.user
        else:
            team_profile = TeamMember.objects.filter(user=request.user).select_related("seller_user").first()
            if not team_profile or not team_profile.seller_user_id:
                return Response(
                    {"success": False, "message": "No seller linked to this account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            seller_user = team_profile.seller_user

        name = str(request.data.get("name") or "").strip()
        phone = str(request.data.get("phone") or "").strip()
        if not name or not phone:
            return Response(
                {"success": False, "message": "name and phone are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = str(request.data.get("email") or "").strip()
        address = str(request.data.get("address") or "").strip()
        source = str(request.data.get("source") or "Manual lead").strip() or "Manual lead"
        team_label = str(request.data.get("team") or "").strip()
        agent_label = str(request.data.get("agent") or "").strip()
        status_raw = str(request.data.get("status") or "New Leads").strip() or "New Leads"
        max_len = LiveChatSession._meta.get_field("lead_pipeline_status").max_length
        status_val = status_raw[:max_len]

        assigned_agent = None
        assigned_team_manager = None

        if role_id == UserRole.ROLE_AGENT:
            assigned_agent = request.user
            agent_profile = TeamMember.objects.filter(user=request.user, member_type=TeamMember.MEMBER_TYPE_AGENT).select_related("parent_team_manager").first()
            if agent_profile:
                assigned_team_manager = agent_profile.parent_team_manager
        elif role_id == UserRole.ROLE_TEAM_MANAGER:
            assigned_team_manager = request.user
            if agent_label:
                managed_agent_ids = _get_team_manager_agent_user_ids(request.user)
                agent_member = TeamMember.objects.filter(
                    seller_user=seller_user,
                    member_type=TeamMember.MEMBER_TYPE_AGENT,
                    user_id__in=managed_agent_ids,
                    user__full_name=agent_label
                ).select_related("user").first()
                if agent_member:
                    assigned_agent = agent_member.user
        elif role_id == UserRole.ROLE_SELLER:
            if agent_label:
                qs = TeamMember.objects.filter(
                    seller_user=seller_user,
                    member_type=TeamMember.MEMBER_TYPE_AGENT,
                    user__status=User.STATUS_ACTIVE,
                ).select_related("user", "parent_team_manager")
                matches = []
                for row in qs:
                    if (row.user.full_name or "").strip() != agent_label:
                        continue
                    if team_label:
                        pt = row.parent_team_manager
                        if not pt or (pt.full_name or "").strip() != team_label:
                            continue
                    matches.append(row)
                if len(matches) == 1:
                    assigned_agent = matches[0].user
                    assigned_team_manager = matches[0].parent_team_manager
            elif team_label:
                tm_member = TeamMember.objects.filter(
                    seller_user=seller_user,
                    member_type=TeamMember.MEMBER_TYPE_TEAM_MANAGER,
                    user__status=User.STATUS_ACTIVE,
                    user__full_name=team_label
                ).select_related("user").first()
                if tm_member:
                    assigned_team_manager = tm_member.user

        vp = {
            "address": address,
            "lead_source": source,
            "manual_team_name": team_label,
            "manual_agent_name": agent_label,
        }

        session = LiveChatSession.objects.create(
            seller_user=seller_user,
            visitor_name=name,
            visitor_mobile_number=phone,
            visitor_email=email,
            visitor_payload=vp,
            lead_pipeline_status=status_val,
            is_manual_pipeline_lead=True,
            assigned_agent=assigned_agent,
            assigned_team_manager=assigned_team_manager,
        )

        _sync_session_assignment_to_data_hub(session)

        return Response(
            {"success": True, "message": "Lead created.", "data": _live_chat_support_session_row(session)},
            status=status.HTTP_201_CREATED,
        )

    def patch(self, request):
        """Update lead_pipeline_status and/or chat assignment (seller / team manager; not agents)."""
        role_id = request.user.role.role_id
        if role_id not in [UserRole.ROLE_SELLER, UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            return Response(
                {"success": False, "message": "Only seller/team users can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        session_id = request.query_params.get("session_id")
        if not session_id:
            return Response(
                {"success": False, "message": "session_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        has_status = "lead_pipeline_status" in request.data
        has_agent_uid = "assigned_agent_user_id" in request.data
        has_tm_uid = "assigned_team_manager_user_id" in request.data
        has_agent_name = bool(str(request.data.get("assigned_agent_name") or "").strip())
        has_tm_name = bool(str(request.data.get("assigned_team_manager_name") or "").strip())

        if not has_status and not has_agent_uid and not has_tm_uid and not has_agent_name:
            return Response(
                {"success": False, "message": "Provide lead_pipeline_status and/or assignment fields."},
                status=status.HTTP_400_BAD_REQUEST,
            )


        try:
            session = _resolve_support_live_chat_session(request.user, session_id)
        except LiveChatSession.DoesNotExist:
            return Response(
                {"success": False, "message": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        update_fields = []

        if has_status:
            raw_status = request.data.get("lead_pipeline_status")
            if raw_status is None:
                return Response(
                    {"success": False, "message": "lead_pipeline_status cannot be null (use empty string to clear)."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            normalized = str(raw_status).strip()
            max_status_len = LiveChatSession._meta.get_field("lead_pipeline_status").max_length
            if len(normalized) > max_status_len:
                return Response(
                    {
                        "success": False,
                        "message": f"lead_pipeline_status must be at most {max_status_len} characters.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            session.lead_pipeline_status = normalized
            update_fields.append("lead_pipeline_status")

        if has_agent_uid or has_tm_uid or has_agent_name:
            if has_agent_name and not has_agent_uid:
                try:
                    _apply_session_assignment_from_names(
                        session,
                        str(request.data.get("assigned_team_manager_name") or ""),
                        str(request.data.get("assigned_agent_name") or ""),
                    )
                except ValueError as exc:
                    return Response({"success": False, "message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
                if role_id == UserRole.ROLE_TEAM_MANAGER:
                    managed = set(_get_team_manager_agent_user_ids(request.user))
                    if session.assigned_agent_id and session.assigned_agent_id not in managed:
                        return Response(
                            {"success": False, "message": "You can only assign agents on your team."},
                            status=status.HTTP_403_FORBIDDEN,
                        )
                update_fields.extend(["assigned_agent", "assigned_team_manager", "visitor_payload"])
            else:
                aid_raw = request.data.get("assigned_agent_user_id")
                if aid_raw in (None, "", False):
                    session.assigned_agent = None
                    if has_tm_uid and request.data.get("assigned_team_manager_user_id") not in (None, "", False):
                        try:
                            tmid = int(request.data.get("assigned_team_manager_user_id"))
                        except (TypeError, ValueError):
                            return Response(
                                {"success": False, "message": "assigned_team_manager_user_id must be an integer."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        tm_user = User.objects.filter(id=tmid, role_id=UserRole.ROLE_TEAM_MANAGER).first()
                        if not tm_user:
                            return Response(
                                {"success": False, "message": "Invalid team manager user."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        session.assigned_team_manager = tm_user
                    else:
                        session.assigned_team_manager = None
                    vp = dict(session.visitor_payload or {})
                    vp.pop("manual_team_name", None)
                    vp.pop("manual_agent_name", None)
                    session.visitor_payload = vp
                    update_fields.extend(["assigned_agent", "assigned_team_manager", "visitor_payload"])
                else:
                    try:
                        aid = int(aid_raw)
                    except (TypeError, ValueError):
                        return Response(
                            {"success": False, "message": "assigned_agent_user_id must be an integer."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    tm_row = (
                        TeamMember.objects.filter(
                            seller_user=session.seller_user,
                            member_type=TeamMember.MEMBER_TYPE_AGENT,
                            user_id=aid,
                            user__status=User.STATUS_ACTIVE,
                        )
                        .select_related("user", "parent_team_manager")
                        .first()
                    )
                    if not tm_row:
                        return Response(
                            {"success": False, "message": "Invalid agent for this seller."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    if role_id == UserRole.ROLE_TEAM_MANAGER:
                        managed = set(_get_team_manager_agent_user_ids(request.user))
                        if aid not in managed:
                            return Response(
                                {"success": False, "message": "You can only assign agents on your team."},
                                status=status.HTTP_403_FORBIDDEN,
                            )
                    session.assigned_agent = tm_row.user
                    if has_tm_uid and request.data.get("assigned_team_manager_user_id") not in (None, "", False):
                        try:
                            tmid = int(request.data.get("assigned_team_manager_user_id"))
                        except (TypeError, ValueError):
                            return Response(
                                {"success": False, "message": "assigned_team_manager_user_id must be an integer."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        tm_user = User.objects.filter(id=tmid, role_id=UserRole.ROLE_TEAM_MANAGER).first()
                        if not tm_user:
                            return Response(
                                {"success": False, "message": "Invalid team manager user."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        session.assigned_team_manager = tm_user
                    else:
                        session.assigned_team_manager = tm_row.parent_team_manager
                    vp = dict(session.visitor_payload or {})
                    vp.pop("manual_team_name", None)
                    vp.pop("manual_agent_name", None)
                    session.visitor_payload = vp
                    update_fields.extend(["assigned_agent", "assigned_team_manager", "visitor_payload"])

        update_fields.append("updated_at")
        session.save(update_fields=list(dict.fromkeys(update_fields)))

        _sync_session_assignment_to_data_hub(session)

        return Response(
            {"success": True, "data": _live_chat_support_session_row(session)},
            status=status.HTTP_200_OK,
        )

    def delete(self, request):
        """Remove a live chat session from the seller pipeline (deletes session + messages; customer row kept)."""
        if request.user.role.role_id not in [UserRole.ROLE_SELLER, UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            return Response(
                {"success": False, "message": "Only seller/team users can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        session_id = request.query_params.get("session_id")
        if not session_id:
            return Response(
                {"success": False, "message": "session_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            session = _resolve_support_live_chat_session(request.user, session_id)
        except LiveChatSession.DoesNotExist:
            return Response(
                {"success": False, "message": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        sid = int(session.session_id)
        _clear_live_chat_session_storage(sid)
        session.delete()

        return Response(
            {"success": True, "message": "Lead removed from pipeline.", "data": {"session_id": sid}},
            status=status.HTTP_200_OK,
        )


class LiveChatPublicMessagesAPIView(APIView):
    """
    Public (customer-side) endpoints used inside the bot widget iframe.
    Auth is by token (sent to the widget after the customer entry is created).

    GET  /api/live-chat/public/messages/?token=<public_token>&limit=50
    POST /api/live-chat/public/messages/?token=<public_token>
      body: { "message": "..." }
    """

    permission_classes = [AllowAny]

    def _get_session_by_token(self, token: str):
        return LiveChatSession.objects.select_related("customer", "seller_user", "source_bot").get(
            public_token=token
        )

    def get(self, request):
        token = request.query_params.get("token")
        limit_raw = request.query_params.get("limit", "50")
        try:
            limit = int(limit_raw)
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 200))

        if not token:
            return Response(
                {"success": False, "message": "token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            session = self._get_session_by_token(token)
            cached = _get_persisted_live_chat_messages(int(session.session_id))
            if cached:
                return Response(
                    {"success": True, "data": cached[-limit:]},
                    status=status.HTTP_200_OK,
                )

            messages_qs = session.messages.order_by("created_at").all()[:limit]
        except LiveChatSession.DoesNotExist:
            return Response(
                {"success": False, "message": "Invalid token."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = LiveChatMessageSerializer(messages_qs, many=True)
        return Response({"success": True, "data": serializer.data}, status=status.HTTP_200_OK)

    def post(self, request):
        token = request.query_params.get("token")
        if not token:
            return Response(
                {"success": False, "message": "token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message_text = (request.data.get("message") or "").strip()
        if not message_text:
            return Response(
                {"success": False, "message": "message is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            session = self._get_session_by_token(token)
            sid = int(session.session_id)
            cached = _get_persisted_live_chat_messages(sid)
            if not cached:
                # Backfill from DB for older sessions that may have persisted data.
                db_msgs = session.messages.order_by("created_at").all()[:200]
                cached = []
                for m in db_msgs:
                    cached.append(
                        {
                            "message_id": m.message_id,
                            "sender_type": m.sender_type,
                            "message": m.message,
                            "created_at": m.created_at.isoformat() if m.created_at else None,
                        }
                    )

            new_msg = {
                "message_id": uuid.uuid4().hex,
                "sender_type": LiveChatMessage.SENDER_CUSTOMER,
                "message": message_text,
                "created_at": timezone.now().isoformat(),
            }
            cached.append(new_msg)
            _set_persisted_live_chat_messages(sid, cached)
        except LiveChatSession.DoesNotExist:
            return Response(
                {"success": False, "message": "Invalid token."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({"success": True, "message": "Message sent.", "data": new_msg}, status=status.HTTP_201_CREATED)


class LiveChatPublicSessionAPIView(APIView):
    """
    Public endpoint to create a live chat session without creating Customer rows.
    """

    permission_classes = [AllowAny]
    parser_classes = [JSONParser]

    def post(self, request):
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.select_related("seller_user").get(bot_id=bot_id, is_published=True)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found or not published."}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data or {}
        full_name = (payload.get("full_name") or "").strip()
        mobile_number = (payload.get("mobile_number") or "").strip()
        email = (payload.get("email") or "").strip()
        flow_messages = payload.get("flow_messages")
        if not isinstance(flow_messages, list):
            flow_messages = []
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        node_id = payload.get("last_agent_assign_node_id") or extra.get("last_agent_assign_node_id")
        assigned_team_manager, assigned_agent = _resolve_agent_assignment(bot, node_id=node_id)

        session = LiveChatSession.objects.create(
            seller_user=bot.seller_user,
            customer=None,
            source_bot=bot,
            visitor_name=full_name,
            visitor_mobile_number=mobile_number,
            visitor_email=email,
            visitor_payload=extra,
            assigned_team_manager=assigned_team_manager,
            assigned_agent=assigned_agent,
        )

        _sync_session_assignment_to_data_hub(session)

        if flow_messages:
            base_time = timezone.now()
            chat_rows = []
            for idx, m in enumerate(flow_messages):
                row = _flow_message_to_chat_row(m, idx, base_time)
                if row:
                    chat_rows.append(row)
            if chat_rows:
                _set_persisted_live_chat_messages(int(session.session_id), chat_rows)

        return Response(
            {
                "success": True,
                "message": "Live chat session created.",
                "data": LiveChatSessionPublicSerializer(session).data,
            },
            status=status.HTTP_201_CREATED,
        )


class LiveChatPublicTranscriptSyncAPIView(APIView):
    """
    Public: replace bot flow transcript on an existing session (e.g. after media upload).
    POST /api/live-chat/public/transcript/?token=<public_token>
    body: { "flow_messages": [{ type, text, file_url?, ... }] }
    """

    permission_classes = [AllowAny]
    parser_classes = [JSONParser]

    def post(self, request):
        token = (request.query_params.get("token") or "").strip()
        if not token:
            return Response({"success": False, "message": "token is required."}, status=status.HTTP_400_BAD_REQUEST)

        flow_messages = request.data.get("flow_messages")
        if not isinstance(flow_messages, list):
            flow_messages = []

        try:
            session = LiveChatSession.objects.get(public_token=token)
        except LiveChatSession.DoesNotExist:
            return Response({"success": False, "message": "Invalid token."}, status=status.HTTP_404_NOT_FOUND)

        base_time = timezone.now()
        chat_rows = []
        for idx, m in enumerate(flow_messages):
            row = _flow_message_to_chat_row(m, idx, base_time)
            if row:
                chat_rows.append(row)

        if chat_rows:
            _set_persisted_live_chat_messages(int(session.session_id), chat_rows)

        return Response(
            {"success": True, "message": "Transcript updated.", "data": chat_rows[-50:]},
            status=status.HTTP_200_OK,
        )


class BotMediaDownloadAPIView(APIView):
    """
    Force-download bot-uploaded media with Content-Disposition: attachment.
    GET /api/bots/media/download/?path=bot_uploads/<bot_id>/<file>&name=<original_name>
    """

    permission_classes = [AllowAny]

    def get(self, request):
        rel = (request.query_params.get("path") or "").strip().replace("\\", "/")
        if not rel or ".." in rel:
            return Response({"success": False, "message": "Invalid path."}, status=status.HTTP_400_BAD_REQUEST)

        if rel.startswith("media/"):
            rel = rel[len("media/") :]

        if not rel.startswith("bot_uploads/") and not rel.startswith("bot_downloads/"):
            return Response({"success": False, "message": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

        media_root = os.path.normpath(str(settings.MEDIA_ROOT))
        abs_path = os.path.normpath(os.path.join(media_root, rel))
        if not abs_path.startswith(media_root) or not os.path.isfile(abs_path):
            return Response({"success": False, "message": "File not found."}, status=status.HTTP_404_NOT_FOUND)

        download_name = (request.query_params.get("name") or "").strip() or os.path.basename(rel)
        download_name = re.sub(r"[^\w.\- ()]", "_", download_name) or "download"

        content_type = None
        guessed = mimetypes.guess_type(abs_path)[0]
        if guessed:
            content_type = guessed

        file_handle = open(abs_path, "rb")
        response = FileResponse(file_handle, as_attachment=True, filename=download_name)
        if content_type:
            response["Content-Type"] = content_type
        return response


class BotPublicMediaUploadAPIView(APIView):
    """
    Public media upload for bot widget (image / video / pdf).
    POST /api/bots/public/upload/?bot_id=<id>
    multipart: file
    """

    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser]

    VIDEO_MAX_BYTES = 10 * 1024 * 1024
    OTHER_MAX_BYTES = 15 * 1024 * 1024

    def post(self, request):
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.get(bot_id=bot_id, is_published=True)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found or not published."}, status=status.HTTP_404_NOT_FOUND)

        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response({"success": False, "message": "file is required."}, status=status.HTTP_400_BAD_REQUEST)

        mime_type = (getattr(uploaded, "content_type", "") or "").lower()
        original_name = (getattr(uploaded, "name", "") or "upload").strip()
        attachment_type = _detect_bot_attachment_type(mime_type, original_name)

        allowed = attachment_type in {"image", "video", "pdf", "document", "audio", "text"}
        if not allowed:
            return Response(
                {"success": False, "message": "File type is not allowed. Upload image, video, or PDF."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if attachment_type == "video" and uploaded.size > self.VIDEO_MAX_BYTES:
            return Response(
                {"success": False, "message": "Video must be 10 MB or smaller."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if attachment_type != "video" and uploaded.size > self.OTHER_MAX_BYTES:
            return Response(
                {"success": False, "message": "File must be 15 MB or smaller."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ext = os.path.splitext(original_name)[1].lower()
        if not ext:
            ext_map = {
                "image": ".jpg",
                "video": ".mp4",
                "pdf": ".pdf",
                "audio": ".mp3",
                "text": ".txt",
            }
            ext = ext_map.get(attachment_type, ".bin")

        safe_name = f"{uuid.uuid4().hex}{ext}"
        rel_dir = f"bot_uploads/{bot.bot_id}"
        saved_path = default_storage.save(f"{rel_dir}/{safe_name}", ContentFile(uploaded.read()))
        file_url = request.build_absolute_uri(f"/media/{saved_path}")

        return Response(
            {
                "success": True,
                "message": "File uploaded.",
                "data": {
                    "file_url": file_url,
                    "file_path": saved_path,
                    "file_name": original_name,
                    "mime_type": mime_type,
                    "attachment_type": attachment_type,
                    "size": uploaded.size,
                    "path": f"media/{saved_path}",
                },
            },
            status=status.HTTP_201_CREATED,
        )


class BotSellerMediaUploadAPIView(APIView):
    """
    Seller bot builder: upload image / video / pdf for Media Download component.
    POST /api/bots/media/upload/?bot_id=<id>
    multipart: file
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    VIDEO_MAX_BYTES = 10 * 1024 * 1024
    OTHER_MAX_BYTES = 15 * 1024 * 1024

    def post(self, request):
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            return Response({"success": False, "message": "bot_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Bot.objects.get(bot_id=bot_id, seller_user_id=request.user.id)
        except Bot.DoesNotExist:
            return Response({"success": False, "message": "Bot not found."}, status=status.HTTP_404_NOT_FOUND)

        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response({"success": False, "message": "file is required."}, status=status.HTTP_400_BAD_REQUEST)

        mime_type = (getattr(uploaded, "content_type", "") or "").lower()
        original_name = (getattr(uploaded, "name", "") or "upload").strip()
        attachment_type = _detect_bot_attachment_type(mime_type, original_name)

        if attachment_type not in {"image", "video", "pdf"}:
            return Response(
                {"success": False, "message": "Only image, video, or PDF files are allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if attachment_type == "video" and uploaded.size > self.VIDEO_MAX_BYTES:
            return Response(
                {"success": False, "message": "Video must be 10 MB or smaller."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if attachment_type != "video" and uploaded.size > self.OTHER_MAX_BYTES:
            return Response(
                {"success": False, "message": "File must be 15 MB or smaller."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ext = os.path.splitext(original_name)[1].lower()
        if not ext:
            ext_map = {"image": ".jpg", "video": ".mp4", "pdf": ".pdf"}
            ext = ext_map.get(attachment_type, ".bin")

        safe_name = f"{uuid.uuid4().hex}{ext}"
        rel_dir = f"bot_downloads/{bot.bot_id}"
        saved_path = default_storage.save(f"{rel_dir}/{safe_name}", ContentFile(uploaded.read()))
        file_url = request.build_absolute_uri(f"/media/{saved_path}")

        return Response(
            {
                "success": True,
                "message": "File uploaded.",
                "data": {
                    "file_url": file_url,
                    "file_path": saved_path,
                    "file_name": original_name,
                    "mime_type": mime_type,
                    "attachment_type": attachment_type,
                    "size": uploaded.size,
                    "path": f"media/{saved_path}",
                },
            },
            status=status.HTTP_201_CREATED,
        )


@method_decorator(xframe_options_exempt, name='dispatch')
class BotWidgetHTMLAPIView(APIView):
    """
    GET /widget/bot?bot_id=123

    Serves a self-contained HTML widget runner inside an iframe.
    It fetches the published bot from /api/bots/public/ and runs the saved flow.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        bot_id = request.query_params.get("bot_id", "")

        # Keep URLs absolute so it works when embedded on any domain.
        api_public_url = request.build_absolute_uri(f"/api/bots/public/?bot_id={bot_id}")
        # Force HTTPS in production to prevent mixed-content blocks
        host = request.get_host().lower()
        if not ("localhost" in host or "127.0.0.1" in host):
            if api_public_url.startswith("http://"):
                api_public_url = "https://" + api_public_url[7:]

        html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Bot Widget</title>
    <style>
      :root {{
        --primary: #0ea5e9;
        --bg: #0f172a;
        --panel: #1e293b;
        --text: #e5e7eb;
        --muted: #9ca3af;
      }}
      body {{
        margin: 0;
        font-family: Arial, sans-serif;
        background: var(--bg);
        color: var(--text);
        height: 100vh;
        overflow: hidden;
      }}
      .wrap {{
        height: 100vh;
        display: flex;
        flex-direction: column;
      }}
      .header {{
        padding: 14px 16px;
        background: var(--primary);
        color: #fff;
      }}
      .header .title {{ font-weight: 700; font-size: 14px; }}
      .header .sub {{ font-size: 12px; opacity: .9; margin-top: 2px; }}
      .msgs {{
        flex: 1;
        padding: 14px;
        overflow: auto;
        background: #0b1624;
      }}
      .row {{ display: flex; margin-bottom: 10px; }}
      .row.user {{ justify-content: flex-end; }}
      .bubble {{
        max-width: 85%;
        padding: 10px 12px;
        border-radius: 12px;
        background: var(--panel);
        color: #fff;
        font-size: 13px;
        white-space: pre-wrap;
        word-break: break-word;
      }}
      .row.user .bubble {{
        background: var(--primary);
      }}
      .input {{
        padding: 12px;
        display: flex;
        gap: 8px;
        border-top: 1px solid rgba(148,163,184,0.2);
        background: var(--bg);
      }}
      .input input {{
        flex: 1;
        padding: 10px 12px;
        border-radius: 10px;
        border: 1px solid rgba(148,163,184,0.25);
        background: #0b1624;
        color: var(--text);
        outline: none;
        font-size: 13px;
      }}
      .btn {{
        width: 44px;
        border: 0;
        border-radius: 10px;
        background: var(--primary);
        color: #fff;
        cursor: pointer;
        font-weight: 700;
      }}
      .btn:disabled {{
        opacity: .5;
        cursor: not-allowed;
      }}
      .error {{
        padding: 12px 14px;
        color: #fecaca;
        background: rgba(239,68,68,0.12);
        border-top: 1px solid rgba(239,68,68,0.25);
        font-size: 13px;
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="header">
        <div class="title" id="hdrTitle">Chat with us</div>
        <div class="sub" id="hdrSub">Bot</div>
      </div>
      <div class="msgs" id="msgs"></div>
      <div class="input">
        <input id="inp" placeholder="Type your response..." />
        <button class="btn" id="send">➤</button>
      </div>
      <div class="error" id="err" style="display:none;"></div>
    </div>

    <script>
      const msgsEl = document.getElementById('msgs');
      const inpEl = document.getElementById('inp');
      const sendEl = document.getElementById('send');
      const errEl = document.getElementById('err');
      const hdrTitle = document.getElementById('hdrTitle');
      const hdrSub = document.getElementById('hdrSub');

      let bot = null;
      let flow = null;
      let nodesById = {{}};
      let edges = [];
      let currentNodeId = 'start';
      let waitingForInput = false;

      function showError(text) {{
        errEl.style.display = 'block';
        errEl.textContent = text;
      }}

      function addMsg(type, text) {{
        const row = document.createElement('div');
        row.className = 'row ' + (type === 'user' ? 'user' : 'bot');
        const b = document.createElement('div');
        b.className = 'bubble';
        b.textContent = text;
        row.appendChild(b);
        msgsEl.appendChild(row);
        msgsEl.scrollTop = msgsEl.scrollHeight;
      }}

      function nextNodeId(sourceId) {{
        const out = edges.filter(e => e.source === sourceId);
        return out.length ? out[0].target : null;
      }}

      function step() {{
        if (!flow) return;
        const node = nodesById[currentNodeId];
        if (!node) return;

        if (node.id === 'start') {{
          const nxt = nextNodeId(node.id);
          if (nxt) {{
            currentNodeId = nxt;
            step();
          }}
          return;
        }}

        const kind = (node.data && node.data.kind) || '';
        if (kind === 'message') {{
          addMsg('bot', node.data.text || node.data.label || '...');
          waitingForInput = false;
          const nxt = nextNodeId(node.id);
          if (nxt) {{
            currentNodeId = nxt;
            setTimeout(step, 200);
          }}
          return;
        }}

        if (kind === 'name' || kind === 'phone' || kind === 'custom_input') {{
          addMsg('bot', node.data.prompt || node.data.label || 'Please enter a value');
          waitingForInput = true;
          return;
        }}
      }}

      function onSend() {{
        if (!waitingForInput) return;
        const val = (inpEl.value || '').trim();
        if (!val) return;
        addMsg('user', val);
        inpEl.value = '';
        waitingForInput = false;
        const nxt = nextNodeId(currentNodeId);
        if (nxt) {{
          currentNodeId = nxt;
          step();
        }}
      }}

      sendEl.addEventListener('click', onSend);
      inpEl.addEventListener('keydown', (e) => {{
        if (e.key === 'Enter') {{
          e.preventDefault();
          onSend();
        }}
      }});

      // Load bot
      fetch('{api_public_url}')
        .then(r => r.json())
        .then(payload => {{
          if (!payload || !payload.success) {{
            throw new Error((payload && payload.message) || 'Bot not found / not published');
          }}
          bot = payload.data;
          flow = bot.flow || {{}};
          edges = flow.edges || [];
          (flow.nodes || []).forEach(n => nodesById[n.id] = n);

          const design = bot.design || {{}};
          if (design.primaryColor) document.documentElement.style.setProperty('--primary', design.primaryColor);
          hdrTitle.textContent = design.chatHeaderTitle || 'Chat with us';
          hdrSub.textContent = design.chatbotName || (bot.name || 'Bot');

          addMsg('bot', 'Welcome 👋');
          currentNodeId = 'start';
          step();
        }})
        .catch(err => {{
          console.error(err);
          showError(err.message || 'Failed to load bot');
        }});
    </script>
  </body>
</html>
"""
        return HttpResponse(html, content_type="text/html")


class SellerAPIView(APIView):
    """
    POST: register seller (public)
    GET: list sellers (admin only)
    PATCH: update seller status (admin only)
    PUT: update seller's own profile (authenticated seller only)
    """

    def get_permissions(self):
        if self.request.method == "GET" or self.request.method == "PATCH":
            return [IsAdmin()]
        elif self.request.method == "PUT":
            return [IsAuthenticated()]
        return [AllowAny()]

    def post(self, request):
        serializer = SellerRegistrationSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            user = serializer.save()
            return Response(
                {
                    "success": True,
                    "message": "Seller registered successfully.",
                    "user_id": user.id,
                },
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Seller registration failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to register seller at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        sellers = (
            User.objects.select_related("role", "meta")
            .filter(role__role_id=UserRole.ROLE_SELLER)
            .order_by("-created_at")
        )
        data = UserDetailSerializer(sellers, many=True).data
        return Response(
            {"success": True, "count": len(data), "data": data},
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        """
        Update seller status (admin only)
        
        Query params:
        - user_id (required): The ID of the seller to update
        
        Body:
        - status (required): New status ('active', 'inactive', or 'pending')
        """
        user_id = request.query_params.get("user_id")
        
        if not user_id:
            return Response(
                {"success": False, "message": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            seller = User.objects.select_related("role", "meta").get(
                id=user_id,
                role__role_id=UserRole.ROLE_SELLER
            )
        except User.DoesNotExist:
            return Response(
                {"success": False, "message": "Seller not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        
        new_status = request.data.get("status")
        if not new_status:
            return Response(
                {"success": False, "message": "status is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        if new_status not in [User.STATUS_ACTIVE, User.STATUS_INACTIVE, User.STATUS_PENDING]:
            return Response(
                {"success": False, "message": f"Invalid status. Must be one of: {User.STATUS_ACTIVE}, {User.STATUS_INACTIVE}, {User.STATUS_PENDING}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        seller.status = new_status
        seller.save(update_fields=['status'])
        
        return Response(
            {
                "success": True,
                "message": f"Seller status updated to {new_status} successfully.",
                "data": UserDetailSerializer(seller).data,
            },
            status=status.HTTP_200_OK,
        )

    def put(self, request):
        """
        Update seller's own profile (authenticated seller only)
        
        Body:
        - full_name (optional): Full name
        - phone (optional): Phone number
        - company_name (optional): Company name
        - gst_number (optional): GST number
        - address (optional): Address
        - pan_number (optional): PAN number
        - account_holder_name (optional): Account holder name
        - account_number (optional): Account number
        - ifsc_code (optional): IFSC code
        - bank_name (optional): Bank name
        """
        if not request.user.is_authenticated:
            return Response(
                {"success": False, "message": "Authentication required."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        
        try:
            seller = User.objects.select_related("role", "meta").get(
                id=request.user.id,
                role__role_id=UserRole.ROLE_SELLER
            )
        except User.DoesNotExist:
            return Response(
                {"success": False, "message": "Seller not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        
        # Update User fields
        user_fields = ['full_name', 'phone', 'company_name', 'gst_number', 'address']
        update_data = {}
        for field in user_fields:
            if field in request.data:
                update_data[field] = request.data[field]
        
        # Update User model
        for field, value in update_data.items():
            setattr(seller, field, value)
        
        if update_data:
            seller.save(update_fields=list(update_data.keys()))
        
        # Update UserMeta fields
        meta_fields = ['pan_number', 'account_holder_name', 'account_number', 'ifsc_code', 'bank_name']
        meta_update_data = {}
        for field in meta_fields:
            if field in request.data:
                meta_update_data[field] = request.data[field]
        
        if meta_update_data:
            # Get or create UserMeta
            user_meta, created = UserMeta.objects.get_or_create(user=seller)
            for field, value in meta_update_data.items():
                setattr(user_meta, field, value)
            user_meta.save(update_fields=list(meta_update_data.keys()))
        
        # Refresh from database
        seller.refresh_from_db()
        
        return Response(
            {
                "success": True,
                "message": "Profile updated successfully.",
                "data": UserDetailSerializer(seller).data,
            },
            status=status.HTTP_200_OK,
        )


class SellerTeamMemberAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def _ensure_seller(self, request):
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response(
                {"success": False, "message": "Only sellers can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _resolve_seller_user(self, request):
        if request.user.role.role_id == UserRole.ROLE_SELLER:
            return request.user
        elif request.user.role.role_id in [UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            profile = TeamMember.objects.filter(user=request.user).select_related("seller_user").first()
            if profile:
                return profile.seller_user
        return None

    def get(self, request):
        seller = self._resolve_seller_user(request)
        if not seller:
            return Response(
                {"success": False, "message": "Access denied."},
                status=status.HTTP_403_FORBIDDEN,
            )

        members = (
            TeamMember.objects.select_related("user", "user__role", "parent_team_manager")
            .filter(seller_user=seller)
            .order_by("-created_at")
        )
        data = TeamMemberSerializer(members, many=True).data
        return Response(
            {"success": True, "count": len(data), "data": data},
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        not_allowed = self._ensure_seller(request)
        if not_allowed:
            return not_allowed

        serializer = TeamMemberCreateSerializer(
            data=request.data,
            context={"seller_user": request.user},
        )
        try:
            serializer.is_valid(raise_exception=True)
            member = serializer.save()
            return Response(
                {
                    "success": True,
                    "message": "User created successfully.",
                    "data": TeamMemberSerializer(member).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

    def patch(self, request):
        not_allowed = self._ensure_seller(request)
        if not_allowed:
            return not_allowed

        team_member_id = request.query_params.get("team_member_id") or request.data.get("team_member_id")
        if not team_member_id:
            return Response(
                {"success": False, "message": "team_member_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        member = TeamMember.objects.select_related("user").filter(
            seller_user=request.user,
            team_member_id=team_member_id,
        ).first()
        if not member:
            return Response(
                {"success": False, "message": "User not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = TeamMemberUpdateSerializer(
            instance=member,
            data=request.data,
            partial=True,
            context={"seller_user": request.user},
        )
        try:
            serializer.is_valid(raise_exception=True)
            updated_member = serializer.save()
            return Response(
                {
                    "success": True,
                    "message": "User updated successfully.",
                    "data": TeamMemberSerializer(updated_member).data,
                },
                status=status.HTTP_200_OK,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )


class SellerTeamManagerOptionsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response(
                {"success": False, "message": "Only sellers can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        managers = (
            TeamMember.objects.select_related("user")
            .filter(
                seller_user=request.user,
                member_type=TeamMember.MEMBER_TYPE_TEAM_MANAGER,
            )
            .order_by("team_name", "first_name")
        )
        data = [
            {
                "id": item.user_id,
                "team_name": item.team_name,
                "manager_name": item.user.full_name,
            }
            for item in managers
        ]
        return Response(
            {"success": True, "count": len(data), "data": data},
            status=status.HTTP_200_OK,
        )


class TeamMemberContextAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user.role.role_id not in [UserRole.ROLE_TEAM_MANAGER, UserRole.ROLE_AGENT]:
            return Response(
                {"success": False, "message": "Only team members can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            member = TeamMember.objects.select_related("seller_user", "user", "parent_team_manager").get(
                user=request.user
            )
        except TeamMember.DoesNotExist:
            return Response(
                {"success": False, "message": "Team member profile not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        effective_team_name = (member.team_name or "").strip()
        if not effective_team_name and member.parent_team_manager_id:
            parent_profile = TeamMember.objects.filter(
                seller_user=member.seller_user,
                member_type=TeamMember.MEMBER_TYPE_TEAM_MANAGER,
                user_id=member.parent_team_manager_id,
            ).first()
            if parent_profile:
                effective_team_name = (parent_profile.team_name or "").strip()

        payload = {
            "team_member": {
                "id": member.team_member_id,
                "name": member.user.full_name,
                "email": member.user.email,
                "member_type": member.member_type,
                "city": member.city,
                "team_name": effective_team_name,
            },
            "seller": {
                "id": member.seller_user_id,
                "name": member.seller_user.full_name,
                "company_name": member.seller_user.company_name,
                "email": member.seller_user.email,
            },
            "assigned_team_manager": (
                {
                    "id": member.parent_team_manager_id,
                    "name": member.parent_team_manager.full_name,
                }
                if member.parent_team_manager
                else None
            ),
        }
        return Response({"success": True, "data": payload}, status=status.HTTP_200_OK)


class CategoryAPIView(APIView):
    """
    POST: add category (authenticated seller only)
    GET: list categories for authenticated seller, or get specific category by category_id
    PATCH: update category (authenticated seller only)
    DELETE: delete category (authenticated seller only)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Add a new category.
        
        The seller_user_id can be obtained from:
        1. The authenticated user's session (request.user.id)
        2. Or explicitly provided in the request data
        
        If seller_user_id is provided in the request, it will be used.
        Otherwise, the authenticated user's ID will be used.
        """
        data = request.data.copy()

        # If seller_user is not provided, use the authenticated user's ID
        if "seller_user" not in data and request.user.id:
            data["seller_user"] = request.user.id

        # Check if category with same name and seller already exists (case-insensitive)
        category_name = data.get('category_name', '').strip()
        seller_user_id = data.get('seller_user')
        
        if category_name and seller_user_id:
            existing_category = Category.objects.filter(
                category_name__iexact=category_name,
                seller_user_id=seller_user_id
            ).first()
            if existing_category:
                return Response(
                    {
                        "success": False,
                        "message": f"Category '{existing_category.category_name}' already exists for this seller.",
                        "category_id": existing_category.category_id,
                        "data": CategorySerializer(existing_category).data,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        serializer = CategorySerializer(data=data, context={'seller_user': request.user})
        try:
            serializer.is_valid(raise_exception=True)
            category = serializer.save()
            return Response(
                {
                    "success": True,
                    "message": "Category added successfully.",
                    "category_id": category.category_id,
                    "data": CategorySerializer(category).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Category creation failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to add category at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        """
        Get categories for the authenticated seller.
        
        Query params:
        - category_id (optional): Get a specific category by ID
        - seller_user (optional): Filter by seller_user ID (defaults to authenticated user)
        
        If category_id is provided, returns a single category.
        Otherwise, returns all categories for the seller.
        """
        category_id = request.query_params.get("category_id")
        seller_user_id = request.query_params.get("seller_user") or request.user.id
        
        try:
            if category_id:
                # Get specific category
                category = Category.objects.get(
                    category_id=category_id,
                    seller_user_id=seller_user_id
                )
                return Response(
                    {
                        "success": True,
                        "data": CategorySerializer(category).data,
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                # Get all categories for the seller
                categories = Category.objects.filter(
                    seller_user_id=seller_user_id
                ).order_by("category_name")
                
                data = CategorySerializer(categories, many=True).data
                return Response(
                    {
                        "success": True,
                        "count": len(data),
                        "data": data,
                    },
                    status=status.HTTP_200_OK,
                )
        except Category.DoesNotExist:
            return Response(
                {"success": False, "message": "Category not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Failed to fetch categories")
            return Response(
                {
                    "success": False,
                    "message": "Unable to fetch categories at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def patch(self, request):
        """
        Update an existing category (partial update).
        
        Query params:
        - category_id (required): The ID of the category to update
        
        Body: Fields to update (category_name, description)
        """
        category_id = request.query_params.get("category_id")
        
        if not category_id:
            return Response(
                {"success": False, "message": "category_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            # Get the category and verify ownership
            category = Category.objects.get(
                category_id=category_id,
                seller_user=request.user
            )
            
            # Prevent changing seller_user
            data = request.data.copy()
            data.pop("seller_user", None)
            
            # Check for duplicate category name if updating name
            new_category_name = data.get('category_name', '').strip()
            if new_category_name and new_category_name.lower() != category.category_name.lower():
                existing_category = Category.objects.filter(
                    category_name__iexact=new_category_name,
                    seller_user=request.user
                ).exclude(category_id=category_id).first()
                if existing_category:
                    return Response(
                        {
                            "success": False,
                            "message": f"Category '{existing_category.category_name}' already exists.",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            
            serializer = CategorySerializer(category, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_category = serializer.save()
            
            return Response(
                {
                    "success": True,
                    "message": "Category updated successfully.",
                    "data": CategorySerializer(updated_category).data,
                },
                status=status.HTTP_200_OK,
            )
        except Category.DoesNotExist:
            return Response(
                {"success": False, "message": "Category not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Category update failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to update category at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def delete(self, request):
        """
        Delete a category.
        
        Query params:
        - category_id (required): The ID of the category to delete
        """
        category_id = request.query_params.get("category_id")
        
        if not category_id:
            return Response(
                {"success": False, "message": "category_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            # Get the category and verify ownership
            category = Category.objects.get(
                category_id=category_id,
                seller_user=request.user
            )
            
            category_name = category.category_name
            category.delete()
            
            return Response(
                {
                    "success": True,
                    "message": f"Category '{category_name}' deleted successfully.",
                },
                status=status.HTTP_200_OK,
            )
        except Category.DoesNotExist:
            return Response(
                {"success": False, "message": "Category not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Category deletion failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to delete category at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class SupplierAPIView(APIView):
    """
    POST: add supplier (authenticated seller only)
    GET: list suppliers for authenticated seller, or get specific supplier by supplier_id
    PATCH: update supplier (authenticated seller only)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Add a new supplier.
        
        The seller_user_id can be obtained from:
        1. The authenticated user's session (request.user.id)
        2. Or explicitly provided in the request data
        
        If seller_user_id is provided in the request, it will be used.
        Otherwise, the authenticated user's ID will be used.
        """
        data = request.data.copy()

        # If seller_user_id is not provided, use the authenticated user's ID
        if "seller_user" not in data and request.user.id:
            data["seller_user"] = request.user.id

        serializer = SupplierSerializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
            supplier = serializer.save()
            return Response(
                {
                    "success": True,
                    "message": "Supplier added successfully.",
                    "supplier_id": supplier.supplier_id,
                    "data": SupplierSerializer(supplier).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Supplier creation failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to add supplier at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        """
        Get suppliers for the authenticated seller.
        
        Query params:
        - supplier_id (optional): Get a specific supplier by ID
        
        If supplier_id is provided, returns a single supplier.
        Otherwise, returns all suppliers for the authenticated user.
        """
        supplier_id = request.query_params.get("supplier_id")
        
        try:
            if supplier_id:
                # Get specific supplier
                supplier = Supplier.objects.get(
                    supplier_id=supplier_id,
                    seller_user=request.user
                )
                return Response(
                    {
                        "success": True,
                        "data": SupplierSerializer(supplier).data,
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                # Get all suppliers for the authenticated seller
                suppliers = Supplier.objects.filter(
                    seller_user=request.user
                ).order_by("-created_at")
                
                data = SupplierSerializer(suppliers, many=True).data
                return Response(
                    {
                        "success": True,
                        "count": len(data),
                        "data": data,
                    },
                    status=status.HTTP_200_OK,
                )
        except Supplier.DoesNotExist:
            return Response(
                {"success": False, "message": "Supplier not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Failed to fetch suppliers")
            return Response(
                {
                    "success": False,
                    "message": "Unable to fetch suppliers at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def patch(self, request):
        """
        Update an existing supplier (partial update).
        
        Query params:
        - supplier_id (required): The ID of the supplier to update
        
        Body: Fields to update (supplier_name, email_id, mobile_number, etc.)
        """
        supplier_id = request.query_params.get("supplier_id")
        
        if not supplier_id:
            return Response(
                {"success": False, "message": "supplier_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            # Get the supplier and verify ownership
            supplier = Supplier.objects.get(
                supplier_id=supplier_id,
                seller_user=request.user
            )
            
            # Prevent changing seller_user
            data = request.data.copy()
            data.pop("seller_user", None)
            
            serializer = SupplierSerializer(supplier, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_supplier = serializer.save()
            
            return Response(
                {
                    "success": True,
                    "message": "Supplier updated successfully.",
                    "data": SupplierSerializer(updated_supplier).data,
                },
                status=status.HTTP_200_OK,
            )
        except Supplier.DoesNotExist:
            return Response(
                {"success": False, "message": "Supplier not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Supplier update failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to update supplier at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ProductAPIView(APIView):
    """
    POST: add product (admin only)
    GET: list products (public)
    """

    # def get_permissions(self):
    #     if self.request.method == "GET":
    #         return [AllowAny()]
    #     return [IsAdmin()]

    def post(self, request):
        """
        Add a new product.
        If initial_quantity is provided, also creates an inventory transaction (stock in)
        and sets the minimum_stock_alert to the initial quantity.
        """
        from datetime import date
        
        product_name = request.data.get('product_name', '').strip()
        seller_user_id = request.data.get('seller_user')
        initial_quantity = request.data.get('initial_quantity')  # Optional field for direct stock in
        
        if not product_name:
            return Response(
                {
                    "success": False,
                    "message": "Product name is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not seller_user_id:
            return Response(
                {
                    "success": False,
                    "message": "seller_user (seller's user id) is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Check if product with same name and seller already exists (case-insensitive)
        existing_product = Product.objects.filter(
            product_name__iexact=product_name,
            seller_user_id=seller_user_id
        ).first()
        if existing_product:
            return Response(
                {
                    "success": False,
                    "message": f"Product '{existing_product.product_name}' already exists for this seller with ID {existing_product.product_id}.",
                    "product_id": existing_product.product_id,
                    "data": ProductSerializer(existing_product).data,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        serializer = ProductSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            product = serializer.save()
            
            inventory_transaction = None
            # If initial_quantity is provided, create an inventory transaction
            if initial_quantity and int(initial_quantity) > 0:
                quantity = int(initial_quantity)
                
                # Set minimum_stock_alert to the initial quantity
                product.minimum_stock_alert = quantity
                product.save(update_fields=['minimum_stock_alert'])
                
                # Create inventory transaction for direct stock in
                inventory_transaction = InventoryTransaction.objects.create(
                    product=product,
                    batch_name="",  # No batch name for direct product creation
                    quantity=quantity,
                    transaction_type=InventoryTransaction.TRANSACTION_TYPE_IN,
                    reason="Initial stock from product creation",
                    reference_id=f"PRODUCT-{product.product_id}",
                    date=date.today(),
                )
            
            response_data = {
                "success": True,
                "message": "Product added successfully.",
                "product_id": product.product_id,
                "data": ProductSerializer(product).data,
            }
            
            if inventory_transaction:
                response_data["inventory_transaction_id"] = inventory_transaction.transaction_id
                response_data["initial_stock_added"] = inventory_transaction.quantity
            
            return Response(response_data, status=status.HTTP_201_CREATED)
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Product creation failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to add product at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        """Get all products, optionally filter by seller_user."""
        seller_user_id = request.query_params.get('seller_user')
        products = Product.objects.all()
        if seller_user_id:
            products = products.filter(seller_user_id=seller_user_id)
        products = products.order_by("-created_at")
        data = ProductSerializer(products, many=True).data
        return Response(
            {"success": True, "count": len(data), "data": data},
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        """
        Update an existing product.
        Allows editing mrp, sale_price, purchase_price, gst, category, brand, unit, etc.
        Requires product_id in request data.
        """
        product_id = request.data.get('product_id')
        if not product_id:
            return Response(
                {
                    "success": False,
                    "message": "product_id is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            product = Product.objects.get(product_id=product_id)
        except Product.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "message": f"Product with ID {product_id} not found.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        
        # Fields that can be updated
        updatable_fields = [
            'product_name', 'category', 'brand', 'unit', 'sale_price', 'mrp',
            'purchase_price', 'gst', 'minimum_stock_alert', 'description'
        ]
        
        update_data = {}
        for field in updatable_fields:
            if field in request.data:
                update_data[field] = request.data[field]
        
        if not update_data:
            return Response(
                {
                    "success": False,
                    "message": "No valid fields provided for update.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        serializer = ProductSerializer(product, data=update_data, partial=True)
        try:
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(
                {
                    "success": True,
                    "message": "Product updated successfully.",
                    "data": ProductSerializer(product).data,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as exc:
            logger.exception("Product update failed")
            return Response(
                {
                    "success": False,
                    "message": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )





class SupplierPurchaseHistoryAPIView(APIView):
    """
    POST: add purchase record (seller only)
    GET: get purchase history by supplier_id (authenticated users)
    
    When a purchase is created, batches are automatically created/updated for each line item.
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsSeller()]
        return [IsAuthenticated()]

    def post(self, request):
        """
        Add a new purchase record (seller only).
        Accepts multiple line items in 'items' field.
        If a product in the line item does not exist, create it with the provided details.
        Automatically creates/updates batches for each line item.
        """
        from datetime import datetime
        # Handle multipart/form-data properly - request.data is QueryDict for multipart
        # For QueryDict, use .get() method which returns single values
        # For regular dict, use as-is
        try:
            # Check if it's a QueryDict (multipart form data)
            from django.http import QueryDict
            if isinstance(request.data, QueryDict):
                # QueryDict - extract values using .get() to get single values
                data = {
                    'supplier': request.data.get('supplier'),
                    'invoice_number': request.data.get('invoice_number'),
                    'invoice_date': request.data.get('invoice_date'),
                    'items': request.data.get('items'),  # This will be a JSON string
                }
            else:
                # Regular dict
                data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data) if request.data else {}
        except Exception as e:
            logger.exception(f"Error processing request data: {str(e)}")
            return Response(
                {"success": False, "message": f"Error processing request data: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Handle invoice file upload (store and pass path as string to serializer)
        uploaded_invoice = request.FILES.get("invoice_file")
        invoice_file_path = None
        if uploaded_invoice:
            try:
                # Build a safe, unique path under invoices/ folder
                # Files will be saved to MEDIA_ROOT/invoices/ (e.g., /etc/nginx/sites-available/backend/media/invoices/filename.pdf)
                ext = os.path.splitext(uploaded_invoice.name)[1] or ""
                unique_filename = f"{uuid.uuid4().hex}{ext}"
                filename = f"invoices/{unique_filename}"
                
                # Ensure invoices directory exists in MEDIA_ROOT
                if settings.MEDIA_ROOT:
                    invoices_dir = os.path.join(settings.MEDIA_ROOT, 'invoices')
                    if not os.path.exists(invoices_dir):
                        os.makedirs(invoices_dir, exist_ok=True)
                        logger.info(f"Created invoices directory: {invoices_dir}")
                    else:
                        logger.info(f"Invoices directory already exists: {invoices_dir}")
                
                # default_storage.save() saves to MEDIA_ROOT/invoices/filename, returns relative path
                # The file will be saved at: MEDIA_ROOT/invoices/{uuid}.pdf
                saved_path = default_storage.save(filename, ContentFile(uploaded_invoice.read()))
                
                # Log the actual save location for verification
                full_path = os.path.join(settings.MEDIA_ROOT, saved_path) if settings.MEDIA_ROOT else saved_path
                logger.info(f"Invoice file saved successfully. Relative path: {saved_path}, Full path: {full_path}")
                
                # Verify file exists
                if os.path.exists(full_path):
                    logger.info(f"Invoice file verified at: {full_path}")
                else:
                    logger.warning(f"Invoice file not found at expected path: {full_path}")
                
                # Store as media/invoices/filename (matching database format)
                # saved_path will be "invoices/filename.pdf", stored as "media/invoices/filename.pdf"
                invoice_file_path = f"media/{saved_path}"
            except Exception as e:
                logger.exception(f"Error saving invoice file: {str(e)}")
                import traceback
                error_trace = traceback.format_exc()
                logger.error(f"Invoice file save traceback: {error_trace}")
                return Response(
                    {"success": False, "message": f"Error saving invoice file: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        supplier_id = data.get("supplier")
        if not supplier_id:
            return Response({"success": False, "message": "supplier is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        # Convert supplier_id to int if it's a string
        try:
            supplier_id = int(supplier_id)
        except (ValueError, TypeError):
            return Response({"success": False, "message": "Invalid supplier ID."}, status=status.HTTP_400_BAD_REQUEST)

        items = data.get('items', [])
        # If items is a string (from JSON), parse it
        import json
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error for items: {str(e)}")
                return Response({"success": False, "message": f"Invalid items format: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.error(f"Error parsing items: {str(e)}")
                return Response({"success": False, "message": "Invalid items format."}, status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure items is a list
        if not isinstance(items, list):
            logger.error(f"Items is not a list, type: {type(items)}, value: {items}")
            return Response({"success": False, "message": "Items must be a list."}, status=status.HTTP_400_BAD_REQUEST)
        
        new_items = []
        created_batches = []
        
        for item in items:
            product_id = item.get('product') or item.get('product_id')
            product_name = item.get('product_name')
            seller_user_id = request.user.id if hasattr(request, 'user') and request.user else None
            batch_name = item.get('batch_name') or item.get('batch') or ''
            expiry_date = item.get('expiry_date')
            manufacturing_date = item.get('manufacturing_date')
            
            # Try to get product by id or name and seller
            product = None
            if product_id:
                try:
                    product = Product.objects.get(product_id=product_id)
                except Product.DoesNotExist:
                    product = None
            if not product and product_name and seller_user_id:
                # Try to get by name (case-insensitive) and seller
                product = Product.objects.filter(product_name__iexact=product_name, seller_user_id=seller_user_id).first()
            if not product and product_name and seller_user_id:
                # Create new product with required fields
                product_fields = {
                    'product_name': product_name,
                    'seller_user_id': seller_user_id,
                    'purchase_price': item.get('purchase_price'),
                    'sale_price': item.get('selling_price') or item.get('sale_price'),
                    'gst': item.get('gst'),
                    'mrp': item.get('mrp'),
                    'barcode': item.get('barcode'),
                    'unit': item.get('unit'),
                }
                # Remove None values
                product_fields = {k: v for k, v in product_fields.items() if v is not None}
                product = Product.objects.create(**product_fields)
            
            if product:
                # Always use the product id for the serializer
                item['product'] = product.product_id
                
                # Handle batch creation/update
                if batch_name:
                    # Parse expiry_date if string
                    parsed_expiry = None
                    if expiry_date:
                        if isinstance(expiry_date, str):
                            try:
                                parsed_expiry = datetime.strptime(expiry_date, '%Y-%m-%d').date()
                            except ValueError:
                                parsed_expiry = None
                        else:
                            parsed_expiry = expiry_date
                    
                    # Parse manufacturing_date if string
                    parsed_mfg = None
                    if manufacturing_date:
                        if isinstance(manufacturing_date, str):
                            try:
                                parsed_mfg = datetime.strptime(manufacturing_date, '%Y-%m-%d').date()
                            except ValueError:
                                parsed_mfg = None
                        else:
                            parsed_mfg = manufacturing_date
                    
                    # Check if batch already exists
                    existing_batch = Batch.objects.filter(
                        batch_name__iexact=batch_name,
                        product=product,
                        supplier_id=supplier_id
                    ).first()
                    
                    quantity = int(item.get('quantity', 0))
                    
                    if existing_batch:
                        # Update existing batch - add quantity
                        existing_batch.initial_quantity = (existing_batch.initial_quantity or 0) + quantity
                        existing_batch.available_quantity = (existing_batch.available_quantity or 0) + quantity
                        if parsed_expiry and (not existing_batch.expiry_date or parsed_expiry < existing_batch.expiry_date):
                            existing_batch.expiry_date = parsed_expiry
                        if item.get('purchase_price'):
                            existing_batch.purchase_price = item.get('purchase_price')
                        if item.get('selling_price'):
                            existing_batch.selling_price = item.get('selling_price')
                        if item.get('mrp'):
                            existing_batch.mrp = item.get('mrp')
                        existing_batch.save()
                        item['batch'] = existing_batch.batch_id
                        created_batches.append({
                            "batch_id": existing_batch.batch_id,
                            "batch_name": existing_batch.batch_name,
                            "action": "updated",
                            "quantity_added": quantity,
                        })
                    else:
                        # Create new batch
                        new_batch = Batch.objects.create(
                            batch_name=batch_name,
                            product=product,
                            supplier_id=supplier_id,
                            expiry_date=parsed_expiry,
                            manufacturing_date=parsed_mfg,
                            purchase_price=item.get('purchase_price'),
                            selling_price=item.get('selling_price'),
                            mrp=item.get('mrp'),
                            initial_quantity=quantity,
                            available_quantity=quantity,
                        )
                        item['batch'] = new_batch.batch_id
                        created_batches.append({
                            "batch_id": new_batch.batch_id,
                            "batch_name": new_batch.batch_name,
                            "action": "created",
                            "quantity": quantity,
                        })
                    
                    item['batch_name'] = batch_name
                    item['expiry_date'] = parsed_expiry
            
            # Handle 'batch' key conversion for backward compatibility
            if 'batch' in item and not isinstance(item.get('batch'), int):
                item['batch_name'] = item.pop('batch')
            
            new_items.append(item)

        # Build a clean payload dict (avoid QueryDict quirks with lists)
        payload = {
            "supplier": supplier_id,
            "invoice_number": data.get("invoice_number"),
            "invoice_date": data.get("invoice_date"),
            "items": new_items,
        }
        # Add invoice_file path if it was saved
        if invoice_file_path:
            payload["invoice_file"] = invoice_file_path

        serializer = ProductPurchaseSerializer(data=payload, context={'request': request})
        try:
            serializer.is_valid(raise_exception=True)
            purchase = serializer.save()
            
            response_data = {
                "success": True,
                "message": "Purchase record added successfully.",
                "product_purchase_id": purchase.product_purchase_id,
                "data": ProductPurchaseSerializer(purchase, context={'request': request}).data,
            }
            
            if created_batches:
                response_data["batches"] = created_batches
            
            return Response(response_data, status=status.HTTP_201_CREATED)
        except serializers.ValidationError as exc:
            logger.exception(f"Purchase validation error: {exc.detail}")
            return Response(
                {"success": False, "errors": exc.detail, "message": "Validation failed. Please check your input."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            logger.exception(f"Purchase record creation failed: {str(e)}")
            import traceback
            error_trace = traceback.format_exc()
            logger.error(f"Full traceback: {error_trace}")
            return Response(
                {
                    "success": False,
                    "message": f"Unable to add purchase record at the moment. Error: {str(e)}",
                    "error_details": str(e) if settings.DEBUG else None,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        """
        Get all purchase records for a specific supplier, or all purchases for the authenticated seller's suppliers if supplier_id is not provided.
        """
        supplier_id = request.query_params.get("supplier_id")

        try:
            # Get all suppliers for the authenticated seller
            seller_suppliers = Supplier.objects.filter(seller_user=request.user)
            seller_supplier_ids = list(seller_suppliers.values_list('supplier_id', flat=True))
            
            if supplier_id:
                # Verify supplier exists and belongs to the seller
                try:
                    supplier = Supplier.objects.get(supplier_id=supplier_id, seller_user=request.user)
                except Supplier.DoesNotExist:
                    return Response(
                        {"success": False, "message": "Supplier not found or you don't have access to it."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                purchases = (
                    ProductPurchase.objects.filter(supplier_id=supplier_id)
                    .order_by("-invoice_date", "-created_at")
                )
                data = ProductPurchaseSerializer(purchases, many=True, context={'request': request}).data
                return Response(
                    {
                        "success": True,
                        "supplier_id": supplier_id,
                        "supplier_name": supplier.supplier_name,
                        "count": len(data),
                        "data": data,
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                # Return all purchases for suppliers belonging to the authenticated seller
                purchases = (
                    ProductPurchase.objects.filter(supplier_id__in=seller_supplier_ids)
                    .order_by("-invoice_date", "-created_at")
                )
                data = ProductPurchaseSerializer(purchases, many=True, context={'request': request}).data
                return Response(
                    {
                        "success": True,
                        "count": len(data),
                        "data": data,
                    },
                    status=status.HTTP_200_OK,
                )
        except Supplier.DoesNotExist:
            return Response(
                {"success": False, "message": "Supplier not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Failed to fetch purchase history")
            return Response(
                {
                    "success": False,
                    "message": "Unable to fetch purchase history at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def patch(self, request):
        """
        Edit a purchase item (batch and quantity).
        Updates the associated batch quantities accordingly.
        
        Per API Documentation:
        - batch_id: Optional - New batch ID (changes batch assignment)
          If provided and same as current batch_id, syncs batch_name from batch table
        - quantity: Optional - New quantity (updates batch available quantity)
        - purchase_price, selling_price, mrp, gst: Optional - Update pricing fields
        
        Request body:
        {
            "item_id": 123,
            "batch_id": 456,  # Optional - change batch assignment or sync batch_name if same
            "quantity": 10,  # Optional - change quantity
            "purchase_price": 100.00,  # Optional
            "selling_price": 120.00,  # Optional
            "mrp": 150.00,  # Optional
            "gst": 18.00  # Optional
        }
        """
        item_id = request.data.get('item_id')
        if not item_id:
            return Response(
                {"success": False, "message": "item_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            item = ProductPurchaseItem.objects.get(item_id=item_id)
        except ProductPurchaseItem.DoesNotExist:
            return Response(
                {"success": False, "message": f"Purchase item with ID {item_id} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as e:
            logger.exception(f"Error fetching purchase item {item_id}")
            return Response(
                {"success": False, "message": f"Error fetching purchase item: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        old_batch = item.batch
        old_quantity = item.quantity
        
        # Track what was updated
        updated_fields = []

        # Handle batch change
        # Check if batch_id was explicitly provided (not None, not empty string)
        # We need to distinguish between "not provided" and "provided but None/0/empty"
        batch_id_provided = 'batch_id' in request.data
        new_batch_id = None  # Initialize to None
        
        if batch_id_provided:
            new_batch_id_raw = request.data.get('batch_id')
            
            # Check if batch_id was provided but is None, empty string, or 0
            if new_batch_id_raw is None or new_batch_id_raw == '':
                return Response(
                    {"success": False, "message": "batch_id must be a valid positive integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            # Convert to int for consistent comparison
            try:
                new_batch_id = int(new_batch_id_raw)
            except (ValueError, TypeError):
                return Response(
                    {"success": False, "message": "batch_id must be a valid integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            # Validate batch_id is positive (0 is not a valid batch_id)
            if new_batch_id <= 0:
                return Response(
                    {"success": False, "message": "batch_id must be a positive integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        
        # Process batch_id if it was provided and valid
        if new_batch_id is not None:
            old_batch_id = old_batch.batch_id if old_batch else None
            
            # Check if batch_id actually changed
            if new_batch_id != old_batch_id:
                # Batch ID changed - assign to new batch
                try:
                    new_batch = Batch.objects.get(batch_id=new_batch_id)
                except Batch.DoesNotExist:
                    return Response(
                        {"success": False, "message": f"Batch with ID {new_batch_id} not found."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                
                # Restore quantity to old batch if exists
                if old_batch:
                    try:
                        old_batch.available_quantity -= old_quantity
                        old_batch.save(update_fields=['available_quantity'])
                    except Exception as e:
                        logger.exception(f"Error updating old batch {old_batch.batch_id} quantity")
                        return Response(
                            {"success": False, "message": f"Error updating old batch quantity: {str(e)}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        )
                
                # Add quantity to new batch
                try:
                    new_batch.available_quantity += item.quantity
                    new_batch.save(update_fields=['available_quantity'])
                except Exception as e:
                    logger.exception(f"Error updating new batch {new_batch_id} quantity")
                    return Response(
                        {"success": False, "message": f"Error updating new batch quantity: {str(e)}"},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
                
                item.batch = new_batch
                item.batch_name = new_batch.batch_name
                updated_fields.append('batch')
            else:
                # Batch ID didn't change - sync batch_name from batch table
                # This handles cases where batch table's batch_name was updated separately
                # We ALWAYS sync batch_name when batch_id is provided, even if it appears unchanged
                # This ensures product_purchase_item.batch_name stays in sync with batch.batch_name
                
                # Ensure we have access to the batch object
                if item.batch:
                    # Item has a batch - refresh it to get latest batch_name from database
                    item.batch.refresh_from_db()
                    item.batch_name = item.batch.batch_name
                    # Always add batch_name to updated_fields when batch_id is provided
                    # This ensures we never get "No valid fields provided" error
                    updated_fields.append('batch_name')
                else:
                    # Item has no batch assigned but batch_id was provided
                    # Try to load the batch from the provided batch_id and assign it
                    try:
                        batch_to_assign = Batch.objects.get(batch_id=new_batch_id)
                        # Assign batch to item
                        item.batch = batch_to_assign
                        item.batch_name = batch_to_assign.batch_name
                        updated_fields.append('batch')
                        updated_fields.append('batch_name')
                    except Batch.DoesNotExist:
                        return Response(
                            {"success": False, "message": f"Batch with ID {new_batch_id} not found. Cannot sync batch name."},
                            status=status.HTTP_404_NOT_FOUND,
                        )

        # Handle quantity change
        new_quantity = request.data.get('quantity')
        if new_quantity is not None:
            try:
                new_quantity = int(new_quantity)
            except (ValueError, TypeError):
                return Response(
                    {"success": False, "message": "quantity must be a valid integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            if new_quantity <= 0:
                return Response(
                    {"success": False, "message": "Quantity must be greater than 0."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            if new_quantity != old_quantity:
                quantity_diff = new_quantity - old_quantity
                
                # Update batch quantity
                current_batch = item.batch
                if current_batch:
                    # Check if reducing quantity below what's available
                    if quantity_diff < 0 and current_batch.available_quantity < abs(quantity_diff):
                        return Response(
                            {
                                "success": False,
                                "message": f"Cannot reduce quantity. Some stock has already been sold. Batch available: {current_batch.available_quantity}"
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    
                    try:
                        current_batch.available_quantity += quantity_diff
                        current_batch.save(update_fields=['available_quantity'])
                    except Exception as e:
                        logger.exception(f"Error updating batch {current_batch.batch_id} quantity")
                        return Response(
                            {"success": False, "message": f"Error updating batch quantity: {str(e)}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        )
                
                item.quantity = new_quantity
                updated_fields.append('quantity')
            # Note: If quantity is provided but hasn't changed, we don't add it to updated_fields
            # This is intentional - only actual changes are tracked

        # Handle other optional fields
        if 'purchase_price' in request.data:
            item.purchase_price = request.data['purchase_price']
            updated_fields.append('purchase_price')
        
        if 'selling_price' in request.data:
            item.selling_price = request.data['selling_price']
            updated_fields.append('selling_price')
        
        if 'mrp' in request.data:
            item.mrp = request.data['mrp']
            updated_fields.append('mrp')
        
        if 'gst' in request.data:
            item.gst = request.data['gst']
            updated_fields.append('gst')

        # Check if any fields were actually updated
        if not updated_fields:
            # Provide more descriptive error message with debug info
            provided_fields = []
            field_values = {}
            for k in ['batch_id', 'quantity', 'purchase_price', 'selling_price', 'mrp', 'gst']:
                if k in request.data:
                    provided_fields.append(k)
                    field_values[k] = request.data[k]
            
            error_msg = "No valid fields provided for update."
            if provided_fields:
                error_msg = (
                    f"No valid changes detected. Provided fields: {', '.join(provided_fields)}. "
                    f"Values: {field_values}. "
                    f"All provided values match current values, or no changes were made. "
                    f"Current item batch_id: {old_batch.batch_id if old_batch else None}, quantity: {old_quantity}"
                )
            else:
                error_msg = (
                    "No valid fields provided for update. At least one of: batch_id, quantity, "
                    "purchase_price, selling_price, mrp, or gst must be provided."
                )
            
            return Response(
                {"success": False, "message": error_msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            item.save()
        except Exception as e:
            logger.exception(f"Error saving purchase item {item_id}")
            return Response(
                {"success": False, "message": f"Error saving purchase item: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            response_data = ProductPurchaseItemSerializer(item).data
        except Exception as e:
            logger.exception(f"Error serializing purchase item {item_id}")
            return Response(
                {"success": False, "message": f"Error serializing purchase item: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "success": True,
                "message": f"Purchase item updated successfully. Updated fields: {', '.join(updated_fields)}",
                "data": response_data,
            },
            status=status.HTTP_200_OK,
        )


class SupplierPaymentAPIView(APIView):
    """
    POST: add payment record (seller only)
    GET: get all invoices/purchases with payment status by supplier_id (authenticated users)
    """
    # Note: DRF will use default parsers which include MultiPartParser
    # No need to explicitly set parser_classes - let DRF handle it automatically (same as SupplierPurchaseHistoryAPIView)

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsSeller()]
        return [IsAuthenticated()]

    def post(self, request):
        """
        Add a new payment record (seller only).
        Supports file upload for payment_proof (PDF, JPG, PNG, JPEG).
        EXACT same pattern as SupplierPurchaseHistoryAPIView.post()
        """
        from datetime import datetime
        # Handle multipart/form-data properly - request.data is QueryDict for multipart
        # For QueryDict, use .get() method which returns single values
        # For regular dict, use as-is
        # EXACT same pattern as SupplierPurchaseHistoryAPIView
        try:
            # Check if it's a QueryDict (multipart form data)
            from django.http import QueryDict
            if isinstance(request.data, QueryDict):
                # QueryDict - extract values using .get() to get single values
                data = {
                    'invoice_number': request.data.get('invoice_number'),
                    'product_purchase': request.data.get('product_purchase'),
                    'amount': request.data.get('amount'),
                    'payment_mode': request.data.get('payment_mode'),
                    'payment_date': request.data.get('payment_date'),
                    'note': request.data.get('note', ''),
                }
            else:
                # Regular dict
                data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data) if request.data else {}
        except Exception as e:
            logger.exception(f"Error processing request data: {str(e)}")
            return Response(
                {"success": False, "message": f"Error processing request data: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Handle payment proof file upload (PDF, JPG, PNG, JPEG only)
        # EXACT same pattern as invoice_file in SupplierPurchaseHistoryAPIView
        
        # Debug logging
        logger.info(f"=== PAYMENT FILE UPLOAD DEBUG ===")
        logger.info(f"Request method: {request.method}")
        logger.info(f"Request content_type: {request.content_type}")
        logger.info(f"Request META CONTENT_TYPE: {request.META.get('CONTENT_TYPE', 'NOT SET')}")
        logger.info(f"request.FILES type: {type(request.FILES)}")
        logger.info(f"request.FILES keys: {list(request.FILES.keys())}")
        logger.info(f"request.FILES values: {list(request.FILES.values())}")
        logger.info(f"request.data type: {type(request.data)}")
        logger.info(f"request.data keys: {list(request.data.keys()) if hasattr(request.data, 'keys') else 'N/A'}")
        if 'payment_proof' in request.data:
            logger.info(f"payment_proof in request.data: {request.data.get('payment_proof')} (type: {type(request.data.get('payment_proof'))})")
        logger.info(f"================================")
        
        uploaded_proof = request.FILES.get("payment_proof")
        payment_proof_path = None
        
        # Payment proof is REQUIRED - return error if not provided
        if not uploaded_proof:
            logger.error("ERROR: payment_proof file not found in request.FILES!")
            logger.error(f"Available FILES keys: {list(request.FILES.keys())}")
            logger.error(f"Available data keys: {list(request.data.keys()) if hasattr(request.data, 'keys') else 'N/A'}")
            logger.error(f"Content-Type: {request.META.get('CONTENT_TYPE', 'NOT SET')}")
            return Response(
                {
                    "success": False,
                    "message": "Payment proof file is required. Please upload a file (PDF, JPG, PNG, or JPEG).",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        if uploaded_proof:
            logger.info(f"Payment proof file received: {uploaded_proof.name}, size: {uploaded_proof.size} bytes")
            try:
                # Validate file extension
                allowed_extensions = ['.pdf', '.jpg', '.jpeg', '.png']
                ext = os.path.splitext(uploaded_proof.name)[1].lower()
                if ext not in allowed_extensions:
                    return Response(
                        {"success": False, "message": f"Invalid file format. Only PDF, JPG, PNG, JPEG are allowed. Got: {ext}"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                
                # Build a safe, unique path under proof/ folder
                unique_filename = f"{uuid.uuid4().hex}{ext}"
                filename = f"proof/{unique_filename}"
                
                # Ensure proof directory exists in MEDIA_ROOT
                if settings.MEDIA_ROOT:
                    proof_dir = os.path.join(settings.MEDIA_ROOT, 'proof')
                    if not os.path.exists(proof_dir):
                        os.makedirs(proof_dir, exist_ok=True)
                        logger.info(f"Created proof directory: {proof_dir}")
                    else:
                        logger.info(f"Proof directory already exists: {proof_dir}")
                
                # Save the file (same pattern as invoice_file in SupplierPurchaseHistoryAPIView)
                # Reset file pointer to beginning in case it was read before
                uploaded_proof.seek(0)
                saved_path = default_storage.save(filename, ContentFile(uploaded_proof.read()))
                
                # Log the actual save location for verification
                full_path = os.path.join(settings.MEDIA_ROOT, saved_path) if settings.MEDIA_ROOT else saved_path
                logger.info(f"Payment proof file saved successfully. Relative path: {saved_path}, Full path: {full_path}")
                
                # Verify file exists (same pattern as invoice_file)
                if os.path.exists(full_path):
                    logger.info(f"Payment proof file verified at: {full_path}")
                else:
                    logger.warning(f"Payment proof file not found at expected path: {full_path}")
                
                # Store as media/proof/filename (same pattern as invoice_file: media/invoices/filename)
                # saved_path will be "proof/filename.pdf", stored as "media/proof/filename.pdf"
                payment_proof_path = f"media/{saved_path}"
            except Exception as e:
                logger.exception(f"Error saving payment proof file: {str(e)}")
                return Response(
                    {"success": False, "message": f"Error saving payment proof file: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        
        # Add payment_proof path to data if it was saved (EXACT same pattern as invoice_file)
        if payment_proof_path:
            data["payment_proof"] = payment_proof_path
            logger.info(f"Payment proof file saved. Path: {payment_proof_path}")
        else:
            # payment_proof is optional, so set to empty string if no file (EXACT same pattern as invoice_file)
            data["payment_proof"] = ""
        
        serializer = SupplierPaymentSerializer(data=data, context={'request': request})
        try:
            # Validate serializer and log errors if any
            if not serializer.is_valid():
                logger.error(f"Serializer validation errors: {serializer.errors}")
                logger.error(f"Data that failed validation: {data}")
                return Response(
                    {
                        "success": False,
                        "message": "Validation failed. Please check your input.",
                        "errors": serializer.errors,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            payment = serializer.save()
            
            # Log the saved payment to verify payment_proof was saved
            logger.info(f"Payment saved successfully. ID: {payment.supplier_payment_id}, payment_proof: {payment.payment_proof}")
            return Response(
                {
                    "success": True,
                    "message": "Payment record added successfully.",
                    "supplier_payment_id": payment.supplier_payment_id,
                    "data": SupplierPaymentSerializer(payment, context={'request': request}).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            logger.exception(f"Serializer ValidationError: {exc.detail}")
            return Response(
                {
                    "success": False,
                    "message": "Validation failed. Please check your input.",
                    "errors": exc.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Payment record creation failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to add payment record at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        """
        Get all purchases with payment status for a specific supplier, or all suppliers if supplier_id is not provided.
        If action=history and supplier_id is provided, returns individual payment records (payment history).
        """
        supplier_id = request.query_params.get("supplier_id")
        action = request.query_params.get("action")

        # Handle payment history request
        if action == "history" and supplier_id:
            try:
                supplier = Supplier.objects.get(supplier_id=supplier_id)
                # Fetch all individual payment records for this supplier
                payments = (
                    SupplierPayment.objects.select_related("product_purchase", "product_purchase__supplier")
                    .prefetch_related("product_purchase__items")
                    .filter(product_purchase__supplier_id=supplier_id)
                    .order_by("-payment_date", "-supplier_payment_id")
                )
                
                payment_data = []
                for payment in payments:
                    # Calculate outstanding amount for this invoice after this payment
                    purchase = payment.product_purchase
                    total_amount = 0
                    if hasattr(purchase, "items"):
                        items_qs = purchase.items.all()
                        for item in items_qs:
                            purchase_price = float(item.purchase_price or 0)
                            quantity = float(item.quantity or 0)
                            gst = float(item.gst or 0)
                            line_total = purchase_price * quantity * (1 + gst / 100)
                            total_amount += line_total
                    
                    # Sum all payments made up to and including this payment
                    # Use payment_date and supplier_payment_id to handle same-date payments
                    payments_up_to_this = SupplierPayment.objects.filter(
                        product_purchase_id=purchase.product_purchase_id
                    ).filter(
                        Q(payment_date__lt=payment.payment_date) |
                        (Q(payment_date=payment.payment_date) & Q(supplier_payment_id__lte=payment.supplier_payment_id))
                    ).aggregate(total_paid=Sum("amount"))
                    total_paid_up_to_this = float(payments_up_to_this["total_paid"] or 0)
                    
                    # Outstanding after this payment
                    outstanding_after = max(total_amount - total_paid_up_to_this, 0)
                    
                    serializer = SupplierPaymentSerializer(payment, context={'request': request})
                    payment_record = serializer.data
                    payment_record["outstanding_amount"] = round(outstanding_after, 2)
                    payment_record["total_amount"] = round(total_amount, 2)  # Add total invoice amount
                    payment_data.append(payment_record)
                
                return Response(
                    {
                        "success": True,
                        "supplier_id": supplier_id,
                        "supplier_name": supplier.supplier_name,
                        "count": len(payment_data),
                        "data": payment_data,
                    },
                    status=status.HTTP_200_OK,
                )
            except Supplier.DoesNotExist:
                return Response(
                    {"success": False, "message": "Supplier not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            except Exception:
                logger.exception("Failed to fetch supplier payment history")
                return Response(
                    {
                        "success": False,
                        "message": "Unable to fetch payment history at the moment.",
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # Original logic for purchase-level data
        try:
            if supplier_id:
                # Verify supplier exists
                supplier = Supplier.objects.get(supplier_id=supplier_id)
                # Fetch all purchases for this supplier (with items for amount calculation)
                purchases = (
                    ProductPurchase.objects.select_related("supplier")
                    .prefetch_related("items")
                    .filter(supplier_id=supplier_id)
                    .order_by("-invoice_date", "-created_at")
                )
                # Build response with payment status and per-invoice totals
                data = []
                for purchase in purchases:
                    # Sum all payments done for this purchase
                    payment_agg = SupplierPayment.objects.filter(
                        product_purchase_id=purchase.product_purchase_id
                    ).aggregate(total_paid=Sum("amount"))
                    total_paid = float(payment_agg["total_paid"] or 0)
                    # If items exist, get batch_name from first item
                    batch_name = None
                    total_amount = 0
                    if hasattr(purchase, "items"):
                        items_qs = purchase.items.all()
                        if items_qs.exists():
                            # Use first item's batch_name for display
                            batch_name = items_qs.first().batch_name
                            # Calculate total amount from all items
                            for item in items_qs:
                                purchase_price = float(item.purchase_price or 0)
                                quantity = float(item.quantity or 0)
                                gst = float(item.gst or 0)
                                line_total = purchase_price * quantity * (1 + gst / 100)
                                total_amount += line_total

                    # Calculate outstanding amount and payment status
                    outstanding = max(total_amount - total_paid, 0)
                    if total_amount <= 0:
                        payment_status = "pending"
                    elif outstanding <= 0:
                        payment_status = "received"
                    else:
                        payment_status = "partial"
                    purchase_data = {
                        "product_purchase_id": purchase.product_purchase_id,
                        "supplier_id": purchase.supplier.supplier_id,
                        "supplier_name": purchase.supplier.supplier_name,
                        "invoice_number": purchase.invoice_number,
                        "invoice_date": purchase.invoice_date,
                        "batch_name": batch_name,
                        "total_amount": round(total_amount, 2),
                        "paid_amount": round(total_paid, 2),
                        "outstanding_amount": round(outstanding, 2),
                        "payment_status": payment_status,
                    }
                    data.append(purchase_data)
                return Response(
                    {
                        "success": True,
                        "supplier_id": supplier_id,
                        "supplier_name": supplier.supplier_name,
                        "count": len(data),
                        "data": data,
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                # Fetch all purchases for all suppliers (with items for amount calculation)
                purchases = (
                    ProductPurchase.objects.select_related("supplier")
                    .prefetch_related("items")
                    .all()
                    .order_by("-invoice_date", "-created_at")
                )
                data = []
                for purchase in purchases:
                    payment_agg = SupplierPayment.objects.filter(
                        product_purchase_id=purchase.product_purchase_id
                    ).aggregate(total_paid=Sum("amount"))
                    total_paid = float(payment_agg["total_paid"] or 0)
                    batch_name = None
                    total_amount = 0
                    if hasattr(purchase, "items"):
                        items_qs = purchase.items.all()
                        if items_qs.exists():
                            batch_name = items_qs.first().batch_name
                            for item in items_qs:
                                purchase_price = float(item.purchase_price or 0)
                                quantity = float(item.quantity or 0)
                                gst = float(item.gst or 0)
                                line_total = purchase_price * quantity * (1 + gst / 100)
                                total_amount += line_total

                    outstanding = max(total_amount - total_paid, 0)
                    if total_amount <= 0:
                        payment_status = "pending"
                    elif outstanding <= 0:
                        payment_status = "received"
                    else:
                        payment_status = "partial"
                    purchase_data = {
                        "product_purchase_id": purchase.product_purchase_id,
                        "supplier_id": purchase.supplier.supplier_id,
                        "supplier_name": purchase.supplier.supplier_name,
                        "invoice_number": purchase.invoice_number,
                        "invoice_date": purchase.invoice_date,
                        "batch_name": batch_name,
                        "total_amount": round(total_amount, 2),
                        "paid_amount": round(total_paid, 2),
                        "outstanding_amount": round(outstanding, 2),
                        "payment_status": payment_status,
                    }
                    data.append(purchase_data)
                return Response(
                    {
                        "success": True,
                        "count": len(data),
                        "data": data,
                    },
                    status=status.HTTP_200_OK,
                )
        except Supplier.DoesNotExist:
            return Response(
                {"success": False, "message": "Supplier not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Failed to fetch supplier payment data")
            return Response(
                {
                    "success": False,
                    "message": "Unable to fetch payment data at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

class SupplierOutstandingAPIView(APIView):
    """
    GET: Show outstanding for suppliers (can filter by supplier_id)
    Response: supplier_id, supplier_name, total_amount, paid_amount, outstanding
    Only shows suppliers belonging to the logged-in seller.
    
    When action=invoices is provided with supplier_id, returns all invoice details
    with payment status (paid/unpaid) for that supplier.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        supplier_id = request.query_params.get("supplier_id")
        action = request.query_params.get("action")

        # If action=invoices, return invoice details for a specific supplier
        if action == "invoices" and supplier_id:
            try:
                # Verify supplier exists and belongs to the seller
                supplier = Supplier.objects.filter(seller_user=request.user, supplier_id=supplier_id).first()
                if not supplier:
                    return Response(
                        {"success": False, "message": "Supplier not found or you don't have access to it."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                
                # Get all purchases for this supplier
                purchases = ProductPurchase.objects.filter(supplier_id=supplier_id).order_by("-invoice_date", "-created_at")
                
                invoices_data = []
                for purchase in purchases:
                    # Calculate total invoice amount
                    total_amount = 0
                    items_qs = purchase.items.all()
                    for item in items_qs:
                        purchase_price = float(item.purchase_price or 0)
                        quantity = float(item.quantity or 0)
                        gst = float(item.gst or 0)
                        line_total = purchase_price * quantity * (1 + gst / 100)
                        total_amount += line_total
                    
                    # Get payment details for this invoice
                    payments = SupplierPayment.objects.filter(
                        product_purchase_id=purchase.product_purchase_id
                    )
                    payment_agg = payments.aggregate(total_paid=Sum("amount"))
                    paid_amount = float(payment_agg["total_paid"] or 0)
                    
                    # Get the latest payment details for mode_of_payment, proof_uploaded, note
                    latest_payment = payments.order_by("-payment_date", "-supplier_payment_id").first()
                    payment_mode = latest_payment.payment_mode if latest_payment else None
                    proof_uploaded = "yes" if (latest_payment and latest_payment.payment_proof) else "no"
                    payment_note = latest_payment.note if latest_payment else None
                    
                    # Calculate outstanding and determine payment status
                    outstanding = max(total_amount - paid_amount, 0)
                    if total_amount <= 0:
                        payment_status = "unpaid"
                    elif outstanding <= 0:
                        payment_status = "paid"
                    elif paid_amount > 0:
                        payment_status = "partial"
                    else:
                        payment_status = "unpaid"
                    
                    # Build invoice file URL
                    invoice_file_url = None
                    if purchase.invoice_file:
                        scheme = request.scheme
                        host = request.get_host()
                        invoice_file_url = f"{scheme}://{host}{purchase.invoice_file}"
                    
                    # Build payment proof URL
                    payment_proof_url = None
                    if latest_payment and latest_payment.payment_proof:
                        scheme = request.scheme
                        host = request.get_host()
                        payment_proof_url = f"{scheme}://{host}{latest_payment.payment_proof}"
                    
                    invoices_data.append({
                        "invoice_id": purchase.product_purchase_id,
                        "invoice_number": purchase.invoice_number,
                        "invoice_date": purchase.invoice_date,
                        "invoice_file": purchase.invoice_file,
                        "invoice_file_url": invoice_file_url,
                        "invoice_amount": round(total_amount, 2),
                        "paid_amount": round(paid_amount, 2),
                        "outstanding_amount": round(outstanding, 2),
                        "payment_status": payment_status,
                        "payment_mode": payment_mode,
                        "proof_uploaded": proof_uploaded,
                        "payment_proof_url": payment_proof_url,
                        "note": payment_note,
                        "created_at": purchase.created_at,
                    })
                
                return Response({
                    "success": True,
                    "supplier_id": supplier.supplier_id,
                    "supplier_name": supplier.supplier_name,
                    "count": len(invoices_data),
                    "invoices": invoices_data,
                }, status=status.HTTP_200_OK)
                
            except Exception as e:
                logger.exception("Failed to fetch supplier invoices")
                return Response(
                    {"success": False, "message": f"Unable to fetch invoice data: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # Default behavior: Show supplier-level outstanding summary
        # Build supplier queryset - filter by logged-in seller
        suppliers = Supplier.objects.filter(seller_user=request.user)
        if supplier_id:
            suppliers = suppliers.filter(supplier_id=supplier_id)

        result = []
        for supplier in suppliers:
            # Get all purchases for this supplier
            purchases = supplier.purchases.all()
            # Sum total amount from all purchase items (including GST)
            total_amount = 0
            for purchase in purchases:
                items = purchase.items.all()
                for item in items:
                    purchase_price = float(item.purchase_price or 0)
                    quantity = float(item.quantity or 0)
                    gst = float(item.gst or 0)
                    line_total = purchase_price * quantity * (1 + gst / 100)
                    total_amount += line_total

            # Sum paid amount from all payments for this supplier
            paid_amount = SupplierPayment.objects.filter(
                product_purchase__supplier=supplier
            ).aggregate(total_paid=Sum('amount'))['total_paid'] or 0

            outstanding = max(total_amount - float(paid_amount), 0)

            result.append({
                "supplier_id": supplier.supplier_id,
                "supplier_name": supplier.supplier_name,
                "total_amount": round(total_amount, 2),
                "paid_amount": round(float(paid_amount), 2),
                "outstanding": round(outstanding, 2),
            })

        return Response({
            "success": True,
            "count": len(result),
            "data": result,
        }, status=status.HTTP_200_OK)
    
class ProductDetailAPIView(APIView):
    """
    GET: Fetch product details by product_id (for dropdown autofill)
    """
    def get(self, request):
        product_id = request.query_params.get("product_id")
        if not product_id:
            return Response({"success": False, "message": "product_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            product = Product.objects.get(product_id=product_id)
            data = ProductSerializer(product).data
            # Only return relevant fields for dropdown autofill
            filtered = {
                "product_id": data["product_id"],
                "product_name": data["product_name"],
                "purchase_price": data["purchase_price"],
                "sale_price": data["sale_price"],
                "gst": data["gst"],
                "mrp": data["mrp"],
                "barcode": data["barcode"],
                "unit": data["unit"],
            }
            return Response({"success": True, "data": filtered}, status=status.HTTP_200_OK)
        except Product.DoesNotExist:
            return Response({"success": False, "message": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception:
            logger.exception("Failed to fetch product details")
            return Response({"success": False, "message": "Unable to fetch product details."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProductBatchesAPIView(APIView):
    """
    GET: Get available batches for a specific product for selection (e.g., during cart operations).
    
    Query params:
        - product_id (required): The product to get batches for
        - min_quantity: Only show batches with at least this quantity available
    
    Returns batches ordered by expiry date (nearest first) for FIFO stock management.
    Only shows active, non-expired batches with available quantity > 0.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        product_id = request.query_params.get("product_id")
        min_quantity = request.query_params.get("min_quantity", 1)
        
        if not product_id:
            return Response(
                {"success": False, "message": "product_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            min_quantity = int(min_quantity)
        except ValueError:
            min_quantity = 1
        
        try:
            # Verify product exists
            product = Product.objects.get(product_id=product_id)
            
            # Get available batches
            from django.utils import timezone
            today = timezone.now().date()
            
            batches = Batch.objects.filter(
                product_id=product_id,
                is_active=True,
                available_quantity__gte=min_quantity
            ).filter(
                Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
            ).select_related('supplier').order_by(
                F('expiry_date').asc(nulls_last=True)
            )
            
            data = []
            for batch in batches:
                data.append({
                    "batch_id": batch.batch_id,
                    "batch_name": batch.batch_name,
                    "supplier_id": batch.supplier_id,
                    "supplier_name": batch.supplier.supplier_name if batch.supplier else None,
                    "expiry_date": batch.expiry_date,
                    "days_until_expiry": batch.days_until_expiry,
                    "available_quantity": batch.available_quantity,
                    "selling_price": batch.selling_price,
                    "mrp": batch.mrp,
                })
            
            return Response(
                {
                    "success": True,
                    "product_id": product.product_id,
                    "product_name": product.product_name,
                    "count": len(data),
                    "data": data,
                },
                status=status.HTTP_200_OK,
            )
        except Product.DoesNotExist:
            return Response(
                {"success": False, "message": "Product not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Failed to fetch product batches")
            return Response(
                {"success": False, "message": "Unable to fetch product batches at the moment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class StockOutBatchesAPIView(APIView):
    """
    GET: Get all batches that have been stocked in for Stock Out operations for a specific product.
    
    This API is specifically designed for Stock Out modal to populate batch dropdown.
    Returns all batches related to the selected product that have been stocked in (available_quantity > 0).
    
    Query params:
        - product_id (required): The product to get batches for
    
    Returns:
        - batch_id: Batch ID
        - batch_name: Batch name
        - available_quantity: Available quantity in the batch
        - expiry_date: Expiry date of the batch (if available)
    
    Returns all batches with available_quantity > 0, regardless of expiry status.
    Batches are ordered by expiry date (expired first, then by nearest expiry).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        product_id = request.query_params.get("product_id")
        
        if not product_id:
            return Response(
                {"success": False, "message": "product_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            product_id_int = int(product_id)
        except (ValueError, TypeError):
            return Response(
                {"success": False, "message": "Invalid product_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            # Verify product exists
            product = Product.objects.get(product_id=product_id_int)
            
            # Get all batches with available quantity > 0 (all batches that have been stocked in)
            # Order by expiry_date ascending (expired batches first), then by batch_name
            # Null expiry dates will be sorted last
            batches = Batch.objects.filter(
                product_id=product_id_int,
                is_active=True,
                available_quantity__gt=0
            ).order_by('expiry_date', 'batch_name')
            
            data = []
            for batch in batches:
                data.append({
                    "batch_id": batch.batch_id,
                    "batch_name": batch.batch_name,
                    "available_quantity": float(batch.available_quantity),
                    "expiry_date": batch.expiry_date.isoformat() if batch.expiry_date else None,
                })
            
            return Response(
                {
                    "success": True,
                    "product_id": product.product_id,
                    "product_name": product.product_name,
                    "count": len(data),
                    "data": data,
                },
                status=status.HTTP_200_OK,
            )
        except Product.DoesNotExist:
            return Response(
                {"success": False, "message": "Product not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Failed to fetch stock out batches")
            return Response(
                {"success": False, "message": "Unable to fetch stock out batches at the moment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class InventoryBatchSummaryAPIView(APIView):
    """
    GET: Get batch-wise inventory summary with detailed stock information.
    
    Query params:
        - product_id: Filter by specific product
        - supplier_id: Filter by specific supplier
        - seller_user: Filter by seller
        - near_expiry_days: Show batches expiring within X days (default: 30)
        - low_stock_threshold: Consider batches with quantity below this as low stock (default: 10)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        product_id = request.query_params.get("product_id")
        supplier_id = request.query_params.get("supplier_id")
        seller_user_id = request.query_params.get("seller_user") or request.user.id
        near_expiry_days = int(request.query_params.get("near_expiry_days", 30))
        low_stock_threshold = int(request.query_params.get("low_stock_threshold", 10))

        from django.utils import timezone
        today = timezone.now().date()
        near_expiry_date = today + timezone.timedelta(days=near_expiry_days)

        # Build queryset
        batches = Batch.objects.filter(
            product__seller_user_id=seller_user_id,
            is_active=True
        ).select_related('product', 'supplier')

        if product_id:
            batches = batches.filter(product_id=product_id)
        if supplier_id:
            batches = batches.filter(supplier_id=supplier_id)

        # Order by expiry date
        batches = batches.order_by(F('expiry_date').asc(nulls_last=True))

        summary = {
            "total_batches": 0,
            "total_quantity": 0,
            "expired_batches": 0,
            "near_expiry_batches": 0,
            "low_stock_batches": 0,
            "out_of_stock_batches": 0,
        }

        data = []
        for batch in batches:
            is_expired = batch.is_expired
            is_near_expiry = batch.expiry_date and batch.expiry_date <= near_expiry_date and not is_expired
            is_low_stock = batch.available_quantity < low_stock_threshold and batch.available_quantity > 0
            is_out_of_stock = batch.available_quantity <= 0

            summary["total_batches"] += 1
            summary["total_quantity"] += batch.available_quantity

            if is_expired:
                summary["expired_batches"] += 1
            if is_near_expiry:
                summary["near_expiry_batches"] += 1
            if is_low_stock:
                summary["low_stock_batches"] += 1
            if is_out_of_stock:
                summary["out_of_stock_batches"] += 1

            data.append({
                "batch_id": batch.batch_id,
                "batch_name": batch.batch_name,
                "product_id": batch.product_id,
                "product_name": batch.product.product_name,
                "supplier_id": batch.supplier_id,
                "supplier_name": batch.supplier.supplier_name if batch.supplier else None,
                "expiry_date": batch.expiry_date,
                "days_until_expiry": batch.days_until_expiry,
                "initial_quantity": batch.initial_quantity,
                "available_quantity": batch.available_quantity,
                "selling_price": batch.selling_price,
                "is_expired": is_expired,
                "is_near_expiry": is_near_expiry,
                "is_low_stock": is_low_stock,
                "is_out_of_stock": is_out_of_stock,
            })

        return Response({
            "success": True,
            "summary": summary,
            "count": len(data),
            "data": data,
        }, status=status.HTTP_200_OK)


class NearExpiryBatchesAPIView(APIView):
    """
    GET: Get batches that are expiring soon for proactive stock management.
    
    Query params:
        - days: Number of days to look ahead (default: 30)
        - seller_user: Filter by seller
        - product_id: Filter by specific product
        - in_stock_only: Only show batches with available quantity > 0 (default: True)
    
    Returns batches ordered by expiry date (most urgent first).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        days = int(request.query_params.get("days", 30))
        seller_user_id = request.query_params.get("seller_user") or request.user.id
        product_id = request.query_params.get("product_id")
        in_stock_only = request.query_params.get("in_stock_only", "true").lower() == "true"

        from django.utils import timezone
        today = timezone.now().date()
        expiry_threshold = today + timezone.timedelta(days=days)

        batches = Batch.objects.filter(
            product__seller_user_id=seller_user_id,
            is_active=True,
            expiry_date__isnull=False,
            expiry_date__lte=expiry_threshold
        ).select_related('product', 'supplier')

        if product_id:
            batches = batches.filter(product_id=product_id)
        
        if in_stock_only:
            batches = batches.filter(available_quantity__gt=0)

        batches = batches.order_by('expiry_date')

        data = []
        for batch in batches:
            urgency = "expired" if batch.is_expired else ("critical" if batch.days_until_expiry <= 7 else ("warning" if batch.days_until_expiry <= 14 else "normal"))
            
            data.append({
                "batch_id": batch.batch_id,
                "batch_name": batch.batch_name,
                "product_id": batch.product_id,
                "product_name": batch.product.product_name,
                "supplier_name": batch.supplier.supplier_name if batch.supplier else None,
                "expiry_date": batch.expiry_date,
                "days_until_expiry": batch.days_until_expiry,
                "available_quantity": batch.available_quantity,
                "selling_price": batch.selling_price,
                "urgency": urgency,
            })

        # Group by urgency for summary
        expired_count = sum(1 for b in data if b["urgency"] == "expired")
        critical_count = sum(1 for b in data if b["urgency"] == "critical")
        warning_count = sum(1 for b in data if b["urgency"] == "warning")

        return Response({
            "success": True,
            "lookup_days": days,
            "summary": {
                "expired": expired_count,
                "critical_7_days": critical_count,
                "warning_14_days": warning_count,
                "total": len(data),
            },
            "count": len(data),
            "data": data,
        }, status=status.HTTP_200_OK)


class BatchAPIView(APIView):
    """
    Full CRUD operations for Batch management.
    
    GET: Get batches with various filters
        - product_id: Filter by product
        - supplier_id: Filter by supplier  
        - batch_id: Get specific batch
        - near_expiry: Show near expiry batches first (default True)
        - include_expired: Include expired batches (default False)
        - in_stock_only: Only show batches with available quantity > 0 (default False)
    
    POST: Create a new batch
    
    PATCH: Update an existing batch
        - batch_id (required): The batch to update
    
    DELETE: Delete a batch
        - batch_id (required): The batch to delete
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get batches with various filters. Orders by expiry date (nearest first) by default."""
        product_id = request.query_params.get("product_id")
        supplier_id = request.query_params.get("supplier_id")
        batch_id = request.query_params.get("batch_id")
        near_expiry = request.query_params.get("near_expiry", "true").lower() == "true"
        include_expired = request.query_params.get("include_expired", "false").lower() == "true"
        in_stock_only = request.query_params.get("in_stock_only", "false").lower() == "true"
        seller_user_id = request.query_params.get("seller_user") or request.user.id
        
        try:
            # If specific batch requested
            if batch_id:
                batch = Batch.objects.select_related('product', 'supplier').get(batch_id=batch_id)
                return Response(
                    {
                        "success": True,
                        "data": BatchSerializer(batch).data,
                    },
                    status=status.HTTP_200_OK,
                )
            
            # Build queryset
            batches = Batch.objects.select_related('product', 'supplier').filter(
                product__seller_user_id=seller_user_id,
                is_active=True
            )
            
            if product_id:
                batches = batches.filter(product_id=product_id)
            
            if supplier_id:
                batches = batches.filter(supplier_id=supplier_id)
            
            # Filter expired batches
            if not include_expired:
                from django.utils import timezone
                today = timezone.now().date()
                batches = batches.filter(
                    Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
                )
            
            # Filter for in-stock only
            if in_stock_only:
                batches = batches.filter(available_quantity__gt=0)
            
            # Exclude batches that have been used in Stock In operations
            # This ensures batches used in Stock In don't appear in the dropdown
            # Only exclude if product_id is provided (i.e., when fetching batches for Stock In dropdown)
            if product_id:
                # Exclude batches that have InventoryTransaction records with transaction_type="in"
                used_batch_ids = InventoryTransaction.objects.filter(
                    batch__isnull=False,
                    transaction_type=InventoryTransaction.TRANSACTION_TYPE_IN,
                    product_id=product_id
                ).values_list('batch_id', flat=True).distinct()
                
                # Exclude used batches from the queryset
                if used_batch_ids:
                    batches = batches.exclude(batch_id__in=used_batch_ids)
            
            # Order by expiry date (nearest first) - null expiry dates go last
            if near_expiry:
                batches = batches.order_by(
                    F('expiry_date').asc(nulls_last=True),
                    '-created_at'
                )
            else:
                batches = batches.order_by('-created_at')
            
            data = BatchListSerializer(batches, many=True).data
            
            return Response(
                {
                    "success": True,
                    "count": len(data),
                    "data": data,
                },
                status=status.HTTP_200_OK,
            )
        except Batch.DoesNotExist:
            return Response(
                {"success": False, "message": "Batch not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Failed to fetch batches")
            return Response(
                {"success": False, "message": "Unable to fetch batches at the moment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def post(self, request):
        """Create a new batch."""
        data = request.data.copy()
        
        # Validate required fields
        if not data.get('product'):
            return Response(
                {"success": False, "message": "product is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not data.get('supplier'):
            return Response(
                {"success": False, "message": "supplier is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not data.get('batch_name'):
            return Response(
                {"success": False, "message": "batch_name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # Check if batch with same name already exists for this product and supplier
        existing_batch = Batch.objects.filter(
            batch_name__iexact=data.get('batch_name'),
            product_id=data.get('product'),
            supplier_id=data.get('supplier')
        ).first()
        
        if existing_batch:
            return Response(
                {
                    "success": False,
                    "message": f"Batch '{existing_batch.batch_name}' already exists for this product and supplier.",
                    "batch_id": existing_batch.batch_id,
                    "data": BatchSerializer(existing_batch).data,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        serializer = BatchSerializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
            batch = serializer.save()
            return Response(
                {
                    "success": True,
                    "message": "Batch created successfully.",
                    "batch_id": batch.batch_id,
                    "data": BatchSerializer(batch).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Batch creation failed")
            return Response(
                {"success": False, "message": "Unable to create batch at the moment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def patch(self, request):
        """Update an existing batch."""
        batch_id = request.query_params.get("batch_id")
        
        if not batch_id:
            return Response(
                {"success": False, "message": "batch_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            batch = Batch.objects.get(batch_id=batch_id)
            
            # Prevent changing product and supplier
            data = request.data.copy()
            data.pop("product", None)
            data.pop("supplier", None)
            
            serializer = BatchSerializer(batch, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_batch = serializer.save()
            
            return Response(
                {
                    "success": True,
                    "message": "Batch updated successfully.",
                    "data": BatchSerializer(updated_batch).data,
                },
                status=status.HTTP_200_OK,
            )
        except Batch.DoesNotExist:
            return Response(
                {"success": False, "message": "Batch not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Batch update failed")
            return Response(
                {"success": False, "message": "Unable to update batch at the moment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def delete(self, request):
        """Delete (deactivate) a batch."""
        batch_id = request.query_params.get("batch_id")
        
        if not batch_id:
            return Response(
                {"success": False, "message": "batch_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            batch = Batch.objects.get(batch_id=batch_id)
            batch_name = batch.batch_name
            
            # Soft delete - deactivate the batch
            batch.is_active = False
            batch.save(update_fields=['is_active'])
            
            return Response(
                {
                    "success": True,
                    "message": f"Batch '{batch_name}' deleted successfully.",
                },
                status=status.HTTP_200_OK,
            )
        except Batch.DoesNotExist:
            return Response(
                {"success": False, "message": "Batch not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Batch deletion failed")
            return Response(
                {"success": False, "message": "Unable to delete batch at the moment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

class InventorySummaryAPIView(APIView):
    """
    GET: Get current inventory summary for all products with batch-wise details
    
    Query params:
        - product_id: Filter by specific product
        - seller_user: Filter by seller
        - batch_wise: If 'true', return batch-wise breakdown (default: false)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Optionally filter by product or seller_user
        product_id = request.query_params.get("product_id")
        seller_user_id = request.query_params.get("seller_user")
        batch_wise = request.query_params.get("batch_wise", "false").lower() == "true"

        # Get all products (optionally filtered)
        products = Product.objects.all()
        if product_id:
            products = products.filter(product_id=product_id)
        if seller_user_id:
            products = products.filter(seller_user_id=seller_user_id)

        result = []
        for product in products:
            # Get stock in from inventory transactions
            stock_in = InventoryTransaction.objects.filter(
                product=product,
                transaction_type=InventoryTransaction.TRANSACTION_TYPE_IN
            ).aggregate(total=Sum("quantity"))["total"] or 0

            # Get stock out from inventory transactions
            stock_out = InventoryTransaction.objects.filter(
                product=product,
                transaction_type=InventoryTransaction.TRANSACTION_TYPE_OUT
            ).aggregate(total=Sum("quantity"))["total"] or 0

            # Get quantity sold to customers from customer purchases
            sold_to_customer = CustomerPurchaseItem.objects.filter(
                product=product
            ).aggregate(total=Sum("quantity"))["total"] or 0

            # Available stock = stock_in - stock_out - sold_to_customer
            # Ensure available_stock never goes below 0
            available_stock = max(0, stock_in - stock_out - sold_to_customer)

            product_data = {
                "product_id": product.product_id,
                "product_name": product.product_name,
                "category": product.category,
                "brand": product.brand,
                "unit": product.unit,
                "stock_in": stock_in,
                "stock_out": stock_out,
                "sold_to_customer": sold_to_customer,
                "available_stock": available_stock,
                "minimum_stock_alert": product.minimum_stock_alert,
                "is_low_stock": available_stock <= (product.minimum_stock_alert or 0) if product.minimum_stock_alert else False,
            }
            
            # Add batch-wise breakdown if requested
            if batch_wise:
                batches = Batch.objects.filter(
                    product=product,
                    is_active=True
                ).order_by(F('expiry_date').asc(nulls_last=True))
                
                batch_data = []
                for batch in batches:
                    batch_data.append({
                        "batch_id": batch.batch_id,
                        "batch_name": batch.batch_name,
                        "supplier_name": batch.supplier.supplier_name if batch.supplier else None,
                        "expiry_date": batch.expiry_date,
                        "available_quantity": batch.available_quantity,
                        "initial_quantity": batch.initial_quantity,
                        "purchase_price": batch.purchase_price,
                        "selling_price": batch.selling_price,
                        "mrp": batch.mrp,
                        "is_expired": batch.is_expired,
                        "days_until_expiry": batch.days_until_expiry,
                        "created_at": batch.created_at,
                    })
                product_data["batches"] = batch_data
                product_data["batch_count"] = len(batch_data)
            
            result.append(product_data)

        return Response({
            "success": True,
            "count": len(result),
            "data": result,
        }, status=status.HTTP_200_OK)

class InventoryStockInAPIView(APIView):
    """
    POST: Add stock in transaction (add to inventory)
    
    If batch_id is provided, updates the batch's available_quantity.
    If batch_name is provided without batch_id, creates a new batch or updates existing.
    Also updates the product's minimum_stock_alert field.
    
    Now accepts expiry_date, manufacturing_date, and category fields.
    If category is provided, updates the product's category.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data.copy()
        data["transaction_type"] = InventoryTransaction.TRANSACTION_TYPE_IN
        
        batch_id = data.get("batch")
        batch_name = data.get("batch_name", "")
        product_id = data.get("product")
        quantity = int(data.get("quantity", 0))
        category = data.get("category")
        expiry_date = data.get("expiry_date")
        manufacturing_date = data.get("manufacturing_date")
        
        batch = None
        batch_updated = False
        
        # Handle batch - either by batch_id or batch_name
        if batch_id:
            # Handle batch by batch_id
            try:
                batch = Batch.objects.get(batch_id=batch_id)
                # Set batch_id in data so transaction links to batch
                data["batch"] = batch.batch_id
                data["batch_name"] = batch.batch_name
                
                # Update batch expiry_date if provided
                # Note: Do NOT update available_quantity for Stock In operations
                if expiry_date:
                    batch.expiry_date = expiry_date
                
                # Update batch manufacturing_date if provided
                if manufacturing_date:
                    batch.manufacturing_date = manufacturing_date
                
                # Save batch with updated dates only (not available_quantity)
                update_fields = []
                if expiry_date:
                    update_fields.append('expiry_date')
                if manufacturing_date:
                    update_fields.append('manufacturing_date')
                
                # Only save if there are fields to update
                if update_fields:
                    batch.save(update_fields=update_fields)
                
                batch_updated = True
            except Batch.DoesNotExist:
                pass
        elif batch_name and batch_name.strip() and product_id:
            # Handle batch by batch_name - find existing batch
            try:
                # Try to find existing batch by name and product
                batch = Batch.objects.get(
                    batch_name=batch_name.strip(),
                    product_id=product_id
                )
                # Set batch_id in data so transaction links to batch
                data["batch"] = batch.batch_id
                
                # Update batch expiry_date if provided
                # Note: Do NOT update available_quantity for Stock In operations
                if expiry_date:
                    batch.expiry_date = expiry_date
                
                # Update batch manufacturing_date if provided
                if manufacturing_date:
                    batch.manufacturing_date = manufacturing_date
                
                # Save batch with updated dates only (not available_quantity)
                update_fields = []
                if expiry_date:
                    update_fields.append('expiry_date')
                if manufacturing_date:
                    update_fields.append('manufacturing_date')
                
                # Only save if there are fields to update
                if update_fields:
                    batch.save(update_fields=update_fields)
                
                batch_updated = True
            except Batch.DoesNotExist:
                # Batch not found by name - cannot create without supplier
                # batch_name will be stored in transaction but batch won't be created
                pass
        
        serializer = InventoryTransactionSerializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
            transaction = serializer.save()
            
            # After transaction is saved, ensure batch dates are updated if transaction is linked to a batch
            # This ensures dates are always updated even if batch wasn't found before save
            if transaction.batch and (expiry_date or manufacturing_date):
                # Use the batch from transaction if we don't have one yet
                if not batch:
                    batch = transaction.batch
                
                # Refresh batch from database to get latest state
                batch.refresh_from_db()
                needs_update = False
                
                # Update batch expiry_date if provided and different
                if expiry_date and batch.expiry_date != expiry_date:
                    batch.expiry_date = expiry_date
                    needs_update = True
                
                # Update batch manufacturing_date if provided and different
                if manufacturing_date and batch.manufacturing_date != manufacturing_date:
                    batch.manufacturing_date = manufacturing_date
                    needs_update = True
                
                # Save batch with updated dates if any changes are needed
                if needs_update:
                    update_fields = []
                    if expiry_date:
                        update_fields.append('expiry_date')
                    if manufacturing_date:
                        update_fields.append('manufacturing_date')
                    batch.save(update_fields=update_fields)
                    batch_updated = True
            
            # Update product's minimum_stock_alert by adding the stock in quantity
            product = transaction.product
            current_alert = product.minimum_stock_alert or 0
            product.minimum_stock_alert = current_alert + transaction.quantity
            
            # Update product category if provided
            if category:
                product.category = category
                product.save(update_fields=['minimum_stock_alert', 'category'])
            else:
                product.save(update_fields=['minimum_stock_alert'])
            
            response_data = {
                "success": True,
                "message": "Stock in recorded successfully.",
                "transaction_id": transaction.transaction_id,
                "minimum_stock_alert": product.minimum_stock_alert,
                "data": InventoryTransactionSerializer(transaction).data,
            }
            
            if category:
                response_data["category_updated"] = category
            
            # Refresh batch from database to get latest dates
            if batch:
                batch.refresh_from_db()
                response_data["batch_updated"] = {
                    "batch_id": batch.batch_id,
                    "batch_name": batch.batch_name,
                    "available_quantity": batch.available_quantity,  # Current quantity (not updated during Stock In)
                    "expiry_date": str(batch.expiry_date) if batch.expiry_date else None,
                    "manufacturing_date": str(batch.manufacturing_date) if batch.manufacturing_date else None,
                }
            
            return Response(response_data, status=status.HTTP_201_CREATED)
        except serializers.ValidationError as exc:
            return Response({"success": False, "errors": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Stock in failed")
            return Response({"success": False, "message": "Unable to record stock in."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class InventoryStockOutAPIView(APIView):
    """
    POST: Add stock out transaction (remove from inventory)
    
    If batch_id is provided, deducts from the batch's available_quantity.
    Validates that batch has sufficient quantity before allowing stock out.
    Also deducts quantity from corresponding ProductPurchaseItem records (Purchase History).
    Uses FIFO (First In First Out) to deduct from oldest purchase items first.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data.copy()
        data["transaction_type"] = InventoryTransaction.TRANSACTION_TYPE_OUT
        
        batch_id = data.get("batch")
        quantity = int(data.get("quantity", 0))
        product_id = data.get("product")
        
        # Validate product_id exists
        if not product_id:
            return Response({
                "success": False,
                "message": "Product ID is required for stock out operation.",
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            product = Product.objects.get(product_id=product_id)
        except Product.DoesNotExist:
            return Response({
                "success": False,
                "message": f"Product with ID {product_id} does not exist.",
            }, status=status.HTTP_400_BAD_REQUEST)
        
        batch = None
        batch_updated = False
        purchase_items_updated = []
        purchase_items_deleted = []
        
        # Handle batch
        if batch_id:
            try:
                batch = Batch.objects.get(batch_id=batch_id)
                # Validate sufficient quantity
                if batch.available_quantity < quantity:
                    return Response({
                        "success": False,
                        "message": f"Insufficient batch quantity. Available: {batch.available_quantity}, Requested: {quantity}",
                    }, status=status.HTTP_400_BAD_REQUEST)
                # Deduct from batch available quantity
                # Ensure batch quantity never goes below 0
                batch.available_quantity = max(0, batch.available_quantity - quantity)
                batch.save(update_fields=['available_quantity'])
                batch_updated = True
                data["batch_name"] = batch.batch_name
                
                # Deduct quantity from Purchase History (ProductPurchaseItem records)
                # Find purchase items that match this batch (by batch_id or batch_name)
                # Use FIFO - oldest purchase items first (order by invoice_date, then created_at)
                purchase_items = ProductPurchaseItem.objects.filter(
                    Q(batch_id=batch_id) | Q(batch_name__iexact=batch.batch_name)
                ).select_related('product_purchase').order_by(
                    'product_purchase__invoice_date',
                    'product_purchase__created_at',
                    'item_id'
                )
                
                remaining_quantity = quantity
                
                for purchase_item in purchase_items:
                    if remaining_quantity <= 0:
                        break
                    
                    # Get current quantity
                    current_qty = purchase_item.quantity
                    
                    if current_qty <= 0:
                        continue
                    
                    # Calculate deduction amount
                    deduction = min(remaining_quantity, current_qty)
                    
                    # Deduct from purchase item
                    purchase_item.quantity = current_qty - deduction
                    
                    if purchase_item.quantity <= 0:
                        # Delete the purchase item if quantity becomes zero
                        purchase_items_deleted.append({
                            "item_id": purchase_item.item_id,
                            "product_purchase_id": purchase_item.product_purchase.product_purchase_id,
                            "invoice_number": purchase_item.product_purchase.invoice_number,
                            "batch_name": purchase_item.batch_name or (batch.batch_name if batch else ''),
                        })
                        purchase_item.delete()
                    else:
                        # Update the purchase item
                        purchase_item.save(update_fields=['quantity'])
                        purchase_items_updated.append({
                            "item_id": purchase_item.item_id,
                            "product_purchase_id": purchase_item.product_purchase.product_purchase_id,
                            "invoice_number": purchase_item.product_purchase.invoice_number,
                            "old_quantity": current_qty,
                            "new_quantity": purchase_item.quantity,
                            "batch_name": purchase_item.batch_name or (batch.batch_name if batch else ''),
                        })
                    
                    remaining_quantity -= deduction
                
                # If there's still remaining quantity after processing all purchase items,
                # it means the batch was partially or fully added through Stock In (not Purchase)
                # This is acceptable - we just log it
                if remaining_quantity > 0:
                    logger.info(f"Stock out quantity {remaining_quantity} could not be matched to purchase items for batch {batch_id}. This may be from Stock In transactions.")
                    
            except Batch.DoesNotExist:
                pass
        
        # Validate product-level available stock only if no batch is selected
        # If a batch is selected, validate against batch's available quantity instead
        # This allows batch Stock Out even when product's overall available stock is 0
        if not batch_id:
            # Calculate current available stock for the product BEFORE processing this Stock Out
            # This prevents Stock Out when available stock is already 0 or would go negative
            stock_in = InventoryTransaction.objects.filter(
                product=product,
                transaction_type=InventoryTransaction.TRANSACTION_TYPE_IN
            ).aggregate(total=Sum("quantity"))["total"] or 0
            
            stock_out = InventoryTransaction.objects.filter(
                product=product,
                transaction_type=InventoryTransaction.TRANSACTION_TYPE_OUT
            ).aggregate(total=Sum("quantity"))["total"] or 0
            
            sold_to_customer = CustomerPurchaseItem.objects.filter(
                product=product
            ).aggregate(total=Sum("quantity"))["total"] or 0
            
            current_available_stock = stock_in - stock_out - sold_to_customer
            
            # Validate that Stock Out won't make available stock negative
            # If current available stock is 0 or less, reject the Stock Out
            # This ensures available_stock never goes negative in the backend
            if current_available_stock <= 0:
                return Response({
                    "success": False,
                    "message": f"Cannot perform Stock Out. Product's available quantity is {max(0, current_available_stock)}. Stock Out operations are not allowed when available quantity is 0 or negative.",
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Validate that Stock Out quantity doesn't exceed available stock
            if quantity > current_available_stock:
                return Response({
                    "success": False,
                    "message": f"Cannot perform Stock Out. Requested quantity ({quantity}) exceeds available stock ({current_available_stock}).",
                }, status=status.HTTP_400_BAD_REQUEST)
        
        serializer = InventoryTransactionSerializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
            transaction = serializer.save()
            
            response_data = {
                "success": True,
                "message": "Stock out recorded successfully.",
                "transaction_id": transaction.transaction_id,
                "data": InventoryTransactionSerializer(transaction).data,
            }
            
            if batch and batch_updated:
                response_data["batch_updated"] = {
                    "batch_id": batch.batch_id,
                    "batch_name": batch.batch_name,
                    "new_available_quantity": batch.available_quantity,
                }
            
            # Include purchase items update information
            if purchase_items_updated or purchase_items_deleted:
                response_data["purchase_items_updated"] = {
                    "updated": purchase_items_updated,
                    "deleted": purchase_items_deleted,
                }
            
            return Response(response_data, status=status.HTTP_201_CREATED)
        except serializers.ValidationError as exc:
            return Response({"success": False, "errors": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Stock out failed")
            return Response({"success": False, "message": "Unable to record stock out."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class InventoryTransactionsAPIView(APIView):
    """
    GET: Get inventory transactions (stock in/out history)
    Supports filtering by product_id and transaction_type ('in' or 'out')
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            product_id = request.query_params.get('product_id')
            transaction_type = request.query_params.get('transaction_type')  # 'in' or 'out'
            
            transactions = InventoryTransaction.objects.all()
            
            # Filter by product_id if provided
            if product_id:
                try:
                    product_id = int(product_id)
                    transactions = transactions.filter(product_id=product_id)
                except (ValueError, TypeError):
                    return Response({
                        "success": False,
                        "message": "Invalid product_id.",
                    }, status=status.HTTP_400_BAD_REQUEST)
            
            # Filter by transaction_type if provided
            if transaction_type:
                if transaction_type not in [InventoryTransaction.TRANSACTION_TYPE_IN, InventoryTransaction.TRANSACTION_TYPE_OUT]:
                    return Response({
                        "success": False,
                        "message": "Invalid transaction_type. Must be 'in' or 'out'.",
                    }, status=status.HTTP_400_BAD_REQUEST)
                transactions = transactions.filter(transaction_type=transaction_type)
            
            # Order by created_at descending (most recent first)
            transactions = transactions.order_by('-created_at')
            
            serializer = InventoryTransactionSerializer(transactions, many=True)
            
            return Response({
                "success": True,
                "count": len(serializer.data),
                "data": serializer.data,
            }, status=status.HTTP_200_OK)
            
        except Exception as exc:
            logger.exception("Error fetching inventory transactions")
            return Response({
                "success": False,
                "message": "Unable to fetch inventory transactions.",
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# customer flow

class SellerProductsAPIView(APIView):
    
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            seller_user = request.user
            
            # Verify user is a seller
            if seller_user.role.role_id != UserRole.ROLE_SELLER:
                return Response(
                    {"success": False, "error": "Only sellers can access this endpoint."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Get all products for this seller
            products = Product.objects.filter(seller_user=seller_user).order_by('-created_at')
            serializer = ProductListSerializer(products, many=True)

            return Response({
                "success": True,
                "count": len(serializer.data),
                "data": serializer.data,
            }, status=status.HTTP_200_OK)

        except Exception as exc:
            logger.error(f"Error fetching seller products: {exc}")
            return Response(
                {"success": False, "error": "Failed to fetch products."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class SellerCustomersAPIView(APIView):
    
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            seller_user = request.user
            
            # Verify user is a seller
            if seller_user.role.role_id != UserRole.ROLE_SELLER:
                return Response(
                    {"success": False, "error": "Only sellers can access this endpoint."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            created_via_bot = request.query_params.get("created_via_bot")
            customers_qs = Customer.objects.filter(seller_user=seller_user)
            if created_via_bot in ("1", "true", "True", "yes", "Yes"):
                customers_qs = customers_qs.filter(created_via_bot=True)
            customers = customers_qs.order_by("full_name")
            serializer = CustomerSerializer(customers, many=True)

            return Response({
                "success": True,
                "count": len(serializer.data),
                "data": serializer.data,
            }, status=status.HTTP_200_OK)

        except Exception as exc:
            logger.error(f"Error fetching seller customers: {exc}")
            return Response(
                {"success": False, "error": "Failed to fetch customers."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def post(self, request):
        """Add a new customer."""
        try:
            seller_user = request.user
            
            # Verify user is a seller
            if seller_user.role.role_id != UserRole.ROLE_SELLER:
                return Response(
                    {"success": False, "error": "Only sellers can create customers."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Add seller_user to request data - pass the User object, not just the ID
            # The serializer will handle the validation
            request_data = request.data.copy()
            request_data['seller_user'] = seller_user.id

            serializer = CustomerSerializer(data=request_data)
            if serializer.is_valid():
                # Save with seller_user object to ensure proper validation
                serializer.save(seller_user=seller_user)
                return Response({
                    "success": True,
                    "message": "Customer created successfully.",
                    "data": serializer.data,
                }, status=status.HTTP_201_CREATED)
            else:
                return Response(
                    {"success": False, "errors": serializer.errors},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Exception as exc:
            logger.error(f"Error creating customer: {exc}")
            import traceback
            logger.error(traceback.format_exc())
            return Response(
                {"success": False, "error": f"Failed to create customer: {str(exc)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def patch(self, request):
        """
        Update an existing customer (partial update).
        
        Query params:
        - customer_id (required): The ID of the customer to update
        
        Body: Fields to update (full_name, email, mobile_number, whatsapp_number, address, gst_number)
        """
        customer_id = request.query_params.get("customer_id")
        
        if not customer_id:
            return Response(
                {"success": False, "message": "customer_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            # Get the customer and verify ownership
            customer = Customer.objects.get(
                customer_id=customer_id,
                seller_user=request.user
            )
            
            # Prevent changing seller_user
            data = request.data.copy()
            data.pop("seller_user", None)
            
            serializer = CustomerSerializer(customer, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_customer = serializer.save()
            
            return Response(
                {
                    "success": True,
                    "message": "Customer updated successfully.",
                    "data": CustomerSerializer(updated_customer).data,
                },
                status=status.HTTP_200_OK,
            )
        except Customer.DoesNotExist:
            return Response(
                {"success": False, "message": "Customer not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Customer update failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to update customer at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CustomerCartAPIView(APIView):
    """
    GET: Get cart items for a customer
    POST: Add product to cart (with optional batch selection)
    PATCH: Update cart item quantity or batch
    DELETE: Remove item from cart
    
    Supports batch-wise cart management. If batch is specified, the item will be
    tracked with that specific batch. Batches are prioritized by nearest expiry date.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get all cart items for a customer."""
        try:
            customer_id = request.query_params.get('customer_id')
            
            if not customer_id:
                return Response(
                    {"success": False, "error": "customer_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            cart_items = CustomerCart.objects.filter(customer_id=customer_id).select_related('customer', 'product', 'batch')
            serializer = CustomerCartSerializer(cart_items, many=True)

            return Response({
                "success": True,
                "count": len(serializer.data),
                "data": serializer.data,
            }, status=status.HTTP_200_OK)

        except Exception as exc:
            logger.error(f"Error fetching cart items: {exc}")
            return Response(
                {"success": False, "error": "Failed to fetch cart items."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def post(self, request):
        """Add a product to cart with optional batch selection."""
        try:
            seller_user = request.user
            
            # Verify user is a seller
            if seller_user.role.role_id != UserRole.ROLE_SELLER:
                return Response(
                    {"success": False, "error": "Only sellers can access this endpoint."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Validate required fields
            customer_id = request.data.get('customer')
            product_id = request.data.get('product')
            quantity = request.data.get('quantity')
            batch_id = request.data.get('batch')  # Optional batch selection
            auto_select_batch = request.data.get('auto_select_batch', True)  # Auto-select nearest expiry batch

            if not customer_id:
                return Response(
                    {"success": False, "error": "customer is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not product_id:
                return Response(
                    {"success": False, "error": "product is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not quantity or int(quantity) <= 0:
                return Response(
                    {"success": False, "error": "quantity must be greater than 0."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Verify customer belongs to seller
            try:
                customer = Customer.objects.get(customer_id=customer_id, seller_user=seller_user)
            except Customer.DoesNotExist:
                return Response(
                    {"success": False, "error": "Customer not found or does not belong to you."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Verify product belongs to seller
            try:
                product = Product.objects.get(product_id=product_id, seller_user=seller_user)
            except Product.DoesNotExist:
                return Response(
                    {"success": False, "error": "Product not found or does not belong to you."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Handle batch selection
            batch = None
            if batch_id:
                try:
                    batch = Batch.objects.get(batch_id=batch_id, product=product)
                    if batch.available_quantity < int(quantity):
                        return Response(
                            {"success": False, "error": f"Insufficient batch quantity. Available: {batch.available_quantity}"},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                except Batch.DoesNotExist:
                    return Response(
                        {"success": False, "error": "Batch not found or does not belong to this product."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
            elif auto_select_batch:
                # Auto-select batch with nearest expiry date that has sufficient quantity
                from django.utils import timezone
                today = timezone.now().date()
                batch = Batch.objects.filter(
                    product=product,
                    is_active=True,
                    available_quantity__gte=int(quantity)
                ).filter(
                    Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
                ).order_by(
                    F('expiry_date').asc(nulls_last=True)
                ).first()

            # Check if product (with same batch) already exists in cart
            cart_lookup = {'customer': customer, 'product': product}
            if batch:
                cart_lookup['batch'] = batch
            else:
                cart_lookup['batch__isnull'] = True
            
            try:
                cart_item = CustomerCart.objects.get(**cart_lookup)
                # Update existing cart item
                cart_item.quantity = int(quantity)
                cart_item.save()
                created = False
            except CustomerCart.DoesNotExist:
                # Create new cart item
                cart_item = CustomerCart.objects.create(
                    customer=customer,
                    product=product,
                    batch=batch,
                    quantity=int(quantity)
                )
                created = True

            serializer = CustomerCartSerializer(cart_item)
            response_data = {
                "success": True,
                "message": "Item added to cart." if created else "Cart item updated.",
                "data": serializer.data,
            }
            
            if batch:
                response_data["batch_selected"] = {
                    "batch_id": batch.batch_id,
                    "batch_name": batch.batch_name,
                    "expiry_date": batch.expiry_date,
                    "available_quantity": batch.available_quantity,
                }
            
            return Response(response_data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

        except Exception as exc:
            logger.error(f"Error adding to cart: {exc}")
            import traceback
            logger.error(traceback.format_exc())
            return Response(
                {"success": False, "error": f"Failed to add item to cart: {str(exc)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def patch(self, request):
        """Update cart item quantity or batch."""
        try:
            cart_id = request.data.get('cart_id')
            quantity = request.data.get('quantity')
            batch_id = request.data.get('batch')

            if not cart_id:
                return Response(
                    {"success": False, "error": "cart_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            cart_item = CustomerCart.objects.get(cart_id=cart_id)
            
            if quantity is not None:
                cart_item.quantity = int(quantity)
            
            if batch_id is not None:
                if batch_id:
                    try:
                        batch = Batch.objects.get(batch_id=batch_id, product=cart_item.product)
                        cart_item.batch = batch
                    except Batch.DoesNotExist:
                        return Response(
                            {"success": False, "error": "Batch not found."},
                            status=status.HTTP_404_NOT_FOUND,
                        )
                else:
                    cart_item.batch = None
            
            cart_item.save()

            serializer = CustomerCartSerializer(cart_item)
            return Response({
                "success": True,
                "message": "Cart item updated.",
                "data": serializer.data,
            }, status=status.HTTP_200_OK)

        except CustomerCart.DoesNotExist:
            return Response(
                {"success": False, "error": "Cart item not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            logger.error(f"Error updating cart item: {exc}")
            return Response(
                {"success": False, "error": "Failed to update cart item."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def delete(self, request):
        """Remove item from cart."""
        try:
            cart_id = request.data.get('cart_id')

            if not cart_id:
                return Response(
                    {"success": False, "error": "cart_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            cart_item = CustomerCart.objects.get(cart_id=cart_id)
            cart_item.delete()

            return Response({
                "success": True,
                "message": "Item removed from cart.",
            }, status=status.HTTP_200_OK)

        except CustomerCart.DoesNotExist:
            return Response(
                {"success": False, "error": "Cart item not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            logger.error(f"Error removing from cart: {exc}")
            return Response(
                {"success": False, "error": "Failed to remove item from cart."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CustomerPurchaseAPIView(APIView):
    """
    POST: Place an order (create purchase from cart items)
    GET: Get purchase history for a customer
    
    Handles batch-wise sales. When an order is placed:
    - Deducts quantity from batch's available_quantity
    - Records batch information in purchase items
    - Prioritizes near-expiry batches for FIFO stock management
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Place an order from cart items with batch-wise stock deduction."""
        try:
            customer_id = request.data.get('customer_id')
            
            if not customer_id:
                return Response(
                    {"success": False, "error": "customer_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            customer = Customer.objects.get(customer_id=customer_id)
            cart_items = CustomerCart.objects.filter(customer_id=customer_id).select_related('product', 'batch')

            if not cart_items.exists():
                return Response(
                    {"success": False, "error": "Cart is empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Validate batch quantities before processing
            insufficient_stock = []
            for cart_item in cart_items:
                if cart_item.batch:
                    if cart_item.batch.available_quantity < cart_item.quantity:
                        insufficient_stock.append({
                            "product": cart_item.product.product_name,
                            "batch": cart_item.batch.batch_name,
                            "available": cart_item.batch.available_quantity,
                            "requested": cart_item.quantity,
                        })
            
            if insufficient_stock:
                return Response(
                    {
                        "success": False,
                        "error": "Insufficient batch quantities.",
                        "insufficient_items": insufficient_stock,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Generate invoice number
            invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"

            # Create purchase record
            purchase = CustomerPurchase.objects.create(
                customer=customer,
                invoice_number=invoice_number,
            )

            # Create purchase items and calculate total
            from decimal import Decimal
            total_amount = Decimal('0')
            batch_updates = []
            
            for cart_item in cart_items:
                product = cart_item.product
                batch = cart_item.batch
                quantity = cart_item.quantity
                
                # Use batch price if available, otherwise use product price
                if batch and batch.selling_price:
                    unit_price = Decimal(str(batch.selling_price))
                else:
                    unit_price = Decimal(str(product.mrp or product.sale_price or 0))
                
                gst_value = Decimal(str(product.gst or 0))

                # Calculate line total: quantity * unit_price * (1 + gst/100)
                line_total = Decimal(str(quantity)) * unit_price * (Decimal('1') + gst_value / Decimal('100'))

                # Create purchase item with batch reference
                CustomerPurchaseItem.objects.create(
                    customer_purchase=purchase,
                    product=product,
                    batch=batch,
                    quantity=quantity,
                    unit_price=unit_price,
                    gst=gst_value,
                    line_total=line_total,
                )

                # Deduct from batch quantity if batch is specified
                if batch:
                    batch.available_quantity = max(0, batch.available_quantity - quantity)
                    batch.save(update_fields=['available_quantity'])
                    batch_updates.append({
                        "batch_id": batch.batch_id,
                        "batch_name": batch.batch_name,
                        "quantity_deducted": quantity,
                        "new_available_quantity": batch.available_quantity,
                    })

                total_amount += line_total

            # Update purchase total
            purchase.total_amount = total_amount
            purchase.save()

            # Clear cart
            cart_items.delete()

            serializer = CustomerPurchaseSerializer(purchase)
            response_data = {
                "success": True,
                "message": "Purchase created successfully.",
                "data": serializer.data,
            }
            
            if batch_updates:
                response_data["batch_updates"] = batch_updates
            
            return Response(response_data, status=status.HTTP_201_CREATED)

        except Customer.DoesNotExist:
            return Response(
                {"success": False, "error": "Customer not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            logger.error(f"Error creating purchase: {exc}")
            return Response(
                {"success": False, "error": "Failed to create purchase."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        """Get purchase history for a customer or all customers."""
        try:
            seller_user = request.user
            customer_id = request.query_params.get('customer_id')
            
            # Build queryset - filter by seller's customers
            if customer_id:
                # Filter by specific customer (must belong to seller)
                purchases = CustomerPurchase.objects.filter(
                    customer_id=customer_id,
                    customer__seller_user=seller_user
                ).prefetch_related('items__product').order_by('-invoice_date')
            else:
                # Return all purchases for seller's customers
                purchases = CustomerPurchase.objects.filter(
                    customer__seller_user=seller_user
                ).prefetch_related('items__product').order_by('-invoice_date')

            serializer = CustomerPurchaseSerializer(purchases, many=True)
            return Response({
                "success": True,
                "count": len(serializer.data),
                "data": serializer.data,
            }, status=status.HTTP_200_OK)

        except Exception as exc:
            logger.error(f"Error fetching purchase history: {exc}")
            return Response(
                {"success": False, "error": "Failed to fetch purchase history."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def delete(self, request):
        """
        Delete a single line item from a customer purchase.

        Expected payload (JSON body or query params):
        - purchase_id: ID of the customer purchase
        - item_id:     ID of the line item to delete

        Effects:
        - Removes the item from the invoice
        - Adds the item's quantity back to the related batch's available_quantity
          (restoring the original stock for that batch)
        - Decreases the purchase.total_amount by the item's line_total
        - Customer outstanding calculations automatically reflect the new total_amount
        """
        from decimal import Decimal
        from django.db import transaction

        try:
            # Support both JSON body and query params
            purchase_id = request.data.get("purchase_id") or request.query_params.get("purchase_id")
            item_id = request.data.get("item_id") or request.query_params.get("item_id")

            if not purchase_id or not item_id:
                return Response(
                    {
                        "success": False,
                        "error": "purchase_id and item_id are required.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                purchase_id = int(purchase_id)
                item_id = int(item_id)
            except (TypeError, ValueError):
                return Response(
                    {
                        "success": False,
                        "error": "purchase_id and item_id must be valid integers.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Ensure the purchase belongs to the logged-in seller's customers
            try:
                purchase = CustomerPurchase.objects.select_related(
                    "customer"
                ).get(
                    purchase_id=purchase_id,
                    customer__seller_user=request.user,
                )
            except CustomerPurchase.DoesNotExist:
                return Response(
                    {
                        "success": False,
                        "error": "Purchase not found or not accessible.",
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

            try:
                item = CustomerPurchaseItem.objects.select_related("batch", "product").get(
                    item_id=item_id,
                    customer_purchase=purchase,
                )
            except CustomerPurchaseItem.DoesNotExist:
                return Response(
                    {
                        "success": False,
                        "error": "Purchase item not found for this invoice.",
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

            with transaction.atomic():
                # Restore batch quantity and reactivate batch (if batch is linked)
                if item.batch_id:
                    Batch.objects.filter(batch_id=item.batch_id).update(
                        available_quantity=F("available_quantity") + item.quantity,
                        is_active=True,
                    )

                # Safely adjust total_amount
                line_total = Decimal(str(item.line_total or 0))
                purchase.refresh_from_db()  # get latest total_amount
                current_total = Decimal(str(purchase.total_amount or 0))
                new_total = current_total - line_total
                if new_total < 0:
                    new_total = Decimal("0")
                purchase.total_amount = new_total
                purchase.save(update_fields=["total_amount"])

                # Finally delete the item
                item.delete()

                # Refresh purchase with items for response
                purchase.refresh_from_db()

            serializer = CustomerPurchaseSerializer(purchase, context={"request": request})
            return Response(
                {
                    "success": True,
                    "message": "Product deleted from invoice successfully.",
                    "data": serializer.data,
                },
                status=status.HTTP_200_OK,
            )

        except Exception as exc:
            logger.error(f"Error deleting customer purchase item: {exc}")
            return Response(
                {
                    "success": False,
                    "error": "Failed to delete product from invoice.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CustomerInvoiceDetailAPIView(APIView):
    """
    GET: Get invoice details by purchase_id or invoice_number
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Fetch invoice details."""
        try:
            purchase_id = request.query_params.get('purchase_id')
            invoice_number = request.query_params.get('invoice_number')

            if not purchase_id and not invoice_number:
                return Response(
                    {"success": False, "error": "purchase_id or invoice_number is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if purchase_id:
                purchase = CustomerPurchase.objects.prefetch_related('items__product').get(purchase_id=purchase_id)
            else:
                purchase = CustomerPurchase.objects.prefetch_related('items__product').get(invoice_number=invoice_number)

            serializer = CustomerPurchaseSerializer(purchase)
            return Response({
                "success": True,
                "data": serializer.data,
            }, status=status.HTTP_200_OK)

        except CustomerPurchase.DoesNotExist:
            return Response(
                {"success": False, "error": "Invoice not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            logger.error(f"Error fetching invoice: {exc}")
            return Response(
                {"success": False, "error": "Failed to fetch invoice."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CustomerPaymentAPIView(APIView):
    """
    POST: add payment record (seller only)
    GET: get all invoices/purchases with payment status by customer_id (authenticated users)
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsSeller()]
        return [IsAuthenticated()]

    def post(self, request):
        """
        Add a new payment record (seller only).
        Supports file upload for payment_proof (PDF, JPG, PNG, JPEG).
        EXACT same pattern as SupplierPaymentAPIView.post()
        """
        from datetime import datetime
        # Handle multipart/form-data properly - request.data is QueryDict for multipart
        # For QueryDict, use .get() method which returns single values
        # For regular dict, use as-is
        # EXACT same pattern as SupplierPurchaseHistoryAPIView
        try:
            # Check if it's a QueryDict (multipart form data)
            from django.http import QueryDict
            if isinstance(request.data, QueryDict):
                # QueryDict - extract values using .get() to get single values
                data = {
                    'invoice_number': request.data.get('invoice_number'),
                    'customer_purchase': request.data.get('customer_purchase'),
                    'amount': request.data.get('amount'),
                    'payment_mode': request.data.get('payment_mode'),
                    'payment_date': request.data.get('payment_date'),
                    'note': request.data.get('note', ''),
                }
            else:
                # Regular dict
                data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data) if request.data else {}
        except Exception as e:
            logger.exception(f"Error processing request data: {str(e)}")
            return Response(
                {"success": False, "message": f"Error processing request data: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Handle payment proof file upload (PDF, JPG, PNG, JPEG only)
        # EXACT same pattern as invoice_file in SupplierPurchaseHistoryAPIView
        uploaded_proof = request.FILES.get("payment_proof")
        payment_proof_path = None
        
        # Payment proof is REQUIRED - return error if not provided
        if not uploaded_proof:
            logger.error("ERROR: payment_proof file not found in request.FILES!")
            logger.error(f"Available FILES keys: {list(request.FILES.keys())}")
            logger.error(f"Available data keys: {list(request.data.keys()) if hasattr(request.data, 'keys') else 'N/A'}")
            logger.error(f"Content-Type: {request.META.get('CONTENT_TYPE', 'NOT SET')}")
            return Response(
                {
                    "success": False,
                    "message": "Payment proof file is required. Please upload a file (PDF, JPG, PNG, or JPEG).",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        if uploaded_proof:
            logger.info(f"Payment proof file received: {uploaded_proof.name}, size: {uploaded_proof.size} bytes")
            try:
                # Validate file extension
                allowed_extensions = ['.pdf', '.jpg', '.jpeg', '.png']
                ext = os.path.splitext(uploaded_proof.name)[1].lower()
                if ext not in allowed_extensions:
                    return Response(
                        {"success": False, "message": f"Invalid file format. Only PDF, JPG, PNG, JPEG are allowed. Got: {ext}"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                
                # Build a safe, unique path under proof/ folder
                unique_filename = f"{uuid.uuid4().hex}{ext}"
                filename = f"proof/{unique_filename}"
                
                # Ensure proof directory exists in MEDIA_ROOT
                if settings.MEDIA_ROOT:
                    proof_dir = os.path.join(settings.MEDIA_ROOT, 'proof')
                    if not os.path.exists(proof_dir):
                        os.makedirs(proof_dir, exist_ok=True)
                        logger.info(f"Created proof directory: {proof_dir}")
                    else:
                        logger.info(f"Proof directory already exists: {proof_dir}")
                
                # Save the file (same pattern as invoice_file in SupplierPurchaseHistoryAPIView)
                # Reset file pointer to beginning in case it was read before
                uploaded_proof.seek(0)
                saved_path = default_storage.save(filename, ContentFile(uploaded_proof.read()))
                
                # Log the actual save location for verification
                full_path = os.path.join(settings.MEDIA_ROOT, saved_path) if settings.MEDIA_ROOT else saved_path
                logger.info(f"Payment proof file saved successfully. Relative path: {saved_path}, Full path: {full_path}")
                
                # Verify file exists (same pattern as invoice_file)
                if os.path.exists(full_path):
                    logger.info(f"Payment proof file verified at: {full_path}")
                else:
                    logger.warning(f"Payment proof file not found at expected path: {full_path}")
                
                # Store as media/proof/filename (same pattern as invoice_file: media/invoices/filename)
                # saved_path will be "proof/filename.pdf", stored as "media/proof/filename.pdf"
                payment_proof_path = f"media/{saved_path}"
            except Exception as e:
                logger.exception(f"Error saving payment proof file: {str(e)}")
                return Response(
                    {"success": False, "message": f"Error saving payment proof file: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        
        # Remove payment_proof from data if it exists (files come from request.FILES, not request.data)
        # EXACT same pattern as invoice_file in SupplierPurchaseHistoryAPIView
        if 'payment_proof' in data:
            del data['payment_proof']
        
        # Add payment_proof path to data if it was saved (EXACT same pattern as invoice_file)
        if payment_proof_path:
            data["payment_proof"] = payment_proof_path
            logger.info(f"Payment proof file saved. Path: {payment_proof_path}")
        else:
            # payment_proof is optional, so set to empty string if no file (EXACT same pattern as invoice_file)
            data["payment_proof"] = ""
        
        serializer = CustomerPaymentSerializer(data=data, context={'request': request})
        try:
            # Validate serializer and log errors if any
            if not serializer.is_valid():
                logger.error(f"Serializer validation errors: {serializer.errors}")
                logger.error(f"Data that failed validation: {data}")
                return Response(
                    {
                        "success": False,
                        "message": "Validation failed. Please check your input.",
                        "errors": serializer.errors,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            payment = serializer.save()
            
            # Log the saved payment to verify payment_proof was saved
            logger.info(f"Payment saved successfully. ID: {payment.payment_id}, payment_proof: {payment.payment_proof}")
            return Response(
                {
                    "success": True,
                    "message": "Payment record added successfully.",
                    "payment_id": payment.payment_id,
                    "data": CustomerPaymentSerializer(payment, context={'request': request}).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except serializers.ValidationError as exc:
            logger.exception(f"Serializer ValidationError: {exc.detail}")
            return Response(
                {
                    "success": False,
                    "message": "Validation failed. Please check your input.",
                    "errors": exc.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Payment record creation failed")
            return Response(
                {
                    "success": False,
                    "message": "Unable to add payment record at the moment.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        """
        Get all purchases with payment status for a specific customer, or all customers if customer_id is not provided.
        If action=history is provided with customer_id, returns individual payment records (payment history).
        """
        customer_id = request.query_params.get("customer_id")
        action = request.query_params.get("action")
        
        # If action=history, return payment history (individual payment records)
        if action == "history" and customer_id:
            try:
                # Verify customer exists
                customer = Customer.objects.get(customer_id=customer_id)
                # Fetch all payment records for this customer
                payments = (
                    CustomerPayment.objects.select_related("customer_purchase", "customer_purchase__customer")
                    .filter(customer_purchase__customer_id=customer_id)
                    .order_by("-payment_date", "-created_at")
                )
                
                # Serialize payment records
                payment_data = []
                for payment in payments:
                    # Calculate outstanding amount for this invoice after this payment
                    purchase = payment.customer_purchase
                    total_amount = float(purchase.total_amount or 0)
                    
                    # Sum all payments made up to and including this payment
                    # Use payment_date and payment_id to handle same-date payments
                    from django.db.models import Q
                    payments_up_to_this = CustomerPayment.objects.filter(
                        customer_purchase_id=purchase.purchase_id
                    ).filter(
                        Q(payment_date__lt=payment.payment_date) |
                        (Q(payment_date=payment.payment_date) & Q(payment_id__lte=payment.payment_id))
                    ).aggregate(total_paid=Sum("amount"))
                    total_paid_up_to_this = float(payments_up_to_this["total_paid"] or 0)
                    
                    # Outstanding after this payment
                    outstanding_after = max(total_amount - total_paid_up_to_this, 0)
                    
                    serializer = CustomerPaymentSerializer(payment, context={'request': request})
                    payment_record = serializer.data
                    payment_record["outstanding_amount"] = round(outstanding_after, 2)
                    payment_record["total_amount"] = round(total_amount, 2)  # Add total invoice amount
                    payment_data.append(payment_record)
                
                return Response(
                    {
                        "success": True,
                        "customer_id": customer_id,
                        "customer_name": customer.full_name,
                        "count": len(payment_data),
                        "data": payment_data,
                    },
                    status=status.HTTP_200_OK,
                )
            except Customer.DoesNotExist:
                return Response(
                    {"success": False, "message": "Customer not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            except Exception:
                logger.exception("Failed to fetch customer payment history")
                return Response(
                    {
                        "success": False,
                        "message": "Unable to fetch payment history at the moment.",
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # Check if the customer_payment table has the required column
        from django.db import connection
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW COLUMNS FROM customer_payment LIKE 'customer_purchase_id'")
                has_column = cursor.fetchone() is not None
            if not has_column:
                return Response(
                    {
                        "success": False,
                        "message": "Database schema is missing required column. Please run: python manage.py migrate",
                        "error": "The customer_payment table is missing the customer_purchase_id column. Run migrations to fix this.",
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        except Exception as schema_check_error:
            # If we can't check the schema, continue and let the actual query fail with a better error
            logger.warning(f"Could not check database schema: {schema_check_error}")

        try:
            if customer_id:
                # Verify customer exists
                customer = Customer.objects.get(customer_id=customer_id)
                # Fetch all purchases for this customer (with items for amount calculation)
                purchases = (
                    CustomerPurchase.objects.select_related("customer")
                    .prefetch_related("items")
                    .filter(customer_id=customer_id)
                    .order_by("-invoice_date", "-created_at")
                )
                # Build response with payment status and per-invoice totals
                data = []
                for purchase in purchases:
                    # Sum all payments done for this purchase
                    # Try using reverse relation, fallback to 0 if column doesn't exist
                    try:
                        payment_agg = purchase.payments.aggregate(total_paid=Sum("amount"))
                        total_paid = float(payment_agg["total_paid"] or 0)
                    except Exception as e:
                        # If the customer_purchase_id column doesn't exist, assume no payments
                        logger.warning(f"Could not fetch payments for purchase {purchase.purchase_id}: {e}")
                        total_paid = 0.0
                    # Get total amount from purchase
                    total_amount = float(purchase.total_amount or 0)

                    # Calculate outstanding amount and payment status
                    outstanding = max(total_amount - total_paid, 0)
                    if total_amount <= 0:
                        payment_status = "pending"
                    elif outstanding <= 0:
                        payment_status = "received"
                    else:
                        payment_status = "partial"
                    purchase_data = {
                        "customer_purchase_id": purchase.purchase_id,
                        "customer_id": purchase.customer.customer_id,
                        "customer_name": purchase.customer.full_name,
                        "invoice_number": purchase.invoice_number,
                        "invoice_date": purchase.invoice_date,
                        "total_amount": round(total_amount, 2),
                        "paid_amount": round(total_paid, 2),
                        "outstanding_amount": round(outstanding, 2),
                        "payment_status": payment_status,
                    }
                    data.append(purchase_data)
                return Response(
                    {
                        "success": True,
                        "customer_id": customer_id,
                        "customer_name": customer.full_name,
                        "count": len(data),
                        "data": data,
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                # Fetch all purchases for all customers (with items for amount calculation)
                purchases = (
                    CustomerPurchase.objects.select_related("customer")
                    .prefetch_related("items")
                    .all()
                    .order_by("-invoice_date", "-created_at")
                )
                data = []
                for purchase in purchases:
                    # Try using reverse relation, fallback to 0 if column doesn't exist
                    try:
                        payment_agg = purchase.payments.aggregate(total_paid=Sum("amount"))
                        total_paid = float(payment_agg["total_paid"] or 0)
                    except Exception as e:
                        # If the customer_purchase_id column doesn't exist, assume no payments
                        logger.warning(f"Could not fetch payments for purchase {purchase.purchase_id}: {e}")
                        total_paid = 0.0
                    total_amount = float(purchase.total_amount or 0)

                    outstanding = max(total_amount - total_paid, 0)
                    if total_amount <= 0:
                        payment_status = "pending"
                    elif outstanding <= 0:
                        payment_status = "received"
                    else:
                        payment_status = "partial"
                    purchase_data = {
                        "customer_purchase_id": purchase.purchase_id,
                        "customer_id": purchase.customer.customer_id,
                        "customer_name": purchase.customer.full_name,
                        "invoice_number": purchase.invoice_number,
                        "invoice_date": purchase.invoice_date,
                        "total_amount": round(total_amount, 2),
                        "paid_amount": round(total_paid, 2),
                        "outstanding_amount": round(outstanding, 2),
                        "payment_status": payment_status,
                    }
                    data.append(purchase_data)
                return Response(
                    {
                        "success": True,
                        "count": len(data),
                        "data": data,
                    },
                    status=status.HTTP_200_OK,
                )
        except Customer.DoesNotExist:
            return Response(
                {"success": False, "message": "Customer not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            logger.exception("Failed to fetch customer payment data")
            error_msg = str(exc)
            # Check if it's a missing column error
            if "Unknown column" in error_msg and "customer_purchase_id" in error_msg:
                return Response(
                    {
                        "success": False,
                        "message": "Database schema mismatch. Please run migrations: python manage.py migrate",
                        "error": "The customer_payment table is missing the customer_purchase_id column.",
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            return Response(
                {
                    "success": False,
                    "message": f"Unable to fetch payment data at the moment. Error: {error_msg}",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CustomerOutstandingAPIView(APIView):
    """
    GET: Show outstanding for customers (can filter by customer_id)
    Response: customer_id, customer_name, total_amount, paid_amount, outstanding
    Only shows customers belonging to the logged-in seller.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        customer_id = request.query_params.get("customer_id")

        # Build customer queryset - filter by logged-in seller
        customers = Customer.objects.filter(seller_user=request.user)
        if customer_id:
            customers = customers.filter(customer_id=customer_id)

        result = []
        for customer in customers:
            # Get all purchases for this customer
            purchases = CustomerPurchase.objects.filter(customer=customer)
            
            # Sum total amount from all purchase items (including GST)
            # Use line_total which is already calculated as: quantity * unit_price * (1 + gst/100)
            total_amount = 0
            for purchase in purchases:
                items = purchase.items.all()
                for item in items:
                    # line_total is already calculated and includes GST
                    line_total = float(item.line_total or 0)
                    total_amount += line_total

            # Sum paid amount from all payments for this customer
            paid_amount = CustomerPayment.objects.filter(
                customer_purchase__customer=customer
            ).aggregate(total_paid=Sum('amount'))['total_paid'] or 0

            outstanding = max(total_amount - float(paid_amount), 0)

            result.append({
                "customer_id": customer.customer_id,
                "customer_name": customer.full_name,
                "total_amount": round(total_amount, 2),
                "paid_amount": round(float(paid_amount), 2),
                "outstanding": round(outstanding, 2),
            })

        return Response({
            "success": True,
            "count": len(result),
            "data": result,
        }, status=status.HTTP_200_OK)


# ==========================================
# WHATSAPP INTEGRATION & WEBHOOK ENGINE
# ==========================================

def _get_next_node_id(edges, source_id, label=None, source_handle=None):
    out = [e for e in edges if e.get("source") == source_id]
    if source_handle:
        sh = str(source_handle)
        for e in out:
            if str(e.get("sourceHandle") or "") == sh:
                return e.get("target")
    if label:
        lbl = str(label).lower()
        # 1. Match sourceHandle directly with label (e.g. 'yes', 'no', 'success', 'fallback')
        for e in out:
            if str(e.get("sourceHandle") or "").lower() == lbl:
                return e.get("target")
        # 2. Match label
        for e in out:
            edge_lbl = str(e.get("label") or "").lower()
            if edge_lbl == lbl or (lbl == "yes" and edge_lbl == "") or (lbl == "yes" and not any(str(e2.get("label") or "").lower() == "no" for e2 in out)):
                return e.get("target")
        if lbl == "no" and len(out) > 1:
            no_edge = next((e for e in out if str(e.get("label") or "").lower() == "no"), out[1])
            return no_edge.get("target")

    # default plain edge (no sourceHandle/label)
    plain = [e for e in out if not str(e.get("sourceHandle") or "").strip() and not str(e.get("label") or "").strip()]
    if plain:
        return plain[0].get("target")
    return out[0].get("target") if out else None


def _render_text_with_vars(text, collected_data):
    if not text:
        return ""
    text_str = str(text)
    def replace_var(match):
        key = match.group(1).strip()
        val = collected_data.get(key)
        if val is None:
            # try lowercase matching
            lower = key.lower()
            for k, v in collected_data.items():
                if k.lower() == lower:
                    val = v
                    break
        if val is not None:
            return str(val)
        return match.group(0)

    # Normalize fullwidth @
    normalized = text_str.replace("\uFF03", "@")
    return re.sub(r"@\s*([a-zA-Z0-9_]+)", replace_var, normalized)


def _sanitize_meta_media_url(raw_url, domain="api.qa.syntrrix.com"):
    if not raw_url:
        return ""
    url_str = str(raw_url).strip()
    if not url_str or url_str.lower().startswith("blob:"):
        return ""

    public_domain = "api.qa.syntrrix.com"

    if url_str.startswith("/"):
        return f"https://{public_domain}{url_str}"

    if any(local_host in url_str.lower() for local_host in ["127.0.0.1", "localhost", "0.0.0.0", "192.168.", "10.0.", "172.16."]):
        import re
        url_str = re.sub(r'http://(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\d+\.\d+\.\d+\.\d+)(?::\d+)?', f'https://{public_domain}', url_str)
        url_str = re.sub(r'https://(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\d+\.\d+\.\d+\.\d+)(?::\d+)?', f'https://{public_domain}', url_str)

    if url_str.startswith("http://"):
        url_str = url_str.replace("http://", "https://")

    return url_str


def _send_whatsapp_message_raw(config, recipient, payload):
    if not config or not config.phone_number_id or not config.access_token:
        logger.error("WhatsApp configuration incomplete.")
        return (False, "WhatsApp configuration incomplete (Missing access token or phone number ID).")

    url = f"https://graph.facebook.com/v21.0/{config.phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {config.access_token}",
        "Content-Type": "application/json"
    }
    to_phone = re.sub(r"\D", "", str(recipient))

    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
    }
    body.update(payload)

    try:
        import requests
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        if resp.status_code in [200, 201]:
            return (True, None)
        else:
            logger.error(f"WhatsApp API error: {resp.status_code} - {resp.text}")
            try:
                err_data = resp.json()
                meta_err = err_data.get("error", {})
                err_msg = meta_err.get("message") or resp.text
                err_code = meta_err.get("code")
                err_subcode = meta_err.get("error_subcode")
                msg = f"Meta Error (Code {err_code}, Subcode {err_subcode}): {err_msg}"
                return (False, msg)
            except Exception:
                return (False, f"Meta API Error: {resp.status_code} - {resp.text}")
    except Exception as ex:
        logger.error(f"WhatsApp API exception: {ex}")
        try:
            import urllib.request
            import urllib.error
            import json
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status in [200, 201]:
                    return (True, None)
                else:
                    return (False, f"Urllib Send Error Status: {r.status}")
        except urllib.error.HTTPError as he:
            try:
                err_body = he.read().decode("utf-8")
                err_data = json.loads(err_body)
                meta_err = err_data.get("error", {})
                err_msg = meta_err.get("message") or err_body
                return (False, f"Meta Error: {err_msg}")
            except Exception:
                return (False, f"HTTP Error {he.code}: {he.reason}")
        except Exception as ex2:
            logger.error(f"Urllib fallback exception: {ex2}")
            return (False, f"Connection Exception: {str(ex2)}")


def _send_whatsapp_text(session, text, config=None):
    if not config:
        from agro_app.models import WhatsAppConfig
        config = WhatsAppConfig.objects.filter(seller_user=session.seller_user, is_connected=True).first()
    if not config or not config.is_connected:
        logger.warning(f"WhatsApp Bot Runner: No active WhatsAppConfig found for seller '{session.seller_user.email}' inside _send_whatsapp_text")
        return False
    payload = {
        "type": "text",
        "text": {"preview_url": False, "body": str(text)}
    }
    ok, err_msg = _send_whatsapp_message_raw(config, session.customer_phone, payload)
    if not ok:
        logger.error(f"WhatsApp Bot Runner: Failed to send text to {session.customer_phone}. Error: {err_msg}")
    else:
        if session and session.live_chat_session:
            _save_live_chat_message(session.live_chat_session, LiveChatMessage.SENDER_SELLER, str(text))
    return ok


def _send_whatsapp_interactive_buttons(session, text, buttons, config=None):
    if not config:
        from agro_app.models import WhatsAppConfig
        config = WhatsAppConfig.objects.filter(seller_user=session.seller_user, is_connected=True).first()
    if not config or not config.is_connected:
        logger.warning(f"WhatsApp Bot Runner: No active WhatsAppConfig found for seller '{session.seller_user.email}' inside _send_whatsapp_interactive_buttons")
        return False

    whatsapp_buttons = []
    btn_titles = []
    for btn in buttons[:3]:
        b_title = str(btn.get("title"))[:20]
        whatsapp_buttons.append({
            "type": "reply",
            "reply": {
                "id": str(btn.get("id"))[:256],
                "title": b_title
            }
        })
        btn_titles.append(f"• {b_title}")

    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": str(text)[:1024]},
            "action": {
                "buttons": whatsapp_buttons
            }
        }
    }
    ok, err_msg = _send_whatsapp_message_raw(config, session.customer_phone, payload)
    if not ok:
        logger.error(f"WhatsApp Bot Runner: Failed to send interactive buttons to {session.customer_phone}. Error: {err_msg}")
    else:
        if session and session.live_chat_session:
            display_msg = f"[Bot Question]: {text}"
            if btn_titles:
                display_msg += "\n\nOptions:\n" + "\n".join(btn_titles)
            _save_live_chat_message(session.live_chat_session, LiveChatMessage.SENDER_SELLER, display_msg)
    return ok


def _send_whatsapp_interactive_list(session, text, button_label, rows, config=None):
    if not config:
        from agro_app.models import WhatsAppConfig
        config = WhatsAppConfig.objects.filter(seller_user=session.seller_user, is_connected=True).first()
    if not config or not config.is_connected:
        return False

    list_rows = []
    row_titles = []
    for i, r in enumerate(rows[:10]):
        title = str(r.get("title"))[:24]
        desc = str(r.get("description") or "")[:72]
        list_rows.append({
            "id": str(r.get("id"))[:200],
            "title": title,
            "description": desc
        })
        row_titles.append(f"{i+1}. {title}" + (f" ({desc})" if desc else ""))

    payload = {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": str(text)[:1024]},
            "action": {
                "button": str(button_label)[:20],
                "sections": [
                    {
                        "title": "Options",
                        "rows": list_rows
                    }
                ]
            }
        }
    }
    ok, _ = _send_whatsapp_message_raw(config, session.customer_phone, payload)
    if ok and session and session.live_chat_session:
        display_msg = f"[Bot Question]: {text}"
        if row_titles:
            display_msg += "\n\nSelectable Options:\n" + "\n".join(row_titles)
        _save_live_chat_message(session.live_chat_session, LiveChatMessage.SENDER_SELLER, display_msg)
    return ok



def _create_or_update_captured_customer(session, bot):
    bag = session.collected_data
    def pick(*keys):
        for k in keys:
            v = bag.get(k)
            if v and str(v).strip():
                return str(v).strip()
    full_name = pick("name", "full_name") or (session.customer_name if session.customer_name and not session.customer_name.startswith("WA Customer") else "")
    email = pick("email") or ""
    address = bag.get("address") or ""

    customer, created = Customer.objects.get_or_create(
        seller_user=bot.seller_user,
        mobile_number=session.customer_phone,
        defaults={
            "full_name": full_name,
            "email": email,
            "whatsapp_number": session.customer_phone,
            "address": address,
            "created_via_bot": True,
            "source_bot": bot,
            "bot_payload": {
                "address": address,
                "data_storage_requirement": bag.get("data_storage_requirement") or "",
                "custom_input": bag.get("custom_input") or "",
            }
        }
    )
    if not created:
        updated = False
        if full_name and customer.full_name != full_name:
            customer.full_name = full_name
            updated = True
        if email and customer.email != email:
            customer.email = email
            updated = True
        if address and customer.address != address:
            customer.address = address
            updated = True
        if updated:
            customer.save()

    return customer


def _persist_whatsapp_chat_transcript(chat_session, collected_data):
    try:
        sid = int(chat_session.session_id)
        existing = _get_persisted_live_chat_messages(sid)
        if existing:
            # Real live chat messages are already preserved in real-time. Do not overwrite!
            return

        chat_rows = []
        base_time = timezone.now() - timedelta(minutes=5)
        idx = 0

        for k, v in (collected_data or {}).items():
            if k in ["name", "full_name", "phone", "email", "address", "data_storage_requirement", "custom_input"] and v:
                idx += 1
                chat_rows.append({
                    "message_id": uuid.uuid4().hex,
                    "sender_type": LiveChatMessage.SENDER_SELLER,
                    "message": f"Collected {k}:",
                    "created_at": (base_time + timedelta(seconds=idx * 5)).isoformat(),
                })
                idx += 1
                chat_rows.append({
                    "message_id": uuid.uuid4().hex,
                    "sender_type": LiveChatMessage.SENDER_CUSTOMER,
                    "message": str(v),
                    "created_at": (base_time + timedelta(seconds=idx * 5)).isoformat(),
                })

        if chat_rows:
            _set_persisted_live_chat_messages(sid, chat_rows)
    except Exception as ex:
        logger.error(f"Error persisting WA transcript: {ex}")


def _execute_data_hub_node_op(session, node, bot):
    try:
        seller_id = int(bot.seller_user_id)
        operation = str(node.get("data", {}).get("operation") or "INSERT").strip().upper()
        table_id = str(node.get("data", {}).get("table_id") or "").strip()
        if not table_id:
            return False

        mappings = node.get("data", {}).get("mappings") or []
        row_id_var = str(node.get("data", {}).get("row_id_var") or "").strip()

        rows_path = _data_hub_table_rows_file(seller_id, table_id)
        payload = _read_json_file(rows_path, default={"rows": []})
        rows = payload.get("rows") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []

        def resolve_var(val, bag):
            val_str = str(val or "").strip()
            if val_str.startswith("@"):
                k = val_str[1:].strip()
                resolved = bag.get(k)
                if resolved is None:
                    lower = k.lower()
                    for bk, bv in bag.items():
                        if bk.lower() == lower:
                            resolved = bv
                            break
                return str(resolved) if resolved is not None else ""
            return val_str

        bag = session.collected_data

        if operation == "INSERT":
            row = {}
            for m in mappings:
                col = str(m.get("column") or "").strip()
                var = str(m.get("variable") or "").strip()
                if col:
                    row[col] = resolve_var(var, bag)

            row_id = uuid.uuid4().hex
            row["row_id"] = row_id
            row["created_at"] = timezone.now().isoformat()
            row["updated_at"] = timezone.now().isoformat()
            rows.append(row)
            _write_json_file_atomic(rows_path, {"rows": rows})
            return True

        elif operation == "UPDATE":
            row_id = resolve_var(row_id_var, bag)
            if not row_id:
                return False
            found = False
            for r in rows:
                if str(r.get("row_id")) == str(row_id):
                    for m in mappings:
                        col = str(m.get("column") or "").strip()
                        var = str(m.get("variable") or "").strip()
                        if col:
                            r[col] = resolve_var(var, bag)
                    r["updated_at"] = timezone.now().isoformat()
                    found = True
                    break
            if found:
                _write_json_file_atomic(rows_path, {"rows": rows})
                return True
            return False

        elif operation == "DELETE":
            row_id = resolve_var(row_id_var, bag)
            if not row_id:
                return False
            new_rows = [r for r in rows if str(r.get("row_id")) != str(row_id)]
            if len(new_rows) != len(rows):
                _write_json_file_atomic(rows_path, {"rows": new_rows})
                return True
            return False

        elif operation == "SEARCH":
            filters = node.get("data", {}).get("filters") or []
            matched_row = None
            for r in rows:
                match_all = True
                for f in filters:
                    col = str(f.get("column") or "").strip()
                    op = str(f.get("operator") or "IS").strip().upper()
                    f_val = resolve_var(f.get("value"), bag)

                    r_val = str(r.get(col) or "").strip()
                    if op in ["IS", "EQUALS"]:
                        if r_val.lower() != f_val.lower():
                            match_all = False
                            break
                    elif op == "CONTAINS":
                        if f_val.lower() not in r_val.lower():
                            match_all = False
                            break
                if match_all:
                    matched_row = r
                    break

            if matched_row:
                rmaps = node.get("data", {}).get("response_mappings") or []
                for rm in rmaps:
                    col = str(rm.get("column") or "").strip()
                    vname = str(rm.get("variable") or "").strip()
                    if vname.startswith("@"):
                        vname = vname[1:]
                    cell_val = matched_row.get(col)
                    if cell_val is None:
                        lower = col.lower()
                        for rk, rv in matched_row.items():
                            if rk.lower() == lower:
                                cell_val = rv
                                break
                    bag[vname] = str(cell_val) if cell_val is not None else ""

                for alt in ["order_no", "orderNo", "OrderNo", "Order number", "row_id"]:
                    val = matched_row.get(alt)
                    if val:
                        bag["order_no"] = str(val)
                        break
                for alt in ["status", "Status", "STATUS"]:
                    val = matched_row.get(alt)
                    if val:
                        bag["status"] = str(val)
                        break
                return True
            return False
    except Exception:
        return False


def _fetch_meta_media_url(config, media_id):
    if not media_id or not config or not config.access_token:
        return ""
    try:
        import requests
        url = f"https://graph.facebook.com/v21.0/{media_id}"
        headers = {"Authorization": f"Bearer {config.access_token}"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json().get("url") or ""
    except Exception:
        pass
    return ""


def _run_whatsapp_bot(session, msg_text=None, incoming_media_payload=None, config=None):
    if config and config.bot:
        bot = config.bot
    else:
        try:
            bot = Bot.objects.get(seller_user=session.seller_user, is_published=True)
        except Bot.DoesNotExist:
            logger.warning(f"WhatsApp Bot Runner: No published bot found for seller '{session.seller_user.email}'")
            return

    try:
        flow = bot.flow or {}
        nodes = {n["id"]: n for n in flow.get("nodes", [])}
        edges = flow.get("edges", [])

        logger.info(f"WhatsApp Bot Runner: Loaded bot flow. Nodes: {list(nodes.keys())}. Edges: {edges}")

        # Allow customer to restart flow anytime by typing reset/start
        if msg_text and str(msg_text).strip().lower() in ["start", "restart", "reset", "/start"]:
            session.current_node_id = "start"
            session.collected_data = {}
            session.is_submitted = False
            session.save()

        is_start_session = False
        # Resolve 'start' current_node_id to the actual root node of the flow
        if session.current_node_id == "start":
            is_start_session = True
            if "start" not in nodes:
                targets = {e.get("target") for e in edges if e.get("target")}
                candidates = [nid for nid in nodes.keys() if nid not in targets]
                if candidates:
                    start_cand = next((c for c in candidates if "start" in c.lower()), candidates[0])
                    session.current_node_id = start_cand
                    logger.info(f"WhatsApp Bot Runner: Resolved 'start' node ID to flow root node '{start_cand}'")
                elif nodes:
                    first_node = list(nodes.keys())[0]
                    session.current_node_id = first_node
                    logger.info(f"WhatsApp Bot Runner: No root node found, fallback to first node '{first_node}'")

        logger.info(f"WhatsApp Bot Runner: Resuming bot execution loop for customer {session.customer_phone} from node '{session.current_node_id}' with text '{msg_text}'")

        # Create/sync customer and LiveChatSession immediately so entry appears in Lead Pipeline & Live Chat Support right away!
        customer = _create_or_update_captured_customer(session, bot)
        channel_source = f"WhatsApp - {config.channel_name}" if config and config.channel_name else "WhatsApp"

        chat_session = session.live_chat_session
        if not chat_session:
            chat_session, created = LiveChatSession.objects.get_or_create(
                customer=customer,
                defaults={
                    "seller_user": bot.seller_user,
                    "source_bot": bot,
                    "lead_pipeline_status": "New Leads",
                    "visitor_payload": {
                        "lead_source": channel_source,
                        **(session.collected_data or {})
                    }
                }
            )
            session.live_chat_session = chat_session
            session.save(update_fields=["live_chat_session"])

        if chat_session and not chat_session.lead_pipeline_status:
            chat_session.lead_pipeline_status = "New Leads"
            chat_session.save(update_fields=["lead_pipeline_status"])

        # Record incoming customer text into transcript for live chat support panel
        if msg_text and chat_session:
            _save_live_chat_message(chat_session, LiveChatMessage.SENDER_CUSTOMER, str(msg_text), incoming_media_payload)

        # On initial bot session start/trigger, clear msg_text & incoming_media_payload so trigger text isn't consumed by 2nd/first question node
        if is_start_session:
            msg_text = None
            incoming_media_payload = None

        while session.current_node_id:
            node = nodes.get(session.current_node_id)
            if not node:
                break

            kind = node.get("data", {}).get("kind")

            if node["id"] == "start":
                session.current_node_id = _get_next_node_id(edges, node["id"])
                continue

            elif kind == "message":
                text = _render_text_with_vars(node.get("data", {}).get("text") or node.get("data", {}).get("label") or "...", session.collected_data)
                _send_whatsapp_text(session, text, config=config)
                session.current_node_id = _get_next_node_id(edges, node["id"])
                continue

            elif kind in ["name", "phone", "email", "address", "data_storage_requirement", "custom_input", "yes_no", "multiple_choice", "media_upload"]:
                if msg_text is not None or incoming_media_payload is not None:
                    var_name = node.get("data", {}).get("variable") or kind
                    if var_name.startswith("@"):
                        var_name = var_name[1:]

                    val = ""
                    selected_opt_id = None
                    if kind == "yes_no":
                        reply = (msg_text or "").strip().lower()
                        if reply in ["yes", "ha", "haan", "y", "1", "true", "correct", "yes_no_yes"]:
                            val = "yes"
                        else:
                            val = "no"
                    elif kind == "multiple_choice":
                        reply = (msg_text or "").strip().lower()
                        options = node.get("data", {}).get("options", [])
                        selected_opt_label = None
                        for opt in options:
                             opt_lbl = str(opt.get("label") or "").strip()
                             opt_id = str(opt.get("id") or "").strip()
                             if reply == opt_lbl.lower() or reply == opt_id.lower():
                                 selected_opt_label = opt_lbl
                                 selected_opt_id = opt_id
                                 break
                        if not selected_opt_label:
                             for idx, opt in enumerate(options):
                                 if reply == str(idx + 1):
                                     selected_opt_label = str(opt.get("label") or "")
                                     selected_opt_id = str(opt.get("id") or "")
                                     break
                        if not selected_opt_label:
                            selected_opt_label = msg_text
                        val = selected_opt_label
                    elif kind == "media_upload":
                        if incoming_media_payload:
                            val = incoming_media_payload.get("file_url") or ""
                            session.collected_data[f"{var_name}_file_name"] = incoming_media_payload.get("file_name") or ""
                            session.collected_data[f"{var_name}_mime_type"] = incoming_media_payload.get("mime_type") or ""
                        else:
                            val = msg_text or ""
                    else:
                        val = msg_text

                    session.collected_data[var_name] = val

                    # Instant name mapping: when user enters name at name step/variable
                    if kind == "name" or str(var_name).lower() in ["name", "full_name", "customer_name", "user_name"]:
                        user_entered_name = str(val or "").strip()
                        if user_entered_name:
                            session.customer_name = user_entered_name
                            if chat_session:
                                chat_session.visitor_name = user_entered_name
                                if chat_session.customer:
                                    chat_session.customer.full_name = user_entered_name
                                    chat_session.customer.save(update_fields=["full_name"])
                                chat_session.save(update_fields=["visitor_name"])

                    next_node_id = None
                    if kind == "yes_no":
                        next_node_id = _get_next_node_id(edges, node["id"], label=val)
                    elif kind == "multiple_choice":
                        next_node_id = _get_next_node_id(edges, node["id"], source_handle=selected_opt_id)
                    else:
                        next_node_id = _get_next_node_id(edges, node["id"])

                    session.current_node_id = next_node_id
                    msg_text = None
                    incoming_media_payload = None
                    continue
                else:
                    prompt = _render_text_with_vars(node.get("data", {}).get("prompt") or node.get("data", {}).get("label") or "Please input details:", session.collected_data)
                    if kind == "yes_no":
                        _send_whatsapp_interactive_buttons(session, prompt, [{"id": "yes_no_yes", "title": "Yes"}, {"id": "yes_no_no", "title": "No"}], config=config)
                    elif kind == "multiple_choice":
                        options = node.get("data", {}).get("options", [])
                        if len(options) > 0 and len(options) <= 3:
                            buttons = [{"id": str(o.get("id") or ""), "title": str(o.get("label") or "")[:20]} for o in options]
                            _send_whatsapp_interactive_buttons(session, prompt, buttons, config=config)
                        elif len(options) > 3 and len(options) <= 10:
                            rows = [{"id": str(o.get("id") or ""), "title": str(o.get("label") or "")[:24], "description": ""} for o in options]
                            _send_whatsapp_interactive_list(session, prompt, "Select Option", rows, config=config)
                        else:
                            opt_text = "\n".join([f"{i+1}. {opt.get('label')}" for i, opt in enumerate(options)])
                            _send_whatsapp_text(session, f"{prompt}\n\n{opt_text}", config=config)
                    else:
                        _send_whatsapp_text(session, prompt, config=config)
                    break

            elif kind == "agent_assign":
                assigned_team_manager, assigned_agent = _resolve_agent_assignment(bot, node_id=node.get("id"))
                customer = _create_or_update_captured_customer(session, bot)

                chat_session, created = LiveChatSession.objects.get_or_create(
                    customer=customer,
                    defaults={
                        "seller_user": bot.seller_user,
                        "source_bot": bot,
                        "assigned_team_manager": assigned_team_manager,
                        "assigned_agent": assigned_agent,
                        "lead_pipeline_status": "New Leads",
                    }
                )
                chat_session.assigned_team_manager = assigned_team_manager
                chat_session.assigned_agent = assigned_agent
                if not chat_session.lead_pipeline_status:
                    chat_session.lead_pipeline_status = "New Leads"
                chat_session.save(update_fields=["assigned_team_manager", "assigned_agent", "lead_pipeline_status", "updated_at"])
                session.live_chat_session = chat_session
                _sync_session_assignment_to_data_hub(chat_session)

                note = node.get("data", {}).get("label") or "You are being assigned to our support team."
                _send_whatsapp_text(session, note, config=config)

                session.current_node_id = _get_next_node_id(edges, node["id"])
                continue

            elif kind == "data_hub":
                op_ok = _execute_data_hub_node_op(session, node, bot)
                edge_label = "success" if op_ok else "fallback"
                session.current_node_id = _get_next_node_id(edges, node["id"], label=edge_label)
                continue

            elif kind == "catalogue":
                prompt = _render_text_with_vars(node.get("data", {}).get("prompt") or node.get("data", {}).get("label") or "Check out our products:", session.collected_data)
                checkout_url = f"https://demo-agrobridge.com/widget/bot?bot_id={bot.bot_id}"
                _send_whatsapp_text(session, f"{prompt}\n\nBrowse catalog & checkout online: {checkout_url}", config=config)
                session.current_node_id = _get_next_node_id(edges, node["id"])
                continue

            else:
                session.current_node_id = _get_next_node_id(edges, node["id"])
                continue

        if not session.current_node_id and not session.is_submitted:
            session.is_submitted = True
            if chat_session:
                payload_data = chat_session.visitor_payload or {}
                payload_data.update(session.collected_data or {})
                payload_data["lead_source"] = channel_source
                chat_session.visitor_payload = payload_data
                chat_session.save(update_fields=["visitor_payload"])
                _persist_whatsapp_chat_transcript(chat_session, session.collected_data or {})

        session.save()
        logger.info(f"WhatsApp Bot Runner: Saved session state for customer {session.customer_phone} (current_node_id='{session.current_node_id}', is_submitted={session.is_submitted})")
    except Exception as flow_ex:
        import traceback
        logger.error(f"WhatsApp Bot Runner crashed during flow execution for customer {session.customer_phone}: {flow_ex}\n{traceback.format_exc()}")


def _process_whatsapp_customer_message(config, customer_phone, customer_name, msg_text, media_payload=None):
    from agro_app.models import WhatsAppChatSession, LiveChatSession
    cutoff = timezone.now() - timedelta(hours=24)
    session = WhatsAppChatSession.objects.filter(
        seller_user=config.seller_user,
        customer_phone=customer_phone,
        is_submitted=False,
        created_at__gte=cutoff
    ).first()

    if session:
        logger.info(f"WhatsApp Webhook: Active WhatsAppChatSession found for customer {customer_phone}. Advancing bot.")
        _run_whatsapp_bot(session, msg_text=msg_text, incoming_media_payload=media_payload, config=config)
    else:
        live_chat_session = LiveChatSession.objects.filter(
            seller_user=config.seller_user,
            customer__mobile_number=customer_phone
        ).first()
        if not live_chat_session:
            live_chat_session = LiveChatSession.objects.filter(
                seller_user=config.seller_user,
                customer__whatsapp_number=customer_phone
            ).first()
        if not live_chat_session:
            live_chat_session = LiveChatSession.objects.filter(
                seller_user=config.seller_user,
                visitor_mobile_number=customer_phone
            ).first()

        if live_chat_session:
            logger.info(f"WhatsApp Webhook: Customer {customer_phone} already has an active LiveChatSession (ID: {live_chat_session.session_id}) in support panel. Forwarding message to agent and ignoring bot flow.")
            _save_live_chat_message(live_chat_session, LiveChatMessage.SENDER_CUSTOMER, msg_text, media_payload)
        else:
            logger.info(f"WhatsApp Webhook: Starting fresh chatbot session for customer {customer_phone} with text '{msg_text}'.")
            session = WhatsAppChatSession.objects.create(
                seller_user=config.seller_user,
                customer_phone=customer_phone,
                customer_name=customer_name,
                current_node_id="start"
            )
            _run_whatsapp_bot(session, msg_text=msg_text, incoming_media_payload=None, config=config)


class WhatsAppConfigAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from agro_app.models import WhatsAppConfig, Bot
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can configure WhatsApp."}, status=403)

        configs = WhatsAppConfig.objects.filter(seller_user=request.user).select_related("bot").order_by("-updated_at")

        domain = request.META.get("HTTP_HOST") or "api.qa.syntrrix.com"
        scheme = "https" if request.is_secure() else "http"
        webhook_url = f"{scheme}://{domain}/api/whatsapp/webhook/"

        # Ensure seller has 1 single verification token shared across all channel cards
        seller_verify_token = None
        for c in configs:
            if c.webhook_verify_token:
                seller_verify_token = c.webhook_verify_token
                break
        if not seller_verify_token:
            seller_verify_token = uuid.uuid4().hex[:16]

        bots_qs = Bot.objects.filter(seller_user=request.user)
        bots_data = [
            {
                "bot_id": b.bot_id,
                "name": b.name,
                "is_published": b.is_published,
            }
            for b in bots_qs
        ]

        cards_data = []
        for c in configs:
            if c.webhook_verify_token != seller_verify_token:
                c.webhook_verify_token = seller_verify_token
                c.save(update_fields=["webhook_verify_token"])

            cards_data.append({
                "config_id": c.whatsapp_config_id,
                "channel_name": c.channel_name,
                "bot_id": c.bot_id if c.bot else None,
                "bot_name": c.bot.name if c.bot else "Default Bot",
                "phone_number": c.phone_number,
                "phone_number_id": c.phone_number_id,
                "whatsapp_business_account_id": c.whatsapp_business_account_id,
                "access_token": c.access_token,
                "webhook_verify_token": c.webhook_verify_token,
                "is_connected": c.is_connected,
                "updated_at": c.updated_at.strftime("%Y-%m-%d %H:%M:%S") if c.updated_at else "",
            })

        return Response({
            "success": True,
            "data": cards_data,
            "bots": bots_data,
            "webhook_url": webhook_url,
            "default_verify_token": seller_verify_token
        })

    def post(self, request):
        from agro_app.models import WhatsAppConfig, Bot
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can configure WhatsApp."}, status=403)

        payload = request.data or {}
        config_id = payload.get("config_id")

        if config_id:
            try:
                config = WhatsAppConfig.objects.get(whatsapp_config_id=config_id, seller_user=request.user)
            except WhatsAppConfig.DoesNotExist:
                return Response({"success": False, "message": "WhatsApp config not found."}, status=404)
        else:
            config = WhatsAppConfig(seller_user=request.user)

        config.channel_name = (payload.get("channel_name") or "WhatsApp Channel").strip()
        config.phone_number = (payload.get("phone_number") or "").strip()
        config.phone_number_id = (payload.get("phone_number_id") or "").strip()
        config.whatsapp_business_account_id = (payload.get("whatsapp_business_account_id") or "").strip()
        config.access_token = (payload.get("access_token") or "").strip()

        # Re-use seller's existing single verification token if available
        seller_existing_config = WhatsAppConfig.objects.filter(seller_user=request.user).exclude(webhook_verify_token="").first()
        if seller_existing_config and seller_existing_config.webhook_verify_token:
            config.webhook_verify_token = seller_existing_config.webhook_verify_token
        elif payload.get("webhook_verify_token"):
            config.webhook_verify_token = (payload.get("webhook_verify_token") or "").strip()
        else:
            config.webhook_verify_token = uuid.uuid4().hex[:16]

        bot_id = payload.get("bot_id")
        if bot_id:
            try:
                config.bot = Bot.objects.get(bot_id=bot_id, seller_user=request.user)
            except Bot.DoesNotExist:
                config.bot = None
        else:
            config.bot = None

        config.is_connected = True
        config.save()

        # Dynamically register the Webhook Subscribed Apps for this WABA to trigger event calls!
        if config.whatsapp_business_account_id and config.access_token:
            try:
                import requests
                sub_url = f"https://graph.facebook.com/v21.0/{config.whatsapp_business_account_id}/subscribed_apps"
                headers = {"Authorization": f"Bearer {config.access_token}"}
                r = requests.post(sub_url, headers=headers, timeout=10)
                logger.info(f"WABA Subscribed Apps Registration Response: {r.status_code} - {r.text}")
            except Exception as e:
                logger.error(f"Failed to dynamically register WABA subscribed apps: {e}")

        return Response({
            "success": True,
            "message": f"WhatsApp channel '{config.channel_name}' configured successfully.",
            "data": {
                "config_id": config.whatsapp_config_id,
                "channel_name": config.channel_name,
                "bot_id": config.bot_id if config.bot else None,
                "bot_name": config.bot.name if config.bot else "Default Bot",
                "phone_number": config.phone_number,
                "phone_number_id": config.phone_number_id,
                "whatsapp_business_account_id": config.whatsapp_business_account_id,
                "access_token": config.access_token,
                "webhook_verify_token": config.webhook_verify_token,
                "is_connected": config.is_connected
            }
        })


class WhatsAppDisconnectAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from agro_app.models import WhatsAppConfig
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can configure WhatsApp."}, status=403)

        config_id = request.data.get("config_id")
        try:
            if config_id:
                config = WhatsAppConfig.objects.get(whatsapp_config_id=config_id, seller_user=request.user)
            else:
                config = WhatsAppConfig.objects.filter(seller_user=request.user).first()
                if not config:
                    return Response({"success": False, "message": "WhatsApp config not found."}, status=404)

            config.delete()
            return Response({"success": True, "message": "WhatsApp channel disconnected successfully."})
        except WhatsAppConfig.DoesNotExist:
            return Response({"success": False, "message": "WhatsApp config not found."}, status=404)


class WhatsAppTestConnectionAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from agro_app.models import WhatsAppConfig
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can test connection."}, status=403)

        config_id = request.data.get("config_id") or request.data.get("whatsapp_config_id")
        business_number_input = str(request.data.get("businessNumber") or request.data.get("business_number") or "").strip()

        config_qs = WhatsAppConfig.objects.filter(seller_user=request.user)
        config = None

        if config_id:
            config = config_qs.filter(whatsapp_config_id=config_id).first() or config_qs.filter(pk=config_id).first()

        if not config and business_number_input:
            config = config_qs.filter(phone_number__icontains=business_number_input).first() or config_qs.filter(channel_name__icontains=business_number_input).first()
            if not config:
                parts = [p.strip() for p in business_number_input.split("·") if p.strip()]
                for part in parts:
                    found = config_qs.filter(phone_number__icontains=part).first() or config_qs.filter(channel_name__icontains=part).first()
                    if found:
                        config = found
                        break

        if not config:
            config = config_qs.filter(is_connected=True).exclude(phone_number_id="").exclude(access_token="").first()

        if not config:
            config = config_qs.exclude(phone_number_id="").exclude(access_token="").first()

        if not config:
            config = config_qs.first()

        if not config or not config.phone_number_id or not config.access_token:
            return Response({"success": False, "message": "Selected WhatsApp channel is missing Phone Number ID or Access Token. Please configure Meta API credentials in WhatsApp settings."}, status=400)

        test_number = (request.data.get("test_number") or "").strip()
        if not test_number:
            return Response({"success": False, "message": "test_number is required."}, status=400)

        LANG_ISO_MAP = {
            "english (us)": "en_US", "english": "en_US", "en_us": "en_US", "en": "en_US",
            "hindi": "hi", "hi": "hi",
            "marathi": "mr", "mr": "mr",
            "gujarati": "gu", "gu": "gu",
            "tamil": "ta", "ta": "ta",
            "telugu": "te", "te": "te",
            "kannada": "kn", "kn": "kn",
            "malayalam": "ml", "ml": "ml",
            "arabic": "ar", "ar": "ar",
            "french": "fr", "fr": "fr",
            "portuguese (br)": "pt_BR", "pt_br": "pt_BR",
            "spanish": "es", "es": "es"
        }

        use_template = request.data.get("use_template", False)
        raw_template_name = (request.data.get("template_name") or "hello_world").strip()
        raw_language_code = (request.data.get("language_code") or "en_US").strip()
        custom_header_url = request.data.get("header_media_url") or request.data.get("media_url")
        custom_body_params = request.data.get("body_params") or {}

        template_name = None
        tpl_db = None

        if use_template:
            from agro_app.models import WhatsAppTemplate
            import re
            
            # Lookup template in seller's database matching selected config first
            tpl_qs = WhatsAppTemplate.objects.filter(seller_user=request.user, name__iexact=raw_template_name)
            if not tpl_qs.exists():
                tpl_qs = WhatsAppTemplate.objects.filter(seller_user=request.user, header_text__iexact=raw_template_name)

            tpl_db = tpl_qs.filter(whatsapp_config=config).first() or tpl_qs.first()

            if not tpl_db:
                # If requested template is not in DB for this config, pick an approved template for this config
                tpl_db = WhatsAppTemplate.objects.filter(seller_user=request.user, whatsapp_config=config, status='APPROVED').first()
                if not tpl_db:
                    tpl_db = WhatsAppTemplate.objects.filter(seller_user=request.user, status='APPROVED').first()

            if tpl_db:
                template_name = tpl_db.name.strip()
                selected_lang = tpl_db.language.strip() if tpl_db.language else raw_language_code
            else:
                template_name = raw_template_name.lower().replace(" ", "_")
                selected_lang = raw_language_code

            language_code = LANG_ISO_MAP.get(selected_lang.lower(), selected_lang)

            domain = request.META.get("HTTP_HOST") or "api.qa.syntrrix.com"
            if any(lh in domain.lower() for lh in ["127.0.0.1", "localhost", "0.0.0.0"]):
                domain = "api.qa.syntrrix.com"
            scheme = "https"

            def _build_template_payload(h_name=None, h_lang=None, header_fmt=None, body_param_count=0, override_media_url=None):
                t_name = h_name or template_name
                l_code = h_lang or language_code
                fmt = (header_fmt or (tpl_db.format if tpl_db else "text")).lower()

                target_media_url = override_media_url or custom_header_url or (tpl_db.header_media.url if tpl_db and tpl_db.header_media else "")
                if target_media_url:
                    low_url = str(target_media_url).lower()
                    if any(ext in low_url for ext in [".mp4", ".mov", ".avi", ".webm", ".3gp"]) or "video" in low_url:
                        fmt = "video"
                    elif any(ext in low_url for ext in [".pdf", ".doc", ".docx"]) or "document" in low_url:
                        fmt = "document"
                    elif any(ext in low_url for ext in [".jpg", ".jpeg", ".png", ".webp"]) or "image" in low_url:
                        fmt = "image"
                components = []

                if fmt == "image":
                    img_link = _sanitize_meta_media_url(override_media_url or custom_header_url, domain)
                    if not img_link and tpl_db and tpl_db.header_media:
                        try:
                            m_path = tpl_db.header_media.url
                            raw_link = f"{scheme}://{domain}{m_path}" if not m_path.startswith("http") else m_path
                            img_link = _sanitize_meta_media_url(raw_link, domain)
                        except Exception:
                            pass
                    if not img_link or any(lh in img_link.lower() for lh in ["127.0.0.1", "localhost", "0.0.0.0"]):
                        img_link = "https://images.unsplash.com/photo-1579546929518-9e396f3cc809?w=800&fm=jpg&fit=crop&q=80"

                    components.append({
                        "type": "header",
                        "parameters": [
                            {
                                "type": "image",
                                "image": {
                                    "link": img_link
                                }
                            }
                        ]
                    })
                elif fmt == "video":
                    video_link = _sanitize_meta_media_url(override_media_url or custom_header_url, domain)
                    if not video_link and tpl_db and tpl_db.header_media:
                        try:
                            m_path = tpl_db.header_media.url
                            raw_link = f"{scheme}://{domain}{m_path}" if not m_path.startswith("http") else m_path
                            video_link = _sanitize_meta_media_url(raw_link, domain)
                        except Exception:
                            pass
                    if not video_link or any(lh in video_link.lower() for lh in ["127.0.0.1", "localhost", "0.0.0.0"]):
                        video_link = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"

                    components.append({
                        "type": "header",
                        "parameters": [
                            {
                                "type": "video",
                                "video": {
                                    "link": video_link
                                }
                            }
                        ]
                    })
                elif fmt == "document":
                    doc_link = _sanitize_meta_media_url(override_media_url or custom_header_url, domain)
                    if not doc_link and tpl_db and tpl_db.header_media:
                        try:
                            m_path = tpl_db.header_media.url
                            raw_link = f"{scheme}://{domain}{m_path}" if not m_path.startswith("http") else m_path
                            doc_link = _sanitize_meta_media_url(raw_link, domain)
                        except Exception:
                            pass
                    if not doc_link or any(lh in doc_link.lower() for lh in ["127.0.0.1", "localhost", "0.0.0.0"]):
                        doc_link = "https://pdfobject.com/pdf/sample.pdf"

                    components.append({
                        "type": "header",
                        "parameters": [
                            {
                                "type": "document",
                                "document": {
                                    "link": doc_link,
                                    "filename": "Sample_Document.pdf"
                                }
                            }
                        ]
                    })

                # Determine body parameter count
                var_count = body_param_count
                if var_count == 0 and tpl_db and tpl_db.body:
                    matches = re.findall(r"\{\{(\d+)\}\}", tpl_db.body)
                    var_count = len(set(matches))

                if var_count > 0:
                    bp_list = []
                    bp_dict = custom_body_params or (tpl_db.body_params if tpl_db and tpl_db.body_params else {}) or {}
                    for i in range(1, var_count + 1):
                        val = bp_dict.get(str(i)) or bp_dict.get(i) or f"Sample {i}"
                        bp_list.append({"type": "text", "text": str(val)})
                    components.append({
                        "type": "body",
                        "parameters": bp_list
                    })

                tpl_obj = {
                    "name": t_name,
                    "language": {
                        "code": l_code
                    }
                }
                if components:
                    tpl_obj["components"] = components

                return {
                    "type": "template",
                    "template": tpl_obj
                }

            payload = _build_template_payload()
            ok, err_msg = _send_whatsapp_message_raw(config, test_number, payload)

            # Auto-Retry Error 131053: Media download failed / 403 Forbidden / Private IP (127.0.0.1)
            if not ok and err_msg and ("131053" in str(err_msg) or "Downloading media" in str(err_msg) or "Forbidden" in str(err_msg) or "private" in str(err_msg).lower()):
                logger.warning(f"WhatsApp Test Connection: Received Meta Media Download Error 131053 ({err_msg}). Retrying with public CDN media URL...")
                cdn_fallback_url = "https://images.unsplash.com/photo-1579546929518-9e396f3cc809?w=800&fm=jpg&fit=crop&q=80"
                retry_cdn_payload = _build_template_payload(override_media_url=cdn_fallback_url)
                ok, err_msg = _send_whatsapp_message_raw(config, test_number, retry_cdn_payload)

            # Auto-Retry Error 132001: Language code translation mismatch or template missing on WABA
            if not ok and err_msg and ("132001" in str(err_msg) or "translation" in str(err_msg).lower() or "does not exist" in str(err_msg).lower()):
                logger.warning(f"WhatsApp Test Connection: Received Meta Error 132001 ({err_msg}). Retrying with fallback language/approved templates...")
                alt_lang = "en_US" if language_code != "en_US" else "en"
                retry_lang_payload = _build_template_payload(h_lang=alt_lang)
                ok, err_msg = _send_whatsapp_message_raw(config, test_number, retry_lang_payload)

                if not ok:
                    approved_tpl = WhatsAppTemplate.objects.filter(seller_user=request.user, whatsapp_config=config, status='APPROVED').first()
                    if not approved_tpl:
                        approved_tpl = WhatsAppTemplate.objects.filter(seller_user=request.user, status='APPROVED').first()
                    if approved_tpl and approved_tpl.name != template_name:
                        logger.warning(f"WhatsApp Test Connection: Retrying with approved template '{approved_tpl.name}' ({approved_tpl.language})...")
                        app_lang = LANG_ISO_MAP.get(str(approved_tpl.language).lower(), approved_tpl.language or "en_US")
                        tpl_db = approved_tpl
                        retry_approved_payload = _build_template_payload(h_name=approved_tpl.name, h_lang=app_lang)
                        ok, err_msg = _send_whatsapp_message_raw(config, test_number, retry_approved_payload)

            # Smart Auto-Healing: If Meta returns Error #132012 (Format mismatch / missing header/body params), auto-retry with detected format!
            if not ok and err_msg and ("132012" in str(err_msg) or "Format mismatch" in str(err_msg) or "variables" in str(err_msg)):
                logger.warning(f"WhatsApp Test Connection: Initial template send received format mismatch error ({err_msg}). Auto-detecting Meta header/body parameters...")
                err_str = str(err_msg).upper()
                
                detected_fmt = None
                if "EXPECTED IMAGE" in err_str or "IMAGE" in err_str:
                    detected_fmt = "image"
                elif "EXPECTED VIDEO" in err_str or "VIDEO" in err_str:
                    detected_fmt = "video"
                elif "EXPECTED DOCUMENT" in err_str or "DOCUMENT" in err_str:
                    detected_fmt = "document"

                # Check if body variable count mismatch error
                detected_var_count = 0
                if "NUMBER OF LOCALIZABLE VARIABLES" in err_str or "PARAMETERS" in err_str:
                    detected_var_count = 2 # default fallback 2 variables

                retry_payload = _build_template_payload(header_fmt=detected_fmt, body_param_count=detected_var_count)
                ok, err_msg = _send_whatsapp_message_raw(config, test_number, retry_payload)

        else:
            payload = {
                "type": "text",
                "text": {"preview_url": False, "body": "Hello! 👋 This is a test message from Syntrrix (The Intelligent Business Matrix) chatbot integration."}
            }
            ok, err_msg = _send_whatsapp_message_raw(config, test_number, payload)

        if ok:
            from agro_app.models import WhatsAppChatSession, LiveChatSession
            
            # Check if a live chat session ALREADY exists for this customer (do NOT create new one on template send)
            existing_live_sess = LiveChatSession.objects.filter(
                seller_user=request.user,
                customer__mobile_number=test_number
            ).first() or LiveChatSession.objects.filter(
                seller_user=request.user,
                customer__whatsapp_number=test_number
            ).first() or LiveChatSession.objects.filter(
                seller_user=request.user,
                visitor_mobile_number=test_number
            ).first()

            if existing_live_sess:
                if use_template:
                    t_name = template_name or (tpl_db.name if tpl_db else "Test Template")
                    t_body = (tpl_db.body if tpl_db else "") or "Test template message."
                    display_msg = f"[Template Message: {t_name}]\n{t_body}"
                else:
                    display_msg = payload.get("text", {}).get("body", "Hello! 👋 This is a test message from Syntrrix (The Intelligent Business Matrix) chatbot integration.")
                _save_live_chat_message(existing_live_sess, LiveChatMessage.SENDER_SELLER, display_msg)

            # Clear any older stale sessions for this customer to start fresh
            WhatsAppChatSession.objects.filter(seller_user=request.user, customer_phone=test_number).delete()

            # Start a new bot session at the 'start' node (linked to existing_live_sess if present, else None)
            WhatsAppChatSession.objects.create(
                seller_user=request.user,
                customer_phone=test_number,
                current_node_id="start",
                collected_data={},
                live_chat_session=existing_live_sess
            )

            return Response({"success": True, "message": "Test message sent and chatbot flow initiated successfully."})
        return Response({"success": False, "message": err_msg or "Failed to send test message. Check access token and phone number ID."}, status=500)


from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

@method_decorator(csrf_exempt, name='dispatch')
def _process_whatsapp_campaign_status_update(status_type, status_recipient, error_details=None):
    """
    Real-time Meta Webhook Campaign Status Handler.
    Parses 'sent', 'delivered', 'read', and 'failed' statuses and exact Meta error payload details,
    updating recipient logs (WhatsAppCampaignRecipient) and campaign totals dynamically in DB.
    """
    if not status_type or not status_recipient:
        return
    
    clean_phone = str(status_recipient).replace("+", "").replace(" ", "").strip()
    if not clean_phone:
        return

    try:
        from agro_app.models import WhatsAppCampaign, WhatsAppCampaignRecipient
        
        # 1. Match active WhatsAppCampaignRecipient log entries for clean_phone
        recipients = WhatsAppCampaignRecipient.objects.filter(
            phone_number__icontains=clean_phone[-10:]
        ).order_by("-timestamp")[:5]

        for r in recipients:
            if status_type == "sent":
                r.sent_status = True
                r.sent_error = ""
                r.save(update_fields=["sent_status", "sent_error"])

            elif status_type == "delivered":
                r.sent_status = True
                r.delivered_status = True
                r.delivered_error = ""
                r.save(update_fields=["sent_status", "delivered_status", "delivered_error"])

            elif status_type == "read":
                r.sent_status = True
                r.delivered_status = True
                r.read_status = True
                r.save(update_fields=["sent_status", "delivered_status", "read_status"])

            elif status_type == "failed":
                r.sent_status = False
                r.delivered_status = False
                err_msg = error_details or "Message undeliverable by Meta Cloud API"
                r.sent_error = err_msg
                r.delivered_error = err_msg
                r.save(update_fields=["sent_status", "delivered_status", "sent_error", "delivered_error"])

        # 2. Recalibrate parent WhatsAppCampaign metrics
        campaigns = WhatsAppCampaign.objects.order_by("-created_at")[:10]
        for c in campaigns:
            total_aud = c.audience if c.audience > 0 else 1
            
            if status_type == "sent":
                sent_val = int(str(c.sent or "0")) if str(c.sent or "0").isdigit() else 0
                if sent_val < total_aud:
                    c.sent = str(sent_val + 1)
                    c.save(update_fields=["sent", "updated_at"])

            elif status_type == "delivered":
                deliv_str = str(c.delivered or "0").split("/")[0].strip()
                cur_deliv = int(deliv_str) if deliv_str.isdigit() else 0
                if cur_deliv < total_aud:
                    new_deliv = cur_deliv + 1
                    c.delivered = str(new_deliv)
                    c.save(update_fields=["delivered", "updated_at"])

            elif status_type == "read":
                read_str = str(c.read or "0").split("/")[0].strip()
                cur_read = int(read_str) if read_str.isdigit() else 0
                if cur_read < total_aud:
                    new_read = cur_read + 1
                    c.read = str(new_read)
                    c.save(update_fields=["read", "updated_at"])

            elif status_type == "failed":
                fail_str = str(c.failed or "0").strip()
                cur_fail = int(fail_str) if fail_str.isdigit() else 0
                new_fail = cur_fail + 1
                c.failed = str(new_fail)
                c.save(update_fields=["failed", "updated_at"])

    except Exception as err:
        logger.error(f"Error updating WhatsAppCampaign status in Webhook: {err}")


class WhatsAppWebhookAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        from agro_app.models import WhatsAppConfig
        
        # Log the full request path, raw query string, and headers
        full_path = request.get_full_path()
        query_string = request.META.get('QUERY_STRING', '')
        headers_dict = {k: v for k, v in request.META.items() if k.startswith('HTTP_') or k in ['CONTENT_TYPE', 'REQUEST_METHOD']}
        
        logger.info(f"WhatsApp Webhook Raw Request - Path: {full_path}, Query String: {query_string}")
        logger.info(f"WhatsApp Webhook Headers: {json.dumps(headers_dict)}")
        logger.info(f"All GET params: {dict(request.GET)}")
        
        mode = request.GET.get("hub.mode") or request.query_params.get("hub.mode")
        token = request.GET.get("hub.verify_token") or request.query_params.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge") or request.query_params.get("hub.challenge")

        logger.info(f"WhatsApp Webhook Verification Request: mode={mode}, token={token}, challenge={challenge}")

        if mode == "subscribe" and token:
            config_exists = WhatsAppConfig.objects.filter(webhook_verify_token=token).exists()
            if config_exists or token == "SyntrrixWebhook123":
                logger.info("WhatsApp Webhook Verification Successful")
                return HttpResponse(challenge, content_type="text/plain")
            else:
                logger.warning(f"WhatsApp Webhook Verification Failed: Token '{token}' not found in any configurations")
        return HttpResponse("Verification failed", status=403)

    def post(self, request):
        from agro_app.models import WhatsAppConfig
        payload = request.data or {}
        logger.info(f"WhatsApp Webhook Event Payload: {json.dumps(payload)}")

        entries = payload.get("entry") or []
        for entry in entries:
            for change in (entry.get("changes") or []):
                value = change.get("value") or {}
                field = change.get("field")
                
                # Capture Meta Graph API Template Approval/Rejection Webhook Updates:
                if field == "message_template_status_update" or "event" in value or "message_template_id" in value:
                    tpl_event = str(value.get("event") or "").upper()
                    tpl_id = value.get("message_template_id")
                    tpl_name = value.get("message_template_name")
                    tpl_reason = value.get("reason") or ""
                    rejection_info = value.get("rejection_info") or {}
                    rej_detail = rejection_info.get("reason") or rejection_info.get("recommendation") or tpl_reason

                    full_reason = f"{tpl_reason}: {rej_detail}" if (tpl_reason and rej_detail and tpl_reason not in str(rej_detail)) else (rej_detail or tpl_reason)

                    logger.info(f"WhatsApp Webhook Template Update: Name='{tpl_name}', Meta ID={tpl_id}, Status='{tpl_event}', Rejection Reason='{full_reason}'")

                    from agro_app.models import WhatsAppTemplate
                    matching_templates = WhatsAppTemplate.objects.none()
                    if tpl_id:
                        matching_templates = WhatsAppTemplate.objects.filter(meta_template_id=str(tpl_id))
                    if not matching_templates.exists() and tpl_name:
                        matching_templates = WhatsAppTemplate.objects.filter(name__iexact=str(tpl_name))

                    for rec in matching_templates:
                        if tpl_event:
                            rec.status = tpl_event
                        if tpl_event in ["REJECTED", "DISABLED", "PAUSED"]:
                            rec.rejected_reason = full_reason or "INVALID_FORMAT: Missing text samples in header, body, or buttons."
                        elif tpl_event == "APPROVED":
                            rec.rejected_reason = ""
                        rec.save()

                # Capture and log status delivery failures/updates from Meta and update campaign metrics in real-time
                statuses = value.get("statuses") or []
                for status in statuses:
                    status_id = status.get("id")
                    status_type = str(status.get("status") or "").lower()
                    status_recipient = status.get("recipient_id")
                    status_errors = status.get("errors") or []

                    err_str = None
                    if status_errors:
                        err_parts = []
                        for err_item in status_errors:
                            c_code = err_item.get("code")
                            c_title = err_item.get("title") or err_item.get("message") or ""
                            c_details = err_item.get("error_data", {}).get("details") or ""
                            part = f"[{c_code}] {c_title}"
                            if c_details and c_details != c_title:
                                part += f": {c_details}"
                            err_parts.append(part)
                        err_str = " | ".join(err_parts)
                        logger.error(f"WhatsApp Message Delivery FAILED to {status_recipient} (Msg ID: {status_id}). Errors: {err_str}")
                        _process_whatsapp_campaign_status_update("failed", status_recipient, error_details=err_str)
                    else:
                        logger.info(f"WhatsApp Status Update: {status_recipient} is {status_type} (Msg ID: {status_id})")
                        _process_whatsapp_campaign_status_update(status_type, status_recipient)

                metadata = value.get("metadata") or {}
                recipient_number_id = metadata.get("phone_number_id")

                try:
                    config = WhatsAppConfig.objects.select_related("seller_user").get(phone_number_id=recipient_number_id, is_connected=True)
                except WhatsAppConfig.DoesNotExist:
                    logger.warning(f"WhatsApp Webhook POST: No connected WhatsAppConfig found for phone_number_id '{recipient_number_id}'")
                    continue

                messages = value.get("messages") or []
                for msg in messages:
                    customer_phone = msg.get("from")
                    if not customer_phone:
                        continue

                    contacts = value.get("contacts") or []
                    customer_name = ""
                    if contacts:
                        customer_name = contacts[0].get("profile", {}).get("name") or ""

                    msg_type = msg.get("type")
                    msg_text = ""
                    media_payload = None

                    if msg_type == "text":
                        msg_text = msg.get("text", {}).get("body") or ""
                    elif msg_type == "interactive":
                        interactive = msg.get("interactive") or {}
                        int_type = interactive.get("type")
                        if int_type == "button_reply":
                            b_reply = interactive.get("button_reply", {})
                            msg_text = b_reply.get("title") or b_reply.get("id") or ""
                        elif int_type == "list_reply":
                            l_reply = interactive.get("list_reply", {})
                            msg_text = l_reply.get("title") or l_reply.get("id") or ""
                    elif msg_type == "button":
                        # Meta Cloud API Template Quick Reply Button Response Event
                        btn_obj = msg.get("button") or {}
                        msg_text = btn_obj.get("text") or btn_obj.get("payload") or ""
                        logger.info(f"WhatsApp Webhook: Customer {customer_phone} clicked Template Quick Reply button '{msg_text}'")
                    elif msg_type in ["image", "video", "document", "audio"]:
                        media_id = msg.get(msg_type, {}).get("id")
                        mime_type = msg.get(msg_type, {}).get("mime_type") or ""
                        filename = msg.get(msg_type, {}).get("filename") or f"upload.{msg_type}"

                        media_url = _fetch_meta_media_url(config, media_id)
                        media_payload = {
                            "file_url": media_url,
                            "file_name": filename,
                            "mime_type": mime_type,
                        }
                        msg_text = f"Uploaded file: {filename}"
                    else:
                        continue

                    _process_whatsapp_customer_message(config, customer_phone, customer_name, msg_text, media_payload)

        return Response({"success": True}, status=200)


def _bg_dispatch_bulk_campaign(campaign_id, seller_user_id, config_id, recipients, template_name, language_code, use_template, custom_text, tpl_db_id=None, domain="api.qa.syntrrix.com", scheme="https", custom_header_url=None, var_mapping=None):
    """
    Asynchronous Background Worker Thread for WhatsApp Bulk Campaign Dispatch.
    Loops through recipients, sends Meta Cloud API messages, updates DB recipient statuses,
    and updates campaign counts (sent, failed, delivered) in real time.
    """
    import threading, time, re
    from agro_app.models import WhatsAppCampaign, WhatsAppCampaignRecipient, WhatsAppConfig, WhatsAppTemplate, User, WhatsAppChatSession, LiveChatSession

    logger.info(f"Starting Background Campaign Dispatch for Campaign #{campaign_id} ({len(recipients)} recipients)...")

    try:
        campaign = WhatsAppCampaign.objects.get(pk=campaign_id)
        config = WhatsAppConfig.objects.filter(pk=config_id).first()
        seller_user = User.objects.get(pk=seller_user_id)
        tpl_db = WhatsAppTemplate.objects.filter(pk=tpl_db_id).first() if tpl_db_id else None

        def _build_payload(phone_num, item=None, header_fmt=None, lang_override=None):
            if not use_template:
                return {
                    "type": "text",
                    "text": {"preview_url": False, "body": custom_text}
                }
            import os
            from django.conf import settings

            l_code = lang_override or language_code
            fmt = (header_fmt or (tpl_db.format if tpl_db else "text")).lower()

            target_media_url = custom_header_url or (tpl_db.header_media.url if tpl_db and tpl_db.header_media else "")
            if target_media_url:
                low_url = str(target_media_url).lower()
                if any(ext in low_url for ext in [".mp4", ".mov", ".avi", ".webm", ".3gp"]) or "video" in low_url:
                    fmt = "video"
                elif any(ext in low_url for ext in [".pdf", ".doc", ".docx"]) or "document" in low_url:
                    fmt = "document"
                elif any(ext in low_url for ext in [".jpg", ".jpeg", ".png", ".webp"]) or "image" in low_url:
                    fmt = "image"
            components = []

            if fmt == "image":
                img_link = _sanitize_meta_media_url(custom_header_url, domain)
                if not img_link and tpl_db and tpl_db.header_media:
                    try:
                        m_path = tpl_db.header_media.url
                        rel_name = tpl_db.header_media.name
                        abs_disk_path = os.path.join(settings.MEDIA_ROOT, rel_name)
                        if os.path.exists(abs_disk_path) and os.path.getsize(abs_disk_path) > 0:
                            raw_link = f"{scheme}://{domain}{m_path}" if not m_path.startswith("http") else m_path
                            img_link = _sanitize_meta_media_url(raw_link, domain)
                    except Exception:
                        pass
                if not img_link or any(lh in img_link.lower() for lh in ["127.0.0.1", "localhost", "0.0.0.0"]):
                    img_link = "https://images.unsplash.com/photo-1579546929518-9e396f3cc809?w=800&fm=jpg&fit=crop&q=80"
                components.append({"type": "header", "parameters": [{"type": "image", "image": {"link": img_link}}]})
            elif fmt == "video":
                video_link = _sanitize_meta_media_url(custom_header_url, domain)
                if not video_link and tpl_db and tpl_db.header_media:
                    try:
                        m_path = tpl_db.header_media.url
                        rel_name = tpl_db.header_media.name
                        abs_disk_path = os.path.join(settings.MEDIA_ROOT, rel_name)
                        if os.path.exists(abs_disk_path) and os.path.getsize(abs_disk_path) > 0:
                            raw_link = f"{scheme}://{domain}{m_path}" if not m_path.startswith("http") else m_path
                            video_link = _sanitize_meta_media_url(raw_link, domain)
                    except Exception:
                        pass
                if not video_link or any(lh in video_link.lower() for lh in ["127.0.0.1", "localhost", "0.0.0.0"]):
                    video_link = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"
                components.append({"type": "header", "parameters": [{"type": "video", "video": {"link": video_link}}]})
            elif fmt == "document":
                doc_link = _sanitize_meta_media_url(custom_header_url, domain)
                if not doc_link and tpl_db and tpl_db.header_media:
                    try:
                        m_path = tpl_db.header_media.url
                        rel_name = getattr(tpl_db.header_media, 'name', '')
                        abs_disk_path = os.path.join(settings.MEDIA_ROOT, rel_name) if rel_name else ""
                        if abs_disk_path and os.path.exists(abs_disk_path) and os.path.getsize(abs_disk_path) > 0:
                            raw_link = f"{scheme}://{domain}{m_path}" if not m_path.startswith("http") else m_path
                            doc_link = _sanitize_meta_media_url(raw_link, domain)
                    except Exception:
                        pass
                if not doc_link or any(lh in doc_link.lower() for lh in ["127.0.0.1", "localhost", "0.0.0.0"]):
                    doc_link = "https://pdfobject.com/pdf/sample.pdf"
                components.append({"type": "header", "parameters": [{"type": "document", "document": {"link": doc_link, "filename": "Document.pdf"}}]})

            # Handle body variable mapping
            matches = re.findall(r"\{\{(\d+)\}\}", tpl_db.body) if (tpl_db and tpl_db.body) else []
            var_count = len(set(matches))
            if var_count > 0:
                bp_list = []
                vm = var_mapping or {}
                for i in range(1, var_count + 1):
                    col_key = vm.get(str(i)) or vm.get(i)
                    val = item.get(col_key) if (col_key and isinstance(item, dict)) else None
                    if not val and isinstance(item, dict):
                        if i == 1:
                            val = item.get("name") or item.get("customer_name") or "Customer"
                        elif i == 2:
                            val = item.get("order") or item.get("email") or "Order"
                        else:
                            val = f"Value {i}"
                    bp_list.append({"type": "text", "text": str(val or f"Sample {i}")})
                components.append({"type": "body", "parameters": bp_list})

            tpl_obj = {"name": template_name, "language": {"code": l_code}}
            if components:
                tpl_obj["components"] = components

            return {"type": "template", "template": tpl_obj}

        success_count = 0
        failed_count = 0
        total_aud = len(recipients)

        for item in recipients:
            phone = str(item.get("phone") or item.get("phone_number") or "").strip()
            name = str(item.get("name") or item.get("customer_name") or "Recipient").strip()
            if not phone:
                failed_count += 1
                continue

            payload = _build_payload(phone, item=item)
            ok, err_msg = _send_whatsapp_message_raw(config, phone, payload)

            # Auto-Retry Error 132001
            if not ok and err_msg and ("132001" in str(err_msg) or "translation" in str(err_msg).lower()):
                alt_lang = "en" if language_code == "en_US" else "en_US"
                retry_payload = _build_payload(phone, item=item, lang_override=alt_lang)
                ok, err_msg = _send_whatsapp_message_raw(config, phone, retry_payload)

            # Auto-Retry Error 132012
            if not ok and err_msg and ("132012" in str(err_msg) or "Format mismatch" in str(err_msg) or "expected" in str(err_msg).lower()):
                err_str = str(err_msg).upper()
                detected_fmt = None
                if "EXPECTED IMAGE" in err_str or "IMAGE" in err_str:
                    detected_fmt = "image"
                elif "EXPECTED VIDEO" in err_str or "VIDEO" in err_str:
                    detected_fmt = "video"
                elif "EXPECTED DOCUMENT" in err_str or "DOCUMENT" in err_str:
                    detected_fmt = "document"

                if detected_fmt:
                    retry_fmt_payload = _build_payload(phone, item=item, header_fmt=detected_fmt)
                    ok, err_msg = _send_whatsapp_message_raw(config, phone, retry_fmt_payload)

            # Match or create recipient log object
            from django.utils import timezone
            now_dispatch_time = timezone.now()

            rec_obj = WhatsAppCampaignRecipient.objects.filter(campaign=campaign, phone_number=phone).first()
            if not rec_obj:
                rec_obj = WhatsAppCampaignRecipient.objects.create(
                    campaign=campaign,
                    phone_number=phone,
                    customer_name=name,
                    timestamp=now_dispatch_time,
                )

            if ok:
                success_count += 1
                rec_obj.sent_status = True
                rec_obj.delivered_status = True
                rec_obj.sent_error = ""
                rec_obj.delivered_error = ""
                rec_obj.timestamp = now_dispatch_time
                rec_obj.save(update_fields=["sent_status", "delivered_status", "sent_error", "delivered_error", "timestamp"])

                try:
                    # Check if a live chat session ALREADY exists for this recipient (do NOT create new one on campaign send)
                    existing_live_sess = LiveChatSession.objects.filter(
                        seller_user=seller_user,
                        customer__mobile_number=phone
                    ).first() or LiveChatSession.objects.filter(
                        seller_user=seller_user,
                        customer__whatsapp_number=phone
                    ).first() or LiveChatSession.objects.filter(
                        seller_user=seller_user,
                        visitor_mobile_number=phone
                    ).first()

                    if existing_live_sess:
                        t_name = template_name or (tpl_db.name if tpl_db else "Campaign Template")
                        t_body = (tpl_db.body if tpl_db else custom_text) or "Campaign message"
                        display_msg = f"[Template Message: {t_name}]\n{t_body}" if use_template else (custom_text or "Campaign message")
                        _save_live_chat_message(existing_live_sess, LiveChatMessage.SENDER_SELLER, display_msg)

                    WhatsAppChatSession.objects.filter(seller_user=seller_user, customer_phone=phone).delete()
                    WhatsAppChatSession.objects.create(
                        seller_user=seller_user,
                        customer_phone=phone,
                        customer_name=name,
                        current_node_id="start",
                        collected_data={},
                        live_chat_session=existing_live_sess
                    )
                except Exception as tpl_ex:
                    logger.error(f"Error preparing chatbot session for campaign recipient {phone}: {tpl_ex}")
            else:
                failed_count += 1
                rec_obj.sent_status = False
                rec_obj.sent_error = err_msg or "Failed to dispatch template via Meta API"
                rec_obj.timestamp = now_dispatch_time
                rec_obj.save(update_fields=["sent_status", "sent_error", "timestamp"])

            # Real-time DB campaign counters update
            campaign.sent = str(success_count)
            campaign.failed = str(failed_count)
            campaign.delivered = f"{success_count}/{total_aud}"
            campaign.save(update_fields=["sent", "failed", "delivered", "updated_at"])

            time.sleep(0.05)

        # Finalize Campaign Status: Running -> Completed
        final_status = "Completed" if (success_count > 0 or failed_count == 0) else "Failed"
        campaign.status = final_status
        campaign.sent = str(success_count)
        campaign.failed = str(failed_count)
        campaign.delivered = f"{success_count}/{total_aud}"
        campaign.save(update_fields=["status", "sent", "failed", "delivered", "updated_at"])
        logger.info(f"Background Campaign Dispatch Finished for #{campaign_id}! Final Status: {final_status}, Sent: {success_count}, Failed: {failed_count}")

    except Exception as ex:
        import traceback
        logger.error(f"Error in background campaign worker for #{campaign_id}: {ex}\n{traceback.format_exc()}")


_scheduled_checker_started = False
_scheduled_checker_lock = threading.Lock()


def check_and_dispatch_scheduled_campaigns():
    """
    Checks DB for any WhatsAppCampaign records with status='Scheduled'
    where scheduled_at <= current time, and launches background dispatch worker.
    Uses atomic SQL status update (UPDATE ... WHERE status='Scheduled') to guarantee
    EXACTLY ONCE execution across concurrent threads and processes.
    """
    try:
        from django.utils import timezone
        from agro_app.models import WhatsAppCampaign

        now = timezone.now()
        due_ids = list(WhatsAppCampaign.objects.filter(
            status="Scheduled",
            scheduled_at__lte=now
        ).values_list("campaign_id", flat=True))

        for cid in due_ids:
            # ATOMIC CLAIM: Update status='Running' ONLY if status is still 'Scheduled' at SQL level
            claimed_count = WhatsAppCampaign.objects.filter(
                campaign_id=cid,
                status="Scheduled"
            ).update(status="Running", updated_at=now)

            if claimed_count == 1:
                # Exactly one process/thread succeeded in claiming this campaign!
                campaign = WhatsAppCampaign.objects.get(pk=cid)
                logger.info(f"Scheduled Campaign #{campaign.campaign_id} '{campaign.title}' ATOMICALLY CLAIMED! Launching background worker...")

                payload = campaign.scheduled_payload or {}
                seller_user_id = payload.get("seller_user_id") or campaign.seller_user_id
                config_id = payload.get("config_id")
                recipients = payload.get("recipients") or []
                template_name = payload.get("template_name") or campaign.template_name
                language_code = payload.get("language_code") or "en_US"
                use_template = payload.get("use_template", True)
                custom_text = payload.get("custom_text") or ""
                tpl_db_id = payload.get("tpl_db_id")
                domain = payload.get("domain") or "api.qa.syntrrix.com"
                scheme = payload.get("scheme") or "https"
                custom_header_url = payload.get("custom_header_url")
                var_mapping = payload.get("var_mapping") or {}

                t = threading.Thread(
                    target=_bg_dispatch_bulk_campaign,
                    kwargs={
                        "campaign_id": campaign.campaign_id,
                        "seller_user_id": seller_user_id,
                        "config_id": config_id,
                        "recipients": recipients,
                        "template_name": template_name,
                        "language_code": language_code,
                        "use_template": use_template,
                        "custom_text": custom_text,
                        "tpl_db_id": tpl_db_id,
                        "domain": domain,
                        "scheme": scheme,
                        "custom_header_url": custom_header_url,
                        "var_mapping": var_mapping,
                    },
                    daemon=True
                )
                t.start()
            else:
                logger.info(f"Scheduled Campaign #{cid} was already claimed by another worker thread. Skipping duplicate execution.")
    except Exception as ex:
        logger.error(f"Error in check_and_dispatch_scheduled_campaigns: {ex}")


def start_scheduled_campaign_checker_daemon():
    global _scheduled_checker_started
    with _scheduled_checker_lock:
        if _scheduled_checker_started:
            return
        _scheduled_checker_started = True

    import os, sys
    # Avoid starting thread in Django auto-reloader parent process
    if os.environ.get("RUN_MAIN") != "true" and "runserver" in getattr(sys, "argv", []):
        logger.info("Skipping scheduler daemon in Django auto-reloader parent process.")
        return

    def _loop():
        import time
        logger.info("Started WhatsApp Scheduled Campaign Background Checker Loop.")
        while True:
            try:
                check_and_dispatch_scheduled_campaigns()
            except Exception as e:
                logger.error(f"Error in scheduled campaign loop: {e}")
            time.sleep(15)

    t = threading.Thread(target=_loop, daemon=True, name="WhatsAppCampaignScheduler")
    t.start()


class WhatsAppBulkTemplateAPIView(APIView):
    """
    WhatsApp Bulk Template / Campaign Dispatch API.
    Creates a WhatsAppCampaign record in DB instantly with status='Running' or status='Scheduled',
    launches a non-blocking background thread or schedules execution, and returns HTTP 200 immediately.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from agro_app.models import WhatsAppConfig, WhatsAppCampaign, WhatsAppCampaignRecipient, WhatsAppTemplate
        from django.db.models import Q

        seller_user = request.user
        config = None
        config_id = request.data.get("config_id") or request.data.get("whatsapp_config_id")
        business_number_str = str(request.data.get("business_number") or request.data.get("phone_number") or "").strip()
        channel_name_str = str(request.data.get("channel_name") or "").strip()
        raw_template_name = (request.data.get("template_name") or "hello_world").strip()

        # Step 1: Find template DB record first
        tpl_db = WhatsAppTemplate.objects.filter(seller_user=seller_user).filter(
            Q(name__iexact=raw_template_name) | Q(header_text__iexact=raw_template_name)
        ).first()

        config_qs = WhatsAppConfig.objects.filter(seller_user=seller_user)

        # Step 2: Lookup config by config_id / whatsapp_config_id
        if config_id:
            config = config_qs.filter(whatsapp_config_id=config_id).first() or \
                     config_qs.filter(phone_number_id=config_id).first() or \
                     config_qs.filter(pk=config_id if str(config_id).isdigit() else None).first()

        # Step 3: Lookup config by business_number string or channel_name string
        if not config and (business_number_str or channel_name_str):
            for c in config_qs:
                p_num = str(c.phone_number or "").strip()
                c_name = str(c.channel_name or "").strip()
                if p_num and p_num in business_number_str:
                    config = c
                    break
                if c_name and (c_name in business_number_str or c_name in channel_name_str):
                    config = c
                    break

        # Step 4: If config is still not resolved, use tpl_db's linked whatsapp_config!
        if not config and tpl_db and tpl_db.whatsapp_config:
            config = tpl_db.whatsapp_config

        # Step 5: Fallback to connected config
        if not config:
            config = config_qs.filter(is_connected=True).exclude(phone_number_id="").exclude(access_token="").first() or config_qs.first()

        if not config:
            return Response({"success": False, "message": "No connected WhatsApp configuration found."}, status=404)

        raw_language_code = (request.data.get("language_code") or "en_US").strip()
        use_template = request.data.get("use_template", True)
        custom_text = (request.data.get("custom_text") or "Hello! 👋 This is a test message from Syntrrix chatbot integration.").strip()
        recipients = request.data.get("recipients") or []

        if not isinstance(recipients, list) or not recipients:
            return Response({"success": False, "message": "recipients must be a non-empty list."}, status=400)

        if tpl_db:
            template_name = tpl_db.name.strip()
            language_code = tpl_db.language.strip() if tpl_db.language else raw_language_code
        else:
            template_name = raw_template_name.lower().replace(" ", "_")
            language_code = raw_language_code

        domain = request.META.get("HTTP_HOST") or "api.qa.syntrrix.com"
        scheme = "https" if (request.is_secure() or "api.qa.syntrrix.com" in domain) else "http"

        total_aud = len(recipients)
        campaign_title = str(request.data.get("campaign_title") or request.data.get("title") or f"Campaign · {template_name}").strip()
        subtitle = str(request.data.get("subtitle") or f"Wizard · {template_name}").strip()
        author_str = str(request.data.get("author") or "By you (Wizard)").strip()
        bus_num = str(request.data.get("business_number") or f"{config.channel_name or 'Syntrrix Main'} · +{config.phone_number_id or '919000012345'}").strip()

        custom_header_url = request.data.get("header_media_url") or request.data.get("media_url")
        var_mapping = request.data.get("var_mapping") or {}

        if custom_header_url and tpl_db:
            try:
                if "/media/" in str(custom_header_url):
                    rel_p = str(custom_header_url).split("/media/")[-1]
                    if rel_p and tpl_db.header_media.name != rel_p:
                        tpl_db.header_media.name = rel_p
                        tpl_db.save(update_fields=['header_media'])
                        logger.info(f"Updated WhatsAppTemplate #{tpl_db.template_id} header_media to '{rel_p}' before campaign dispatch.")
            except Exception as update_ex:
                logger.error(f"Error updating tpl_db header_media in bulk template view: {update_ex}")

        # Delivery & Schedule Parsing (Indian Standard Time - Asia/Kolkata UTC+05:30)
        send_option = str(request.data.get("send_option") or request.data.get("delivery_option") or "now").strip().lower()
        raw_scheduled_at = request.data.get("scheduled_at") or request.data.get("scheduled_time") or request.data.get("scheduledDateTime")

        scheduled_dt = None
        if send_option == "schedule" or raw_scheduled_at:
            if raw_scheduled_at:
                from django.utils.dateparse import parse_datetime
                from django.utils import timezone
                import datetime, zoneinfo
                from datetime import timezone as py_timezone, timedelta as py_timedelta

                try:
                    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
                except Exception:
                    ist_tz = py_timezone(py_timedelta(hours=5, minutes=30))

                if isinstance(raw_scheduled_at, str):
                    parsed = parse_datetime(raw_scheduled_at)
                    if not parsed:
                        try:
                            parsed = datetime.datetime.fromisoformat(raw_scheduled_at)
                        except Exception:
                            pass
                    if parsed:
                        if timezone.is_naive(parsed):
                            scheduled_dt = parsed.replace(tzinfo=ist_tz)
                        else:
                            scheduled_dt = parsed.astimezone(ist_tz)

        is_scheduled = (send_option == "schedule") and (scheduled_dt is not None)
        initial_status = "Scheduled" if is_scheduled else "Running"

        scheduled_payload_data = {
            "seller_user_id": seller_user.id,
            "config_id": config.pk,
            "recipients": recipients,
            "template_name": template_name,
            "language_code": language_code,
            "use_template": use_template,
            "custom_text": custom_text,
            "tpl_db_id": tpl_db.pk if tpl_db else None,
            "domain": domain,
            "scheme": scheme,
            "custom_header_url": custom_header_url,
            "var_mapping": var_mapping,
        }

        # 1. Create WhatsAppCampaign in DB
        from agro_app.models import WhatsAppCampaign, WhatsAppCampaignRecipient
        campaign_kwargs = {
            "seller_user": seller_user,
            "title": campaign_title,
            "status": initial_status,
            "subtitle": subtitle,
            "business_number": bus_num,
            "template_name": template_name,
            "channel_name": config.channel_name or "WhatsApp",
            "audience": total_aud,
            "sent": "0",
            "delivered": f"0/{total_aud}",
            "read": f"0/{total_aud}",
            "failed": "0",
            "author": author_str,
        }
        if hasattr(WhatsAppCampaign, "scheduled_at"):
            campaign_kwargs["scheduled_at"] = scheduled_dt if is_scheduled else None
            campaign_kwargs["scheduled_payload"] = scheduled_payload_data if is_scheduled else {}

        campaign = WhatsAppCampaign.objects.create(**campaign_kwargs)

        # 2. Bulk-create recipient entries in DB
        recipient_objs = []
        for item in recipients:
            p_num = str(item.get("phone") or item.get("phone_number") or "").strip()
            if p_num:
                c_name = str(item.get("name") or item.get("customer_name") or "Recipient").strip()
                recipient_objs.append(
                    WhatsAppCampaignRecipient(
                        campaign=campaign,
                        phone_number=p_num,
                        customer_name=c_name,
                        sent_status=False,
                    )
                )
        if recipient_objs:
            WhatsAppCampaignRecipient.objects.bulk_create(recipient_objs)

        if is_scheduled:
            formatted_dt_str = scheduled_dt.strftime("%d %b %Y, %I:%M %p IST")
            return Response({
                "success": True,
                "campaign_id": campaign.campaign_id,
                "status": "Scheduled",
                "scheduled_at": scheduled_dt.isoformat(),
                "message": f"Bulk campaign '{campaign.title}' scheduled for {formatted_dt_str} for {total_aud} recipients.",
                "data": {
                    "id": campaign.campaign_id,
                    "title": campaign.title,
                    "status": "Scheduled",
                    "audience": total_aud,
                    "sent": "0",
                    "delivered": f"0/{total_aud}",
                    "read": f"0/{total_aud}",
                    "failed": "0",
                    "date": campaign.created_at.strftime("%m/%d/%Y"),
                    "scheduled_at": scheduled_dt.isoformat(),
                }
            }, status=200)

        # 3. Immediate background dispatch thread
        t = threading.Thread(
            target=_bg_dispatch_bulk_campaign,
            kwargs={
                "campaign_id": campaign.campaign_id,
                "seller_user_id": seller_user.id,
                "config_id": config.pk,
                "recipients": recipients,
                "template_name": template_name,
                "language_code": language_code,
                "use_template": use_template,
                "custom_text": custom_text,
                "tpl_db_id": tpl_db.pk if tpl_db else None,
                "domain": domain,
                "scheme": scheme,
                "custom_header_url": custom_header_url,
                "var_mapping": var_mapping,
            },
            daemon=True
        )
        t.start()

        # 4. Instant HTTP 200 Response
        return Response({
            "success": True,
            "campaign_id": campaign.campaign_id,
            "status": "Running",
            "message": f"Bulk campaign '{campaign.title}' initiated for {total_aud} recipients. Running in background.",
            "data": {
                "id": campaign.campaign_id,
                "title": campaign.title,
                "status": "Running",
                "audience": total_aud,
                "sent": "0",
                "delivered": f"0/{total_aud}",
                "read": f"0/{total_aud}",
                "failed": "0",
                "date": campaign.created_at.strftime("%m/%d/%Y"),
            }
        }, status=200)


# ==========================================
# AUTH, SELLER & PLAN MANAGEMENT API VIEWS
# ==========================================

class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            user = serializer.validated_data["user"]
            refresh = RefreshToken.for_user(user)
            user_data = UserDetailSerializer(user).data
            response_payload = {
                "success": True,
                "message": "Login successful.",
                "token_type": "Bearer",
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": user_data,
            }
            return Response(response_payload, status=status.HTTP_200_OK)
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:
            logger.exception("Unexpected error during login")
            err_msg = str(exc) if getattr(settings, "DEBUG", False) else "Unexpected error occurred. Please try again later."
            return Response(
                {
                    "success": False,
                    "message": err_msg,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class BuyerAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        buyers = User.objects.filter(role_id=UserRole.ROLE_BUYER).select_related("role", "meta").order_by("-created_at")
        serializer = UserDetailSerializer(buyers, many=True)
        return Response({"success": True, "count": len(buyers), "data": serializer.data}, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = BuyerRegistrationSerializer(data=request.data)
        if serializer.is_valid():
            buyer = serializer.save()
            return Response({"success": True, "message": "Buyer created successfully.", "user_id": buyer.id}, status=status.HTTP_201_CREATED)
        return Response({"success": False, "errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


class PlanAPIView(APIView):
    """
    GET  -> List all created plans
    POST -> Create a new plan
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        plans = Plan.objects.all().order_by("-created_at")
        serializer = PlanSerializer(plans, many=True)
        return Response({"success": True, "count": len(plans), "data": serializer.data}, status=status.HTTP_200_OK)

    def post(self, request):
        if request.user.role.role_id != UserRole.ROLE_ADMIN:
            return Response({"success": False, "message": "Only admin can create plans."}, status=status.HTTP_403_FORBIDDEN)
        
        name = (request.data.get("name") or "").strip()
        features = request.data.get("features") or []
        description = (request.data.get("description") or "").strip()

        if not name:
            return Response({"success": False, "message": "Plan name is required."}, status=status.HTTP_400_BAD_REQUEST)

        if Plan.objects.filter(name__iexact=name).exists():
            return Response({"success": False, "message": f"Plan '{name}' already exists."}, status=status.HTTP_400_BAD_REQUEST)

        plan = Plan.objects.create(
            name=name,
            features=features if isinstance(features, list) else [],
            description=description,
        )
        return Response({
            "success": True,
            "message": "Plan created successfully.",
            "data": PlanSerializer(plan).data,
        }, status=status.HTTP_201_CREATED)


class PlanDetailAPIView(APIView):
    """
    GET       -> Get plan details by pk
    PUT/PATCH -> Update plan
    DELETE    -> Delete plan
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        try:
            plan = Plan.objects.get(pk=pk)
            return Response({"success": True, "data": PlanSerializer(plan).data}, status=status.HTTP_200_OK)
        except Plan.DoesNotExist:
            return Response({"success": False, "message": "Plan not found."}, status=status.HTTP_404_NOT_FOUND)

    def put(self, request, pk=None):
        return self.patch(request, pk)

    def patch(self, request, pk=None):
        if request.user.role.role_id != UserRole.ROLE_ADMIN:
            return Response({"success": False, "message": "Only admin can edit plans."}, status=status.HTTP_403_FORBIDDEN)
        try:
            plan = Plan.objects.get(pk=pk)
        except Plan.DoesNotExist:
            return Response({"success": False, "message": "Plan not found."}, status=status.HTTP_404_NOT_FOUND)

        data = request.data or {}
        name = (data.get("name") or "").strip()
        if name and name.lower() != plan.name.lower():
            if Plan.objects.filter(name__iexact=name).exclude(pk=pk).exists():
                return Response({"success": False, "message": f"Plan '{name}' already exists."}, status=status.HTTP_400_BAD_REQUEST)
            plan.name = name

        if "features" in data and isinstance(data["features"], list):
            plan.features = data["features"]
        if "description" in data:
            plan.description = data["description"]

        plan.save()
        return Response({"success": True, "message": "Plan updated successfully.", "data": PlanSerializer(plan).data}, status=status.HTTP_200_OK)

    def delete(self, request, pk=None):
        if request.user.role.role_id != UserRole.ROLE_ADMIN:
            return Response({"success": False, "message": "Only admin can delete plans."}, status=status.HTTP_403_FORBIDDEN)
        try:
            plan = Plan.objects.get(pk=pk)
            plan.delete()
            return Response({"success": True, "message": "Plan deleted successfully."}, status=status.HTTP_200_OK)
        except Plan.DoesNotExist:
            return Response({"success": False, "message": "Plan not found."}, status=status.HTTP_404_NOT_FOUND)


class SellerAPIView(APIView):
    """
    Seller management API endpoint for admin and seller operations.
    GET   -> List sellers
    POST  -> Create seller (Sign Up / Register)
    PATCH -> Update status or assign plan (plan_id)
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        user_id = request.query_params.get("user_id")
        if user_id:
            try:
                seller = User.objects.select_related("role", "meta", "plan").get(id=user_id, role_id=UserRole.ROLE_SELLER)
                return Response({"success": True, "data": UserDetailSerializer(seller).data}, status=status.HTTP_200_OK)
            except User.DoesNotExist:
                return Response({"success": False, "message": "Seller not found."}, status=status.HTTP_404_NOT_FOUND)

        sellers = User.objects.filter(role_id=UserRole.ROLE_SELLER).select_related("role", "meta", "plan").order_by("-created_at")
        serializer = UserDetailSerializer(sellers, many=True)
        return Response({"success": True, "count": len(sellers), "data": serializer.data}, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = SellerRegistrationSerializer(data=request.data)
        if serializer.is_valid():
            seller = serializer.save()
            return Response({"success": True, "message": "Seller created successfully.", "user_id": seller.id}, status=status.HTTP_201_CREATED)
        return Response({"success": False, "errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request):
        user_id = request.query_params.get("user_id") or request.data.get("user_id")
        if not user_id:
            return Response({"success": False, "message": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            seller = User.objects.select_related("role", "meta", "plan").get(id=user_id, role_id=UserRole.ROLE_SELLER)
        except User.DoesNotExist:
            return Response({"success": False, "message": "Seller not found."}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data or {}
        if "status" in payload:
            status_val = str(payload["status"]).strip()
            if status_val in [User.STATUS_ACTIVE, User.STATUS_INACTIVE, User.STATUS_PENDING]:
                seller.status = status_val

        if "plan_id" in payload:
            plan_id = payload["plan_id"]
            if plan_id is None or plan_id == "" or plan_id == 0:
                seller.plan = None
            else:
                try:
                    plan = Plan.objects.get(pk=plan_id)
                    seller.plan = plan
                except Plan.DoesNotExist:
                    return Response({"success": False, "message": f"Plan with ID {plan_id} not found."}, status=status.HTTP_404_NOT_FOUND)

        if "app_id" in payload or "whatsapp_app_id" in payload:
            app_id_val = str(payload.get("app_id") or payload.get("whatsapp_app_id") or "").strip()
            config_id = payload.get("config_id")
            from agro_app.models import WhatsAppConfig
            if config_id:
                try:
                    config = WhatsAppConfig.objects.get(whatsapp_config_id=config_id, seller_user=seller)
                    config.app_id = app_id_val
                    config.save()
                except WhatsAppConfig.DoesNotExist:
                    pass
            else:
                configs = WhatsAppConfig.objects.filter(seller_user=seller)
                if configs.exists():
                    configs.update(app_id=app_id_val)
                else:
                    WhatsAppConfig.objects.create(seller_user=seller, channel_name="WhatsApp Channel", app_id=app_id_val)

        seller.save()
        seller.refresh_from_db()
        return Response({
            "success": True,
            "message": "Seller updated successfully.",
            "data": UserDetailSerializer(seller).data,
        }, status=status.HTTP_200_OK)


class WhatsAppMediaUploadAPIView(APIView):
    """
    Media Upload API Endpoint for WhatsApp Templates and Campaigns (Image, Video, Document).
    Accepts multipart/form-data with 'file', 'media', 'header_media', or 'image'.
    Saves file to server disk under MEDIA_ROOT/whatsapp_template_headers/ and replaces previous file.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        uploaded_file = request.FILES.get("file") or request.FILES.get("media") or request.FILES.get("header_media") or request.FILES.get("image")
        # Meta WhatsApp API Maximum Header File Size Validation (16 MB)
        file_size = getattr(uploaded_file, 'size', 0)
        if file_size > 16777216:
            mb_size = round(file_size / (1024 * 1024), 2)
            return Response({
                "success": False,
                "message": f"File size ({mb_size} MB) exceeds maximum allowed limit of 16 MB for Meta WhatsApp video media."
            }, status=400)

        import os, uuid
        from django.conf import settings

        media_dir = os.path.join(settings.MEDIA_ROOT, 'whatsapp_template_headers')
        os.makedirs(media_dir, exist_ok=True)

        orig_name = getattr(uploaded_file, 'name', 'media_file')
        ext = os.path.splitext(orig_name)[1].lower() or '.jpg'
        mime_type = getattr(uploaded_file, 'content_type', '') or 'application/octet-stream'

        safe_filename = f"header_{uuid.uuid4().hex[:8]}{ext}"
        full_disk_path = os.path.join(media_dir, safe_filename)

        with open(full_disk_path, 'wb') as f_disk:
            for chunk in uploaded_file.chunks():
                f_disk.write(chunk)

        # Automatic Backend FFmpeg Video Compression & H.264 MP4 Optimization
        if ext in ['.mp4', '.mov', '.avi', '.mkv', '.3gp', '.webm']:
            try:
                import shutil, subprocess
                ffmpeg_bin = shutil.which("ffmpeg")
                if ffmpeg_bin:
                    compressed_filename = f"header_{uuid.uuid4().hex[:8]}.mp4"
                    compressed_disk_path = os.path.join(media_dir, compressed_filename)
                    cmd = [
                        ffmpeg_bin, "-y", "-i", full_disk_path,
                        "-vcodec", "libx264", "-crf", "28", "-preset", "fast",
                        "-acodec", "aac", "-movflags", "+faststart",
                        compressed_disk_path
                    ]
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                    if res.returncode == 0 and os.path.exists(compressed_disk_path) and os.path.getsize(compressed_disk_path) > 0:
                        if os.path.exists(full_disk_path) and full_disk_path != compressed_disk_path:
                            try:
                                os.remove(full_disk_path)
                            except Exception:
                                pass
                        safe_filename = compressed_filename
                        full_disk_path = compressed_disk_path
                        ext = ".mp4"
                        mime_type = "video/mp4"
                        logger.info(f"Successfully compressed and optimized video file using FFmpeg: {compressed_filename}")
            except Exception as ffmpeg_ex:
                logger.warning(f"Backend FFmpeg video compression skipped/failed: {ffmpeg_ex}")

        domain = request.META.get("HTTP_HOST") or "api.qa.syntrrix.com"
        scheme = "https" if (request.is_secure() or ("api.qa.syntrrix.com" in domain and not any(lh in domain for lh in ["127.0.0.1", "localhost"]))) else "http"

        file_url = f"{scheme}://{domain}/media/whatsapp_template_headers/{safe_filename}"
        rel_media_path = f"whatsapp_template_headers/{safe_filename}"

        # If template_id or template_name is provided, update the template's header_media DB field directly
        tpl_id = request.POST.get("template_id") or request.data.get("template_id")
        tpl_name = request.POST.get("template_name") or request.data.get("template_name")

        if tpl_id or tpl_name:
            try:
                from agro_app.models import WhatsAppTemplate
                tpl_all = WhatsAppTemplate.objects.all()
                tpl = None
                if tpl_id:
                    tpl = tpl_all.filter(seller_user=request.user, pk=tpl_id if str(tpl_id).isdigit() else None).first() or \
                          tpl_all.filter(pk=tpl_id if str(tpl_id).isdigit() else None).first() or \
                          tpl_all.filter(seller_user=request.user, template_id=tpl_id if str(tpl_id).isdigit() else None).first() or \
                          tpl_all.filter(template_id=tpl_id if str(tpl_id).isdigit() else None).first() or \
                          tpl_all.filter(seller_user=request.user, name__iexact=str(tpl_id)).first() or \
                          tpl_all.filter(name__iexact=str(tpl_id)).first()
                if not tpl and tpl_name:
                    tpl = tpl_all.filter(seller_user=request.user, name__iexact=str(tpl_name)).first() or \
                          tpl_all.filter(name__iexact=str(tpl_name)).first()

                if tpl:
                    # Remove previous media file on disk if replacing
                    if tpl.header_media and hasattr(tpl.header_media, 'name') and tpl.header_media.name:
                        old_rel = tpl.header_media.name
                        old_disk = os.path.join(settings.MEDIA_ROOT, old_rel)
                        if os.path.exists(old_disk) and old_disk != full_disk_path:
                            try:
                                os.remove(old_disk)
                                logger.info(f"Removed previous template header file from disk: {old_disk}")
                            except Exception as rm_ex:
                                logger.warning(f"Could not remove old template header file: {rm_ex}")

                    tpl.header_media.name = rel_media_path
                    tpl.save(update_fields=['header_media'])
                    logger.info(f"Successfully replaced header_media for template '{tpl.name}' (ID: {tpl.template_id}) to '{rel_media_path}'")
            except Exception as ex_tpl:
                logger.error(f"Failed to update template header_media DB field: {ex_tpl}")

        logger.info(f"WhatsApp Media Upload SUCCESS: {orig_name} -> {file_url} (MIME: {mime_type})")

        return Response({
            "success": True,
            "message": "Media uploaded and replaced successfully.",
            "file_url": file_url,
            "media_url": file_url,
            "file_name": orig_name,
            "mime_type": mime_type
        }, status=200)


class WhatsAppTemplateAPIView(APIView):
    """
    WhatsApp Templates Management & Meta Creation Endpoint
    GET  -> List local templates (or sync from Meta)
    POST -> Submit template to Meta Graph API & save to DB
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from agro_app.models import WhatsAppTemplate, WhatsAppConfig
        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can access templates."}, status=403)

        config_id = request.query_params.get("config_id")
        templates_qs = WhatsAppTemplate.objects.filter(seller_user=request.user)
        if config_id:
            templates_qs = templates_qs.filter(whatsapp_config_id=config_id)
        
        domain = request.META.get("HTTP_HOST") or "api.qa.syntrrix.com"
        scheme = "https" if (request.is_secure() or "api.qa.syntrrix.com" in domain) else "http"

        def _resolve_template_media(t):
            if t.header_media:
                try:
                    rel_url = t.header_media.url if hasattr(t.header_media, 'url') else str(t.header_media)
                    if rel_url:
                        if rel_url.startswith("http://") or rel_url.startswith("https://"):
                            return rel_url
                        clean_rel = rel_url if rel_url.startswith("/") else f"/{rel_url}"
                        return f"{scheme}://{domain}{clean_rel}"
                except Exception:
                    pass

            if t.header_handle and (t.header_handle.startswith("http://") or t.header_handle.startswith("https://")):
                return t.header_handle

            import os, re
            from django.conf import settings
            t_fmt = str(t.format or "").lower().strip()
            if t_fmt in ["video", "image", "document"]:
                clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '', t.name)
                search_dirs = [
                    os.path.join(settings.MEDIA_ROOT, 'whatsapp_template_headers'),
                    os.path.join(settings.MEDIA_ROOT, 'whatsapp_media')
                ]
                for s_dir in search_dirs:
                    if os.path.exists(s_dir):
                        for f_name in os.listdir(s_dir):
                            if (clean_name and clean_name.lower() in f_name.lower()) or t.name.lower() in f_name.lower():
                                folder = os.path.basename(s_dir)
                                rel_path = f"{folder}/{f_name}"
                                try:
                                    t.header_media = rel_path
                                    t.save(update_fields=["header_media"])
                                except Exception:
                                    pass
                                return f"{scheme}://{domain}/media/{rel_path}"

            if t_fmt == "video":
                return "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"
            elif t_fmt == "image":
                return "https://images.unsplash.com/photo-1579546929518-9e396f3cc809?w=800&fm=jpg&fit=crop&q=80"
            return ""

        data = [
            {
                "template_id": t.template_id,
                "meta_template_id": t.meta_template_id,
                "name": t.name,
                "category": t.category,
                "language": t.language,
                "status": t.status,
                "format": t.format,
                "header_text": t.header_text,
                "header_media": _resolve_template_media(t),
                "media_url": _resolve_template_media(t),
                "body": t.body,
                "footer": t.footer,
                "body_params": t.body_params,
                "buttons": t.buttons,
                "components_json": t.components_json,
                "quality_score": t.quality_score,
                "rejected_reason": t.rejected_reason,
                "channel_name": t.whatsapp_config.channel_name if t.whatsapp_config else "",
                "phone_number": t.whatsapp_config.phone_number if t.whatsapp_config else "",
                "config_id": t.whatsapp_config.whatsapp_config_id if t.whatsapp_config else None,
                "whatsapp_config_id": t.whatsapp_config.whatsapp_config_id if t.whatsapp_config else None,
                "business_number": f"{t.whatsapp_config.phone_number} · {t.whatsapp_config.channel_name}" if (t.whatsapp_config and t.whatsapp_config.phone_number and t.whatsapp_config.channel_name) else (t.whatsapp_config.phone_number or t.whatsapp_config.channel_name if t.whatsapp_config else "No Channel Linked"),
                "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "",
            }
            for t in templates_qs
        ]
        return Response({"success": True, "count": len(data), "data": data})

    def post(self, request):
        import requests
        from agro_app.models import WhatsAppTemplate, WhatsAppConfig

        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can create templates."}, status=403)

        # Handle JSON or FormData payload
        if request.content_type and "multipart/form-data" in request.content_type:
            name = str(request.POST.get("name") or "").strip().lower().replace(" ", "_")
            category = str(request.POST.get("category") or "UTILITY").upper()
            language = str(request.POST.get("language") or "en_US")
            format_type = str(request.POST.get("format") or "text").lower()
            header_text = str(request.POST.get("headerText") or "")
            body_text = str(request.POST.get("body") or "")
            footer_text = str(request.POST.get("footer") or "")
            config_id = request.POST.get("config_id")
            business_number_input = str(request.POST.get("businessNumber") or "")

            import json
            try:
                body_params = json.loads(request.POST.get("bodyParams") or "{}")
            except Exception:
                body_params = {}
            try:
                buttons = json.loads(request.POST.get("buttons") or "[]")
            except Exception:
                buttons = []
        else:
            payload = request.data or {}
            name = str(payload.get("name") or "").strip().lower().replace(" ", "_")
            category = str(payload.get("category") or "UTILITY").upper()
            language = str(payload.get("language") or "en_US")
            format_type = str(payload.get("format") or "text").lower()
            header_text = str(payload.get("headerText") or payload.get("header_text") or "")
            body_text = str(payload.get("body") or "")
            footer_text = str(payload.get("footer") or "")
            body_params = payload.get("bodyParams") or payload.get("body_params") or {}
            buttons = payload.get("buttons") or []
            config_id = payload.get("config_id")
            business_number_input = str(payload.get("businessNumber") or "")

        import re

        # Meta Template Name Validation: Lowercase, single underscores, no consecutive underscores, no special chars
        name = re.sub(r'[^a-z0-9_]', '_', name.lower())
        name = re.sub(r'_+', '_', name).strip('_')
        if not name:
            name = f"template_{uuid.uuid4().hex[:6]}"

        # Smart Local DB Unique Check: If template name already exists, auto-append unique version tag
        if WhatsAppTemplate.objects.filter(seller_user=request.user, name=name).exists():
            import random
            name = f"{name}_v{random.randint(10, 99)}"

        if not body_text:
            return Response({"success": False, "message": "Body content is required."}, status=400)

        config_qs = WhatsAppConfig.objects.filter(seller_user=request.user)
        config = None

        if config_id:
            config = config_qs.filter(whatsapp_config_id=config_id).first()

        if not config and business_number_input:
            config = config_qs.filter(phone_number__icontains=business_number_input).first() or config_qs.filter(channel_name__icontains=business_number_input).first()
            if not config:
                parts = [p.strip() for p in business_number_input.split("·") if p.strip()]
                for part in parts:
                    found = config_qs.filter(phone_number__icontains=part).first() or config_qs.filter(channel_name__icontains=part).first()
                    if found:
                        config = found
                        break

        if not config:
            config = config_qs.first()

        header_media_file = request.FILES.get("header_media") or request.FILES.get("file") or request.FILES.get("image")
        header_handle = None
        saved_rel_media_path = None

        # Explicit Disk Saving & Meta Resumable Upload
        if header_media_file:
            try:
                import os, io
                from django.conf import settings
                media_dir = os.path.join(settings.MEDIA_ROOT, 'whatsapp_template_headers')
                os.makedirs(media_dir, exist_ok=True)

                header_media_file.seek(0)
                file_bytes = header_media_file.read()
                header_media_file.seek(0)
                file_len = len(file_bytes)
                mime_type = getattr(header_media_file, 'content_type', None) or "image/jpeg"

                # Convert WebP images to JPEG automatically since Meta Graph API rejects WebP format
                if mime_type.lower() == "image/webp":
                    try:
                        from PIL import Image
                        img = Image.open(io.BytesIO(file_bytes))
                        if img.mode != 'RGB':
                            img = img.convert('RGB')
                        buf = io.BytesIO()
                        img.save(buf, format='JPEG', quality=92)
                        file_bytes = buf.getvalue()
                        file_len = len(file_bytes)
                        mime_type = "image/jpeg"
                        logger.info("Successfully converted uploaded WebP image to JPEG for Meta Graph API compliance.")
                    except Exception as conv_ex:
                        logger.warning(f"Could not convert WebP to JPEG via PIL: {conv_ex}")
                        mime_type = "image/jpeg"

                # Save file explicitly to server disk
                orig_name = getattr(header_media_file, 'name', 'header.jpg')
                ext = os.path.splitext(orig_name)[1].lower() or '.jpg'
                if ext == '.webp':
                    ext = '.jpg'

                safe_disk_filename = f"{name}_{uuid.uuid4().hex[:6]}{ext}"
                full_disk_path = os.path.join(media_dir, safe_disk_filename)

                with open(full_disk_path, 'wb') as f_disk:
                    f_disk.write(file_bytes)

                saved_rel_media_path = f"whatsapp_template_headers/{safe_disk_filename}"
                logger.info(f"Successfully saved uploaded template header media to server disk at: {full_disk_path}")

                # Auto-discover Meta App ID using debug_token API if app_id is missing in config
                if config and config.access_token and not getattr(config, 'app_id', None):
                    try:
                        dbg_res = requests.get(
                            f"https://graph.facebook.com/v23.0/debug_token?input_token={config.access_token}",
                            headers={"Authorization": f"Bearer {config.access_token}"},
                            timeout=6
                        )
                        if dbg_res.status_code == 200:
                            fetched_app_id = dbg_res.json().get("data", {}).get("app_id")
                            if fetched_app_id:
                                config.app_id = str(fetched_app_id)
                                try:
                                    config.save(update_fields=['app_id'])
                                except Exception:
                                    pass
                    except Exception as dbg_ex:
                        logger.error(f"Failed to auto-fetch Meta app_id: {dbg_ex}")

                app_id_val = getattr(config, 'app_id', None) or getattr(config, 'meta_app_id', None)

                # Perform Meta Resumable Upload if seller configured WABA App ID and Token
                if config and app_id_val and config.access_token:
                    try:
                        u_session_url = f"https://graph.facebook.com/v23.0/{app_id_val}/uploads?file_length={file_len}&file_type={mime_type}"
                        u_headers = {"Authorization": f"Bearer {config.access_token}"}
                        r_session = requests.post(u_session_url, headers=u_headers, timeout=12)

                        if r_session.status_code in [200, 201]:
                            session_id = r_session.json().get("id")
                            if session_id:
                                u_binary_url = f"https://graph.facebook.com/v23.0/{session_id}"
                                u_bin_headers = {
                                    "Authorization": f"OAuth {config.access_token}",
                                    "file_offset": "0",
                                    "Content-Type": mime_type
                                }
                                r_bin = requests.post(u_binary_url, headers=u_bin_headers, data=file_bytes, timeout=20)
                                if r_bin.status_code in [200, 201]:
                                    header_handle = r_bin.json().get("h")
                                    logger.info(f"Meta Image Resumable Upload Handle: {header_handle}")
                                else:
                                    logger.error(f"Meta Resumable Upload Step 2 Binary Upload Error ({r_bin.status_code}): {r_bin.text}")
                        else:
                            logger.error(f"Meta Resumable Upload Step 1 Session Creation Error ({r_session.status_code}): {r_session.text}")
                    except Exception as meta_u_ex:
                        logger.error(f"Meta Resumable Upload Exception: {meta_u_ex}")
            except Exception as file_proc_ex:
                logger.error(f"Template header media file processing error: {file_proc_ex}")

        # Build Meta Graph API components
        components = []
        if format_type == "text" and header_text:
            header_var_matches = re.findall(r"\{\{([^}]+)\}\}", header_text)
            header_comp = {"type": "HEADER", "format": "TEXT", "text": header_text}
            if header_var_matches:
                header_comp["example"] = {"header_text": [f"Sample_{v.strip()}" for v in header_var_matches]}
            components.append(header_comp)
        elif format_type in ["image", "video", "document"]:
            h_format = format_type.upper()
            components.append({"type": "HEADER", "format": h_format, "example": {"header_handle": [header_handle or "4930201830492"]}})

        body_var_matches = re.findall(r"\{\{([^}]+)\}\}", body_text)
        body_comp = {"type": "BODY", "text": body_text}
        if body_var_matches:
            body_comp["example"] = {"body_text": [[f"Sample_{v.strip()}" for v in body_var_matches]]}
        components.append(body_comp)

        if footer_text:
            components.append({"type": "FOOTER", "text": footer_text})

        if buttons and len(buttons) > 0:
            meta_buttons = []
            for btn in buttons:
                b_type = btn.get("type", "quick_reply")
                b_label = (btn.get("label") or "Click").strip()[:25]
                b_val = (btn.get("value") or "").strip()
                if b_type == "quick_reply":
                    meta_buttons.append({"type": "QUICK_REPLY", "text": b_label})
                elif b_type == "call_phone":
                    meta_buttons.append({"type": "PHONE_NUMBER", "text": b_label, "phone_number": b_val})
                elif b_type in ["visit_website", "dynamic_url", "url"]:
                    url_comp = {"type": "URL", "text": b_label, "url": b_val}
                    if "{{" in b_val: url_comp["example"] = ["123456"]
                    meta_buttons.append(url_comp)
                elif b_type == "copy_coupon":
                    meta_buttons.append({"type": "COPY_CODE", "example": b_val or "COUPON50"})
            if meta_buttons:
                components.append({"type": "BUTTONS", "buttons": meta_buttons})

        LANG_ISO_MAP = {"english (us)": "en_US", "english": "en_US", "hindi": "hi", "marathi": "mr", "gujarati": "gu", "tamil": "ta", "telugu": "te", "kannada": "kn", "malayalam": "ml", "arabic": "ar", "french": "fr", "portuguese (br)": "pt_BR", "spanish": "es"}
        meta_language = LANG_ISO_MAP.get(language.lower(), language)
        meta_id = ""
        status_val = "PENDING"

        if config and config.whatsapp_business_account_id and config.access_token:
            waba_id = config.whatsapp_business_account_id
            token = config.access_token
            meta_url = f"https://graph.facebook.com/v23.0/{waba_id}/message_templates"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            body_payload = {"name": name, "category": category, "language": meta_language, "allow_category_change": True, "components": components}
            try:
                r = requests.post(meta_url, headers=headers, json=body_payload, timeout=12)
                res_data = r.json()
                if r.status_code in [200, 201]:
                    meta_id = str(res_data.get("id") or "")
                    status_val = str(res_data.get("status") or "PENDING").upper()
                else:
                    err_obj = res_data.get("error") or {}
                    user_title = err_obj.get("error_user_title") or "Meta API Error"
                    user_msg = err_obj.get("error_user_msg") or err_obj.get("message") or f"Meta returned HTTP {r.status_code} error."
                    if "already exists" in user_msg.lower() or "already exists" in str(user_title).lower():
                        import random
                        unique_name = f"{name}_v{random.randint(10, 99)}"
                        body_payload["name"] = unique_name
                        r_retry = requests.post(meta_url, headers=headers, json=body_payload, timeout=12)
                        res_data_retry = r_retry.json()
                        if r_retry.status_code in [200, 201]:
                            name = unique_name
                            meta_id = str(res_data_retry.get("id") or "")
                            status_val = str(res_data_retry.get("status") or "PENDING").upper()
                        else:
                            return Response({"success": False, "message": f"Template name '{name}' already exists on Meta. Please choose a unique template name.", "meta_error": res_data_retry.get("error") or err_obj}, status=400)
                    else:
                        return Response({"success": False, "message": f"{user_title}: {user_msg}", "meta_error": err_obj}, status=400)
            except Exception as e:
                return Response({"success": False, "message": f"Connection error while contacting Meta Cloud API: {str(e)}"}, status=500)

        template_obj = WhatsAppTemplate.objects.create(
            seller_user=request.user,
            whatsapp_config=config,
            meta_template_id=meta_id,
            name=name,
            category=category,
            language=language,
            status=status_val,
            format=format_type,
            header_text=header_text,
            header_media=saved_rel_media_path or header_media_file,
            header_handle=str(header_handle or "")[:250],
            body=body_text,
            footer=footer_text,
            body_params=body_params,
            buttons=buttons,
            components_json=components,
        )

        return Response({
            "success": True,
            "message": "Template created and submitted to Meta successfully.",
            "data": {
                "template_id": template_obj.template_id,
                "meta_template_id": template_obj.meta_template_id,
                "name": template_obj.name,
                "category": template_obj.category,
                "language": template_obj.language,
                "status": template_obj.status,
                "format": template_obj.format,
                "created_at": template_obj.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        })


class WhatsAppMediaUploadAPIView(APIView):
    """
    WhatsApp Campaign Header Media Upload & Replacement API
    POST -> Upload image/video/document file, save to media/whatsapp_media/ and return public URL.
    Overwrites/replaces previous media file if same template or filename exists.
    Also updates the seller's WhatsAppTemplate DB record's header_media column!
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import os
        import re
        import mimetypes
        from django.conf import settings
        from agro_app.models import WhatsAppTemplate, UserRole

        # Register standard MIME types to prevent Meta error #131053 (application/octet-stream)
        mimetypes.add_type('image/jpeg', '.jpg')
        mimetypes.add_type('image/jpeg', '.jpeg')
        mimetypes.add_type('image/png', '.png')
        mimetypes.add_type('image/webp', '.webp')
        mimetypes.add_type('video/mp4', '.mp4')
        mimetypes.add_type('application/pdf', '.pdf')

        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can upload campaign media."}, status=403)

        media_file = request.FILES.get("file")
        if not media_file:
            return Response({"success": False, "message": "No media file provided."}, status=400)

        # Directory setup: media/whatsapp_media/
        media_dir = os.path.join(settings.MEDIA_ROOT, "whatsapp_media")
        os.makedirs(media_dir, exist_ok=True)

        template_identifier = (request.POST.get("template_name") or request.POST.get("template_id") or "").strip()
        original_name = media_file.name
        content_type = getattr(media_file, "content_type", "") or ""

        # Determine extension cleanly
        ext = os.path.splitext(original_name)[1].lower()
        if not ext or ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".pdf", ".doc", ".docx"]:
            if "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            elif "mp4" in content_type or "video" in content_type:
                ext = ".mp4"
            elif "pdf" in content_type:
                ext = ".pdf"
            else:
                ext = ".jpg"

        # Build clean filename e.g. template_media_card_demo.jpg
        if template_identifier:
            clean_tpl_name = re.sub(r'[^a-zA-Z0-9_\-]', '', template_identifier)
            filename = f"template_media_{clean_tpl_name}{ext}"
        else:
            clean_file_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', media_file.name)
            filename = f"upload_{clean_file_name}"

        file_path = os.path.join(media_dir, filename)

        # Overwrite/replace file cleanly on disk
        with open(file_path, "wb+") as destination:
            for chunk in media_file.chunks():
                destination.write(chunk)

        # Update WhatsAppTemplate DB record's header_media field
        updated_template_name = None
        if template_identifier:
            tpl_db = WhatsAppTemplate.objects.filter(
                seller_user=request.user,
                name__iexact=template_identifier
            ).first()
            if not tpl_db and template_identifier.isdigit():
                tpl_db = WhatsAppTemplate.objects.filter(
                    seller_user=request.user,
                    template_id=int(template_identifier)
                ).first()

            if tpl_db:
                relative_media_path = f"whatsapp_media/{filename}"
                tpl_db.header_media = relative_media_path
                tpl_db.save(update_fields=["header_media", "updated_at"])
                updated_template_name = tpl_db.name

        domain = request.META.get("HTTP_HOST") or "api.qa.syntrrix.com"
        scheme = "https" if (request.is_secure() or ("api.qa.syntrrix.com" in domain and not any(lh in domain for lh in ["127.0.0.1", "localhost"]))) else "http"
        media_url = f"{scheme}://{domain}/media/whatsapp_media/{filename}"

        ext_lower = ext.lower()
        file_type = "image"
        if ext_lower in [".mp4", ".mov", ".avi", ".mkv"]:
            file_type = "video"
        elif ext_lower in [".pdf", ".doc", ".docx", ".xls", ".xlsx"]:
            file_type = "document"

        return Response({
            "success": True,
            "message": "Media uploaded and template database header_media updated successfully.",
            "media_url": media_url,
            "file_url": media_url,
            "relative_url": f"/media/whatsapp_media/{filename}",
            "filename": filename,
            "file_type": file_type,
            "updated_template": updated_template_name,
        })


class WhatsAppTemplateSyncAPIView(APIView):
    """
    Sync templates directly with Meta Graph API
    GET //{WABA_ID}/message_templates
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import requests
        from agro_app.models import WhatsAppTemplate, WhatsAppConfig

        if request.user.role.role_id != UserRole.ROLE_SELLER:
            return Response({"success": False, "message": "Only sellers can sync templates."}, status=403)

        payload = request.data or {}
        config_id = payload.get("config_id")
        business_number_input = str(payload.get("businessNumber") or payload.get("business_number") or "").strip()

        config_qs = WhatsAppConfig.objects.filter(seller_user=request.user)
        config = None

        if config_id:
            config = config_qs.filter(whatsapp_config_id=config_id).first() or config_qs.filter(pk=config_id).first()

        if not config and business_number_input:
            config = config_qs.filter(phone_number__icontains=business_number_input).first() or config_qs.filter(channel_name__icontains=business_number_input).first()
            if not config:
                parts = [p.strip() for p in business_number_input.split("·") if p.strip()]
                for part in parts:
                    found = config_qs.filter(phone_number__icontains=part).first() or config_qs.filter(channel_name__icontains=part).first()
                    if found:
                        config = found
                        break

        if not config:
            config = config_qs.filter(is_connected=True).exclude(whatsapp_business_account_id="").exclude(access_token="").first()

        if not config:
            config = config_qs.exclude(whatsapp_business_account_id="").exclude(access_token="").first()

        if not config:
            config = config_qs.first()

        waba_id = (config.whatsapp_business_account_id if config else "").strip()
        token = (config.access_token if config else "").strip()

        if not waba_id or not token:
            valid_config = config_qs.exclude(whatsapp_business_account_id="").exclude(access_token="").first()
            if valid_config:
                if not waba_id:
                    waba_id = valid_config.whatsapp_business_account_id
                if not token:
                    token = valid_config.access_token

        if not waba_id or not token:
            return Response({"success": False, "message": "Active WhatsApp configuration with WABA ID and Access Token is required to sync templates with Meta. Please connect WABA account in WhatsApp Channel Settings."}, status=400)

        sync_url = f"https://graph.facebook.com/v23.0/{waba_id}/message_templates?fields=id,name,status,category,language,components,quality_score,rejected_reason,created_time&limit=100"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            r = requests.get(sync_url, headers=headers, timeout=12)
            if r.status_code == 200:
                res_data = r.json()
                meta_templates = res_data.get("data") or []
                synced_count = 0
                for item in meta_templates:
                    t_name = item.get("name")
                    if not t_name:
                        continue
                    t_status = str(item.get("status") or "PENDING").upper()
                    t_cat = str(item.get("category") or "UTILITY").upper()
                    t_lang = str(item.get("language") or "en_US")
                    t_meta_id = str(item.get("id") or "")
                    t_components = item.get("components") or []

                    t_body = ""
                    t_header = ""
                    t_footer = ""
                    t_format = "text"
                    t_buttons = []
                    t_header_handle = ""

                    for comp in t_components:
                        c_type = str(comp.get("type") or "").upper()
                        if c_type == "BODY":
                            t_body = comp.get("text") or ""
                        elif c_type == "HEADER":
                            h_fmt = str(comp.get("format") or "").lower().strip()
                            if h_fmt in ["image", "video", "document", "text", "location", "carousel"]:
                                t_format = h_fmt
                            elif comp.get("text"):
                                t_format = "text"

                            if comp.get("text"):
                                t_header = comp.get("text") or ""

                            ex_obj = comp.get("example") or {}
                            ex_handles = ex_obj.get("header_handle") or ex_obj.get("header_url") or []
                            if isinstance(ex_handles, list) and len(ex_handles) > 0:
                                t_header_handle = str(ex_handles[0])
                            elif isinstance(ex_handles, str) and ex_handles:
                                t_header_handle = ex_handles

                        elif c_type == "FOOTER":
                            t_footer = comp.get("text") or ""
                        elif c_type == "BUTTONS":
                            t_buttons = comp.get("buttons") or []

                    tmpl, created = WhatsAppTemplate.objects.update_or_create(
                        seller_user=request.user,
                        name=t_name,
                        defaults={
                            "whatsapp_config": config,
                            "meta_template_id": t_meta_id,
                            "category": t_cat,
                            "language": t_lang,
                            "status": t_status,
                            "format": t_format,
                            "header_text": t_header,
                            "header_handle": t_header_handle,
                            "body": t_body,
                            "footer": t_footer,
                            "buttons": t_buttons,
                            "components_json": t_components,
                        }
                    )

                    # Auto-discover local uploaded header media on disk if header_media is empty
                    if not tmpl.header_media and t_format in ["video", "image", "document"]:
                        import os, re
                        from django.conf import settings
                        clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '', t_name)
                        search_dirs = [
                            (os.path.join(settings.MEDIA_ROOT, 'whatsapp_template_headers'), 'whatsapp_template_headers'),
                            (os.path.join(settings.MEDIA_ROOT, 'whatsapp_media'), 'whatsapp_media')
                        ]
                        for s_dir, subfolder in search_dirs:
                            if os.path.exists(s_dir):
                                for f_name in os.listdir(s_dir):
                                    if (clean_name and clean_name.lower() in f_name.lower()) or t_name.lower() in f_name.lower():
                                        tmpl.header_media = f"{subfolder}/{f_name}"
                                        tmpl.save(update_fields=["header_media"])
                                        break
                            if tmpl.header_media:
                                break

                    synced_count += 1

                return Response({
                    "success": True,
                    "message": f"Successfully synchronized {synced_count} templates from Meta Cloud API.",
                    "count": synced_count
                })
            else:
                return Response({"success": False, "message": f"Meta API Sync Error: {r.status_code} - {r.text}"}, status=400)
        except Exception as e:
            return Response({"success": False, "message": f"Template sync failed: {str(e)}"}, status=500)


class WhatsAppCampaignAPIView(APIView):
    """
    WhatsApp Campaign Management API Endpoint
    GET  -> List seller campaigns from DB
    POST -> Create a new WhatsApp campaign record in DB
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            from agro_app.models import WhatsAppCampaignRecipient
            # Filter campaigns for the seller and order by created_at DESC
            campaigns = WhatsAppCampaign.objects.filter(
                seller_user=request.user
            ).order_by("-created_at")

            data = []
            for c in campaigns:
                rec_qs = WhatsAppCampaignRecipient.objects.filter(campaign=c)
                if rec_qs.exists():
                    aud_val = rec_qs.count()
                    sent_val = rec_qs.filter(sent_status=True).count()
                    deliv_val = rec_qs.filter(delivered_status=True).count()
                    read_val = rec_qs.filter(read_status=True).count()
                    fail_val = rec_qs.filter(sent_status=False).count()

                    delivered_str = str(deliv_val)
                    read_str = str(read_val)
                    sent_str = str(sent_val)
                    failed_str = str(fail_val)
                    audience_val = aud_val
                else:
                    audience_val = c.audience
                    sent_str = str(c.sent or "0")
                    delivered_str = str(c.delivered or "0").split("/")[0].strip()
                    read_str = str(c.read or "0").split("/")[0].strip()
                    failed_str = str(c.failed or "0")

                data.append({
                    "id": c.campaign_id,
                    "title": c.title,
                    "status": c.status,
                    "subtitle": c.subtitle or f"{c.template_name} · {c.channel_name}",
                    "business_number": c.business_number or "Syntrrix Main · +919000012345",
                    "audience": audience_val,
                    "sent": sent_str,
                    "delivered": delivered_str,
                    "read": read_str,
                    "failed": failed_str,
                    "author": c.author,
                    "date": c.created_at.strftime("%m/%d/%Y"),
                    "created_at": c.created_at.isoformat(),
                })
            return Response({"success": True, "count": len(data), "data": data}, status=200)
        except Exception as e:
            return Response({"success": False, "message": f"Failed to fetch campaigns: {str(e)}"}, status=500)

    def post(self, request):
        try:
            payload = request.data or {}
            title = str(payload.get("title") or "").strip()
            if not title:
                return Response({"success": False, "message": "Campaign title is required."}, status=400)

            status_val = str(payload.get("status") or "Completed").strip()
            subtitle = str(payload.get("subtitle") or "").strip()
            business_number = str(payload.get("business_number") or "").strip()
            template_name = str(payload.get("template_name") or "").strip()
            channel_name = str(payload.get("channel_name") or "").strip()
            audience = int(payload.get("audience") or 0)
            sent = str(payload.get("sent") or str(audience)).strip()
            delivered = str(payload.get("delivered") or f"{audience} / {audience}").strip()
            read = str(payload.get("read") or f"{int(audience * 0.85)} / {audience}").strip()
            failed = str(payload.get("failed") or "0").strip()
            author = str(payload.get("author") or "By you").strip()

            campaign = WhatsAppCampaign.objects.create(
                seller_user=request.user,
                title=title,
                status=status_val,
                subtitle=subtitle,
                business_number=business_number,
                template_name=template_name,
                channel_name=channel_name,
                audience=audience,
                sent=sent,
                delivered=delivered,
                read=read,
                failed=failed,
                author=author,
            )

            # Auto populate recipient logs if passed in payload
            recipients_data = payload.get("recipients_data") or payload.get("recipients") or []
            if isinstance(recipients_data, list) and len(recipients_data) > 0:
                from agro_app.models import WhatsAppCampaignRecipient
                for r_item in recipients_data:
                    p_num = str(r_item.get("phone") or r_item.get("phone_number") or "").strip()
                    if p_num:
                        c_name = str(r_item.get("name") or r_item.get("customer_name") or "Recipient").strip()
                        s_status = bool(r_item.get("sent_status", True))
                        s_err = str(r_item.get("sent_error") or "").strip()
                        d_status = bool(r_item.get("delivered_status", False))
                        d_err = str(r_item.get("delivered_error") or "").strip()
                        r_status = bool(r_item.get("read_status", False))

                        WhatsAppCampaignRecipient.objects.create(
                            campaign=campaign,
                            phone_number=p_num,
                            customer_name=c_name,
                            sent_status=s_status,
                            sent_error=s_err,
                            delivered_status=d_status,
                            delivered_error=d_err,
                            read_status=r_status,
                        )

            return Response({
                "success": True,
                "message": "WhatsApp Campaign saved to database successfully.",
                "data": {
                    "id": campaign.campaign_id,
                    "title": campaign.title,
                    "status": campaign.status,
                    "subtitle": campaign.subtitle,
                    "business_number": campaign.business_number,
                    "audience": campaign.audience,
                    "sent": campaign.sent,
                    "delivered": campaign.delivered,
                    "read": campaign.read,
                    "failed": campaign.failed,
                    "author": campaign.author,
                    "date": campaign.created_at.strftime("%m/%d/%Y"),
                }
            }, status=201)
        except Exception as e:
            return Response({"success": False, "message": f"Failed to save campaign: {str(e)}"}, status=500)


class WhatsAppCampaignRecipientsAPIView(APIView):
    """
    Returns detailed recipient logs for a specific campaign ID in table format.
    GET /api/whatsapp/campaigns/recipients/?campaign_id=<id>
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        campaign_id = request.query_params.get("campaign_id") or request.query_params.get("id")
        if not campaign_id:
            return Response({"success": False, "message": "campaign_id is required."}, status=400)

        try:
            from agro_app.models import WhatsAppCampaignRecipient, WhatsAppCampaign
            campaign = WhatsAppCampaign.objects.get(pk=campaign_id, seller_user=request.user)
            recipients = WhatsAppCampaignRecipient.objects.filter(campaign=campaign).order_by("recipient_id")

            import zoneinfo
            from datetime import timezone as py_timezone, timedelta as py_timedelta
            try:
                ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
            except Exception:
                ist_tz = py_timezone(py_timedelta(hours=5, minutes=30))

            data = []
            for r in recipients:
                if r.timestamp:
                    if timezone.is_naive(r.timestamp):
                        ist_time = r.timestamp.replace(tzinfo=ist_tz)
                    else:
                        ist_time = r.timestamp.astimezone(ist_tz)
                    time_str = ist_time.strftime("%I:%M %p · %d/%m/%Y")
                    iso_time = ist_time.isoformat()
                else:
                    time_str = ""
                    iso_time = ""

                data.append({
                    "id": r.recipient_id,
                    "phone_number": r.phone_number,
                    "customer_name": r.customer_name or "Recipient",
                    "sent_status": r.sent_status,
                    "sent_error": r.sent_error,
                    "delivered_status": r.delivered_status,
                    "delivered_error": r.delivered_error,
                    "read_status": r.read_status,
                    "time": time_str,
                    "timestamp": iso_time,
                })
            return Response({"success": True, "count": len(data), "data": data}, status=200)
        except WhatsAppCampaign.DoesNotExist:
            return Response({"success": False, "message": "Campaign not found."}, status=404)
        except Exception as e:
            return Response({"success": False, "message": f"Failed to fetch campaign recipients: {str(e)}"}, status=500)
