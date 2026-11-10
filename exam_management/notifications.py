"""
Publication and notification outbox.

Safety rules (each is enforced in code and covered by a test):
  * only an APPROVED allocation can be published; only a PUBLISHED one produces notifications
  * everything is a dry run unless the caller passes dry_run=False AND confirmed=True
  * one notification per (allocation, person, channel, kind): re-planning or re-sending never duplicates
  * a contact must pass validation; a person without one is SKIPPED with the reason, not guessed
  * the status stored is exactly what the provider reported: 'sent' (accepted) is never promoted to
    'delivered' unless the provider itself confirms delivery
  * a channel with no connected provider is SKIPPED ("no provider connected"), never reported as sent
  * every state change is audited
"""

from __future__ import annotations

import json
import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from .db import transaction
from .repository import now
from .service import ExamError

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_PHONE = re.compile(r"^\+?[0-9]{8,15}$")


@dataclass
class DeliveryResult:
    status: str                 # 'sent' | 'delivered' | 'failed'
    ref: str = ""
    detail: str = ""


class Provider(Protocol):
    def send(self, address: str, subject: str, body: str) -> DeliveryResult: ...


class SmtpEmailProvider:
    """Email through an SMTP server configured by environment (never hardcoded).  'sent' = accepted by the server."""

    def __init__(self, env: Optional[Dict[str, str]] = None):
        e = env if env is not None else os.environ
        self.host, self.port = e.get("EXAM_MGMT_SMTP_HOST", ""), int(e.get("EXAM_MGMT_SMTP_PORT", "587") or 587)
        self.user, self.password = e.get("EXAM_MGMT_SMTP_USER", ""), e.get("EXAM_MGMT_SMTP_PASSWORD", "")
        self.sender = e.get("EXAM_MGMT_SMTP_FROM", self.user)

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def send(self, address: str, subject: str, body: str) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult("failed", detail="SMTP is not configured")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, address, subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                s.starttls()
                if self.user:
                    s.login(self.user, self.password)
                s.send_message(msg)
            return DeliveryResult("sent", detail="accepted by the mail server (delivery is not confirmed)")
        except Exception as exc:
            return DeliveryResult("failed", detail=str(exc)[:200])


class NotificationService:

    def __init__(self, service, providers: Optional[Dict[str, Provider]] = None, actor: str = "administrator"):
        self.svc = service
        self.repo = service.repo
        self.conn = service.repo.conn
        self.actor = actor
        self.providers: Dict[str, Provider] = dict(providers or {})
        smtp = SmtpEmailProvider()
        if "email" not in self.providers and smtp.configured:
            self.providers["email"] = smtp

    # ------------------------------------------------------------ workflow
    def reject(self, aid: int, reason: str) -> Dict[str, Any]:
        """Administrator rejects a draft: it is cancelled with the reason on record, revise by generating again."""
        if not reason.strip():
            raise ExamError("a reason is required to reject a draft")
        self.svc.cancel_draft(aid)
        self.repo.audit("reject", "allocation", aid, {"reason": reason}, self.actor)
        return {"status": "rejected", "reason": reason}

    def publish(self, aid: int, note: str = "") -> Dict[str, Any]:
        a = self.repo.allocation(aid)
        if a is None:
            raise ExamError(f"unknown allocation {aid}")
        if a["status"] != "approved":
            raise ExamError(f"only an approved allocation can be published (version {a['version']} is {a['status']})")
        with transaction(self.conn):
            if self.is_published(aid):
                return {"published": True, "already": True}
            self.conn.execute("INSERT INTO exm_publication(allocation_id,published_by,published_at,note) VALUES (?,?,?,?)",
                              (aid, self.actor, now(), note))
            self.repo.audit("publish", "allocation", aid, {"version": a["version"]}, self.actor)
        return {"published": True, "already": False}

    def is_published(self, aid: int) -> bool:
        return self.conn.execute("SELECT 1 FROM exm_publication WHERE allocation_id=?", (aid,)).fetchone() is not None

    # ------------------------------------------------------------- contacts
    @staticmethod
    def validate_contact(channel: str, address: str) -> Optional[str]:
        address = (address or "").strip()
        if channel == "email":
            return None if _EMAIL.match(address) else "not a valid email address"
        if channel in ("sms", "whatsapp"):
            return None if _PHONE.match(address.replace(" ", "").replace("-", "")) else "not a valid phone number"
        if channel == "in_app":
            return None
        return f"unknown channel '{channel}'"

    def set_contact(self, faculty_id: int, channel: str, address: str, verified: bool = False) -> Dict[str, Any]:
        err = self.validate_contact(channel, address)
        if err:
            raise ExamError(f"{channel} contact rejected: {err}")
        with transaction(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO exm_contact(faculty_id,channel,address,verified,updated_at) VALUES (?,?,?,?,?)",
                              (faculty_id, channel, address.strip(), int(verified), now()))
            self.repo.audit("contact_set", "faculty", faculty_id, {"channel": channel}, self.actor)
        return {"faculty_id": faculty_id, "channel": channel}

    # ---------------------------------------------------------------- plan
    def _message(self, aid: int, fid: int, duties: List[Dict[str, Any]], name: str) -> Dict[str, str]:
        cfg = self.svc.settings
        labels = cfg.seat_role_labels
        lines = []
        for d in sorted(duties, key=lambda x: (x["exam_date"], x["start_time"])):
            role = labels[d["seat"] - 1] if 0 < d["seat"] <= len(labels) else f"Invigilator {d['seat']}"
            lines.append(f"- {d['exam_date']}, {d['start_time']}-{d['end_time']}, {d['room']} ({role})")
        body = (cfg.notification_greeting.format(name=name) + "\n\n" + cfg.notification_intro + "\n" +
                "\n".join(lines) + "\n\n" + cfg.notification_closing)
        return {"subject": cfg.notification_subject, "body": body}

    def plan(self, aid: int, channels: Sequence[str] = ("in_app", "email")) -> Dict[str, Any]:
        if not self.is_published(aid):
            raise ExamError("notifications are only prepared for a PUBLISHED allocation (approve, then publish)")
        by: Dict[int, List[Dict[str, Any]]] = {}
        for d in self.repo.duties(aid):
            by.setdefault(d["faculty_id"], []).append(d)
        created = skipped = existing = 0
        with transaction(self.conn):
            for fid, ds in by.items():
                name = ds[0]["faculty"]
                msg = self._message(aid, fid, ds, name)
                for ch in channels:
                    row = self.conn.execute("SELECT * FROM exm_contact WHERE faculty_id=? AND channel=?", (fid, ch)).fetchone()
                    addr = row["address"] if row else ""
                    status, detail = "queued", ""
                    if ch == "in_app":
                        status = "in_app"
                    elif not row:
                        status, detail = "skipped", "no contact on file"
                    elif self.validate_contact(ch, addr):
                        status, detail = "skipped", "contact failed validation"
                    elif ch not in self.providers:
                        status, detail = "skipped", "no provider connected for this channel"
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO exm_notification(allocation_id,faculty_id,channel,kind,address,subject,body,"
                        "status,detail,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, fid, ch, "assignment", addr, msg["subject"], msg["body"], status, detail, now(), now()))
                    if cur.rowcount:
                        created += 1
                        skipped += status == "skipped"
                    else:
                        existing += 1                         # already planned: never duplicated
            self.repo.audit("notifications_planned", "allocation", aid, {"created": created, "existing": existing}, self.actor)
        return {"created": created, "already_planned": existing, "skipped_at_planning": skipped}

    # ------------------------------------------------------------- dispatch
    def dispatch(self, aid: int, *, dry_run: bool = True, confirmed: bool = False,
                 retry_failed: bool = False, max_attempts: int = 3) -> Dict[str, Any]:
        """Dry run (default): shows what WOULD be sent and sends nothing.  Real: needs dry_run=False AND confirmed=True."""

        if not self.is_published(aid):
            raise ExamError("only a published allocation can be notified")
        wanted = ["queued"] + (["failed"] if retry_failed else [])
        rows = [dict(r) for r in self.conn.execute(
            f"SELECT * FROM exm_notification WHERE allocation_id=? AND status IN ({','.join('?' * len(wanted))}) "
            f"AND channel<>'in_app'", (aid, *wanted))]
        if dry_run or not confirmed:
            return {"mode": "dry_run", "would_send": len(rows), "sent": 0,
                    "message": "Nothing was sent. Review, then dispatch with dry_run=False and confirmed=True."
                               if rows else "Nothing is waiting to be sent."}
        out = {"mode": "live", "sent": 0, "delivered": 0, "failed": 0, "skipped": 0}
        for r in rows:
            if r["attempts"] >= max_attempts:
                out["skipped"] += 1
                continue
            prov = self.providers.get(r["channel"])
            if prov is None:
                res = DeliveryResult("failed", detail="no provider connected")
            else:
                try:
                    res = prov.send(r["address"], r["subject"], r["body"])
                except Exception as exc:                      # a provider crash is a failure, not a success
                    res = DeliveryResult("failed", detail=str(exc)[:200])
            status = res.status if res.status in ("sent", "delivered", "failed") else "failed"
            with transaction(self.conn):
                self.conn.execute("UPDATE exm_notification SET status=?, detail=?, provider_ref=?, attempts=attempts+1, "
                                  "updated_at=? WHERE id=?", (status, res.detail, res.ref, now(), r["id"]))
            out[status] += 1
        self.repo.audit("notifications_dispatched", "allocation", aid, out, self.actor)
        return out

    # -------------------------------------------------------------- views
    def status(self, aid: int) -> Dict[str, int]:
        return {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) n FROM exm_notification WHERE allocation_id=? GROUP BY status", (aid,))}

    def inbox(self, faculty_id: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT n.* FROM exm_notification n JOIN exm_publication p ON p.allocation_id=n.allocation_id "
            "WHERE n.faculty_id=? AND n.channel='in_app' ORDER BY n.id DESC", (faculty_id,))]