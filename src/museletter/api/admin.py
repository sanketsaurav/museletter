import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from .. import doctor as doctor_mod
from ..db import SUBSCRIBER_STATUSES, new_id, utcnow
from ..render import build_email
from .common import (
    campaign_json,
    campaign_stats,
    get_campaign,
    get_list,
    get_subscriber,
    get_tag,
    normalize_email,
    require_api_key,
    slugify,
    subscriber_json,
    subscriber_tag_names,
    valid_email,
)

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


class ListIn(BaseModel):
    name: str
    slug: str | None = None


class ListPatch(BaseModel):
    name: str | None = None
    slug: str | None = None


class SubscriberIn(BaseModel):
    email: str
    name: str = ""
    tags: list[str] = []
    status: str = "active"


class SubscriberPatch(BaseModel):
    name: str | None = None
    status: str | None = None


class TagIn(BaseModel):
    name: str


class CampaignIn(BaseModel):
    subject: str
    body_markdown: str
    tag: str | None = None


class CampaignPatch(BaseModel):
    subject: str | None = None
    body_markdown: str | None = None
    tag: str | None = None


class TestSendIn(BaseModel):
    to: str


class SendIn(BaseModel):
    confirm: bool = False
    dry_run: bool = False
    skip_test: bool = False


class SuppressionIn(BaseModel):
    email: str
    reason: str = "manual"


class ImportIn(BaseModel):
    csv: str


# ---------- lists ----------


@router.get("/lists")
async def list_lists(request: Request):
    db = request.app.state.db
    async with db.execute(
        "SELECT l.*, (SELECT COUNT(*) FROM subscribers s WHERE s.list_id = l.id AND s.status = 'active') AS active_subscribers "
        "FROM lists l ORDER BY l.created_at"
    ) as cur:
        rows = await cur.fetchall()
    return {"lists": [dict(r) for r in rows]}


@router.post("/lists", status_code=201)
async def create_list(request: Request, body: ListIn):
    db = request.app.state.db
    slug = slugify(body.slug or body.name)
    async with db.execute("SELECT 1 FROM lists WHERE slug = ?", (slug,)) as cur:
        if await cur.fetchone():
            raise HTTPException(status_code=409, detail=f"list slug already exists: {slug}")
    list_id = new_id("list")
    await db.execute(
        "INSERT INTO lists (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
        (list_id, slug, body.name, utcnow()),
    )
    await db.commit()
    return {"id": list_id, "slug": slug, "name": body.name}


@router.get("/lists/{ref}")
async def get_list_detail(request: Request, ref: str):
    db = request.app.state.db
    lst = await get_list(db, ref)
    async with db.execute(
        "SELECT status, COUNT(*) AS n FROM subscribers WHERE list_id = ? GROUP BY status", (lst["id"],)
    ) as cur:
        counts = {r["status"]: r["n"] for r in await cur.fetchall()}
    return {
        **dict(lst),
        "subscribers": {status: counts.get(status, 0) for status in SUBSCRIBER_STATUSES},
        "subscribe_url": f"{request.app.state.settings.base_url}/subscribe/{lst['slug']}",
    }


@router.patch("/lists/{ref}")
async def update_list(request: Request, ref: str, body: ListPatch):
    db = request.app.state.db
    lst = await get_list(db, ref)
    name = body.name if body.name is not None else lst["name"]
    slug = slugify(body.slug) if body.slug is not None else lst["slug"]
    if slug != lst["slug"]:
        async with db.execute("SELECT 1 FROM lists WHERE slug = ? AND id != ?", (slug, lst["id"])) as cur:
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail=f"list slug already exists: {slug}")
    await db.execute("UPDATE lists SET name = ?, slug = ? WHERE id = ?", (name, slug, lst["id"]))
    await db.commit()
    return {"id": lst["id"], "slug": slug, "name": name}


@router.delete("/lists/{ref}", status_code=204)
async def delete_list(request: Request, ref: str):
    db = request.app.state.db
    lst = await get_list(db, ref)
    async with db.execute("SELECT COUNT(*) AS n FROM lists") as cur:
        if (await cur.fetchone())["n"] <= 1:
            raise HTTPException(status_code=409, detail="cannot delete the only list")
    await db.execute("DELETE FROM lists WHERE id = ?", (lst["id"],))
    await db.commit()
    return Response(status_code=204)


# ---------- subscribers ----------


@router.get("/lists/{ref}/subscribers")
async def list_subscribers(
    request: Request,
    ref: str,
    status: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    db = request.app.state.db
    lst = await get_list(db, ref)
    where = ["s.list_id = ?"]
    params: list = [lst["id"]]
    if status:
        where.append("s.status = ?")
        params.append(status)
    if tag:
        tag_row = await get_tag(db, lst["id"], tag)
        if tag_row is None:
            raise HTTPException(status_code=404, detail=f"tag not found: {tag}")
        where.append(
            "EXISTS (SELECT 1 FROM subscriber_tags st WHERE st.subscriber_id = s.id AND st.tag_id = ?)"
        )
        params.append(tag_row["id"])
    if q:
        where.append("(s.email LIKE ? OR s.name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where_sql = " AND ".join(where)
    async with db.execute(f"SELECT COUNT(*) AS n FROM subscribers s WHERE {where_sql}", params) as cur:
        total = (await cur.fetchone())["n"]
    async with db.execute(
        f"SELECT s.* FROM subscribers s WHERE {where_sql} ORDER BY s.created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ) as cur:
        rows = await cur.fetchall()
    items = [subscriber_json(r, await subscriber_tag_names(db, r["id"])) for r in rows]
    return {"subscribers": items, "total": total, "limit": limit, "offset": offset}


async def _attach_tags(db, list_id: str, subscriber_id: str, tag_names: list[str]) -> None:
    for raw in tag_names:
        name = raw.strip()
        if not name:
            continue
        tag_row = await get_tag(db, list_id, name)
        if tag_row is None:
            tag_id = new_id("tag")
            await db.execute(
                "INSERT INTO tags (id, list_id, name, created_at) VALUES (?, ?, ?, ?)",
                (tag_id, list_id, name, utcnow()),
            )
        else:
            tag_id = tag_row["id"]
        await db.execute(
            "INSERT OR IGNORE INTO subscriber_tags (subscriber_id, tag_id) VALUES (?, ?)",
            (subscriber_id, tag_id),
        )


@router.post("/lists/{ref}/subscribers", status_code=201)
async def add_subscriber(request: Request, ref: str, body: SubscriberIn):
    db = request.app.state.db
    lst = await get_list(db, ref)
    email = normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(status_code=422, detail=f"invalid email: {body.email}")
    if body.status not in SUBSCRIBER_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {body.status}")
    async with db.execute(
        "SELECT id FROM subscribers WHERE list_id = ? AND email = ?", (lst["id"], email)
    ) as cur:
        if await cur.fetchone():
            raise HTTPException(status_code=409, detail=f"subscriber already exists: {email}")
    now = utcnow()
    sub_id = new_id("sub")
    confirmed_at = now if body.status == "active" else None
    await db.execute(
        "INSERT INTO subscribers (id, list_id, email, name, status, created_at, confirmed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sub_id, lst["id"], email, body.name.strip(), body.status, now, confirmed_at),
    )
    await _attach_tags(db, lst["id"], sub_id, body.tags)
    await db.commit()
    async with db.execute("SELECT 1 FROM suppressions WHERE email = ?", (email,)) as cur:
        suppressed = bool(await cur.fetchone())
    result = subscriber_json(await get_subscriber(db, sub_id), await subscriber_tag_names(db, sub_id))
    if suppressed:
        result["warning"] = "this email is on the suppression list and will be skipped at send time"
    return result


@router.get("/subscribers/{subscriber_id}")
async def get_subscriber_detail(request: Request, subscriber_id: str):
    db = request.app.state.db
    row = await get_subscriber(db, subscriber_id)
    return subscriber_json(row, await subscriber_tag_names(db, subscriber_id))


@router.patch("/subscribers/{subscriber_id}")
async def update_subscriber(request: Request, subscriber_id: str, body: SubscriberPatch):
    db = request.app.state.db
    row = await get_subscriber(db, subscriber_id)
    name = body.name if body.name is not None else row["name"]
    status = body.status if body.status is not None else row["status"]
    if status not in SUBSCRIBER_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {status}")
    await db.execute(
        "UPDATE subscribers SET name = ?, status = ? WHERE id = ?", (name, status, subscriber_id)
    )
    await db.commit()
    return subscriber_json(
        await get_subscriber(db, subscriber_id), await subscriber_tag_names(db, subscriber_id)
    )


@router.delete("/subscribers/{subscriber_id}", status_code=204)
async def delete_subscriber(request: Request, subscriber_id: str):
    db = request.app.state.db
    await get_subscriber(db, subscriber_id)
    await db.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
    await db.commit()
    return Response(status_code=204)


@router.post("/subscribers/{subscriber_id}/tags")
async def tag_subscriber(request: Request, subscriber_id: str, body: TagIn):
    db = request.app.state.db
    row = await get_subscriber(db, subscriber_id)
    await _attach_tags(db, row["list_id"], subscriber_id, [body.name])
    await db.commit()
    return subscriber_json(row, await subscriber_tag_names(db, subscriber_id))


@router.delete("/subscribers/{subscriber_id}/tags/{tag_ref}")
async def untag_subscriber(request: Request, subscriber_id: str, tag_ref: str):
    db = request.app.state.db
    row = await get_subscriber(db, subscriber_id)
    tag_row = await get_tag(db, row["list_id"], tag_ref)
    if tag_row is None:
        raise HTTPException(status_code=404, detail=f"tag not found: {tag_ref}")
    await db.execute(
        "DELETE FROM subscriber_tags WHERE subscriber_id = ? AND tag_id = ?", (subscriber_id, tag_row["id"])
    )
    await db.commit()
    return subscriber_json(row, await subscriber_tag_names(db, subscriber_id))


# ---------- csv import / export ----------


@router.post("/lists/{ref}/subscribers/import")
async def import_subscribers(request: Request, ref: str, body: ImportIn):
    db = request.app.state.db
    lst = await get_list(db, ref)
    reader = csv.reader(io.StringIO(body.csv))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise HTTPException(status_code=422, detail="empty CSV")

    header = [c.strip().lower() for c in rows[0]]
    if "email" in header:
        col = {name: header.index(name) for name in ("email", "name", "tags", "status") if name in header}
        data_rows = rows[1:]
    else:
        # Headerless: first column email, optional second column name.
        col = {"email": 0, "name": 1} if len(rows[0]) > 1 else {"email": 0}
        data_rows = rows

    def cell(row: list[str], name: str) -> str:
        idx = col.get(name)
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    imported, skipped_existing, skipped_invalid = 0, 0, 0
    now = utcnow()
    for row in data_rows:
        email = normalize_email(cell(row, "email"))
        if not valid_email(email):
            skipped_invalid += 1
            continue
        status = cell(row, "status") or "active"
        if status not in SUBSCRIBER_STATUSES:
            status = "active"
        async with db.execute(
            "SELECT 1 FROM subscribers WHERE list_id = ? AND email = ?", (lst["id"], email)
        ) as cur:
            if await cur.fetchone():
                skipped_existing += 1
                continue
        sub_id = new_id("sub")
        await db.execute(
            "INSERT INTO subscribers (id, list_id, email, name, status, created_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sub_id, lst["id"], email, cell(row, "name"), status, now, now if status == "active" else None),
        )
        tags = [t for t in cell(row, "tags").split(";") if t.strip()]
        if tags:
            await _attach_tags(db, lst["id"], sub_id, tags)
        imported += 1
    await db.commit()
    return {"imported": imported, "skipped_existing": skipped_existing, "skipped_invalid": skipped_invalid}


@router.get("/lists/{ref}/subscribers/export")
async def export_subscribers(request: Request, ref: str):
    db = request.app.state.db
    lst = await get_list(db, ref)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["email", "name", "status", "tags", "created_at"])
    async with db.execute(
        "SELECT * FROM subscribers WHERE list_id = ? ORDER BY created_at", (lst["id"],)
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        tags = await subscriber_tag_names(db, row["id"])
        writer.writerow([row["email"], row["name"], row["status"], ";".join(tags), row["created_at"]])
    return Response(content=out.getvalue(), media_type="text/csv")


# ---------- tags ----------


@router.get("/lists/{ref}/tags")
async def list_tags(request: Request, ref: str):
    db = request.app.state.db
    lst = await get_list(db, ref)
    async with db.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM subscriber_tags st WHERE st.tag_id = t.id) AS subscriber_count "
        "FROM tags t WHERE t.list_id = ? ORDER BY t.name",
        (lst["id"],),
    ) as cur:
        rows = await cur.fetchall()
    return {"tags": [dict(r) for r in rows]}


@router.post("/lists/{ref}/tags", status_code=201)
async def create_tag(request: Request, ref: str, body: TagIn):
    db = request.app.state.db
    lst = await get_list(db, ref)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="tag name is required")
    if await get_tag(db, lst["id"], name):
        raise HTTPException(status_code=409, detail=f"tag already exists: {name}")
    tag_id = new_id("tag")
    await db.execute(
        "INSERT INTO tags (id, list_id, name, created_at) VALUES (?, ?, ?, ?)",
        (tag_id, lst["id"], name, utcnow()),
    )
    await db.commit()
    return {"id": tag_id, "list_id": lst["id"], "name": name}


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(request: Request, tag_id: str):
    db = request.app.state.db
    async with db.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail=f"tag not found: {tag_id}")
    await db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    await db.commit()
    return Response(status_code=204)


# ---------- campaigns ----------


async def _campaign_tag_name(db, row) -> str | None:
    if not row["tag_id"]:
        return None
    async with db.execute("SELECT name FROM tags WHERE id = ?", (row["tag_id"],)) as cur:
        tag = await cur.fetchone()
    return tag["name"] if tag else None


async def _resolve_tag_id(db, list_id: str, tag_ref: str | None) -> str | None:
    if tag_ref is None or tag_ref == "":
        return None
    tag_row = await get_tag(db, list_id, tag_ref)
    if tag_row is None:
        raise HTTPException(status_code=404, detail=f"tag not found: {tag_ref}")
    return tag_row["id"]


@router.post("/lists/{ref}/campaigns", status_code=201)
async def create_campaign(request: Request, ref: str, body: CampaignIn):
    db = request.app.state.db
    lst = await get_list(db, ref)
    if not body.subject.strip():
        raise HTTPException(status_code=422, detail="subject is required")
    if not body.body_markdown.strip():
        raise HTTPException(status_code=422, detail="body_markdown is required")
    tag_id = await _resolve_tag_id(db, lst["id"], body.tag)
    campaign_id = new_id("cmp")
    await db.execute(
        "INSERT INTO campaigns (id, list_id, subject, body_markdown, tag_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (campaign_id, lst["id"], body.subject.strip(), body.body_markdown, tag_id, utcnow()),
    )
    await db.commit()
    row = await get_campaign(db, campaign_id)
    return campaign_json(row, await _campaign_tag_name(db, row))


@router.get("/campaigns")
async def list_campaigns(request: Request, list: str | None = None, status: str | None = None):
    db = request.app.state.db
    where, params = ["1=1"], []
    if list:
        lst = await get_list(db, list)
        where.append("c.list_id = ?")
        params.append(lst["id"])
    if status:
        where.append("c.status = ?")
        params.append(status)
    async with db.execute(
        f"SELECT c.* FROM campaigns c WHERE {' AND '.join(where)} ORDER BY c.created_at DESC", params
    ) as cur:
        rows = await cur.fetchall()
    return {"campaigns": [campaign_json(r, await _campaign_tag_name(db, r)) for r in rows]}


@router.get("/campaigns/{campaign_id}")
async def get_campaign_detail(request: Request, campaign_id: str):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    result = campaign_json(row, await _campaign_tag_name(db, row))
    if row["status"] != "draft":
        result["stats"] = await campaign_stats(db, campaign_id)
    return result


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(request: Request, campaign_id: str, body: CampaignPatch):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    if row["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"campaign is {row['status']}; only drafts can be edited")
    subject = body.subject.strip() if body.subject is not None else row["subject"]
    markdown = body.body_markdown if body.body_markdown is not None else row["body_markdown"]
    tag_id = await _resolve_tag_id(db, row["list_id"], body.tag) if body.tag is not None else row["tag_id"]
    await db.execute(
        "UPDATE campaigns SET subject = ?, body_markdown = ?, tag_id = ?, test_sent_at = NULL WHERE id = ?",
        (subject, markdown, tag_id, campaign_id),
    )
    await db.commit()
    row = await get_campaign(db, campaign_id)
    return campaign_json(row, await _campaign_tag_name(db, row))


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(request: Request, campaign_id: str):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    if row["status"] == "sending":
        raise HTTPException(status_code=409, detail="campaign is currently sending; cannot delete")
    await db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    await db.commit()
    return Response(status_code=204)


def _render_campaign_preview(request: Request, row, lst) -> tuple[str, str, str]:
    settings = request.app.state.settings
    return build_email(
        row["subject"],
        row["body_markdown"],
        name="Sam",
        email="sample@example.com",
        unsubscribe_url=f"{settings.base_url}/unsubscribe/preview",
        list_name=lst["name"],
        postal_address=settings.postal_address,
    )


@router.get("/campaigns/{campaign_id}/preview")
async def preview_campaign(request: Request, campaign_id: str):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    lst = await get_list(db, row["list_id"])
    subject, html, text = _render_campaign_preview(request, row, lst)
    return {"subject": subject, "html": html, "text": text}


@router.post("/campaigns/{campaign_id}/test")
async def test_send_campaign(request: Request, campaign_id: str, body: TestSendIn):
    db = request.app.state.db
    settings = request.app.state.settings
    row = await get_campaign(db, campaign_id)
    lst = await get_list(db, row["list_id"])
    to = normalize_email(body.to)
    if not valid_email(to):
        raise HTTPException(status_code=422, detail=f"invalid email: {body.to}")
    subject, html, text = _render_campaign_preview(request, row, lst)
    try:
        message_id = await request.app.state.ses.send_email(
            to,
            f"[test] {subject}",
            html,
            text,
            from_email=settings.from_email,
            from_name=settings.from_name,
        )
    except Exception as exc:  # surface SES failures as a client-visible error
        raise HTTPException(status_code=502, detail=f"test send failed: {exc}") from exc
    await db.execute("UPDATE campaigns SET test_sent_at = ? WHERE id = ?", (utcnow(), campaign_id))
    await db.commit()
    return {"sent_to": to, "ses_message_id": message_id}


def _audience_query(list_id: str, tag_id: str | None) -> tuple[str, list]:
    sql = (
        "FROM subscribers s WHERE s.list_id = ? AND s.status = 'active' "
        "AND NOT EXISTS (SELECT 1 FROM suppressions sup WHERE sup.email = s.email)"
    )
    params: list = [list_id]
    if tag_id:
        sql += (
            " AND EXISTS (SELECT 1 FROM subscriber_tags st WHERE st.subscriber_id = s.id AND st.tag_id = ?)"
        )
        params.append(tag_id)
    return sql, params


@router.post("/campaigns/{campaign_id}/send")
async def send_campaign(request: Request, campaign_id: str, body: SendIn):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    if row["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"campaign is already {row['status']}")

    audience_sql, params = _audience_query(row["list_id"], row["tag_id"])
    async with db.execute(f"SELECT COUNT(*) AS n {audience_sql}", params) as cur:
        recipient_count = (await cur.fetchone())["n"]

    if body.dry_run:
        async with db.execute(f"SELECT s.email {audience_sql} LIMIT 10", params) as cur:
            sample = [r["email"] for r in await cur.fetchall()]
        return {"dry_run": True, "recipient_count": recipient_count, "sample": sample}

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=f"this would email {recipient_count} people; pass confirm=true to proceed "
            "(or dry_run=true to see the audience)",
        )
    if row["test_sent_at"] is None and not body.skip_test:
        raise HTTPException(
            status_code=412,
            detail="no test send yet; POST /test with your address first, or pass skip_test=true",
        )
    if recipient_count == 0:
        raise HTTPException(status_code=400, detail="audience is empty; nothing to send")

    now = utcnow()
    await db.execute(
        f"INSERT INTO campaign_recipients (campaign_id, subscriber_id, email, name, status, updated_at) "
        f"SELECT ?, s.id, s.email, s.name, 'pending', ? {audience_sql}",
        [campaign_id, now, *params],
    )
    await db.execute(
        "UPDATE campaigns SET status = 'sending', recipient_count = ?, started_at = ? WHERE id = ?",
        (recipient_count, now, campaign_id),
    )
    await db.commit()
    request.app.state.sender.wake()
    row = await get_campaign(db, campaign_id)
    return campaign_json(row, await _campaign_tag_name(db, row))


@router.get("/campaigns/{campaign_id}/stats")
async def get_campaign_stats(request: Request, campaign_id: str):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    return {
        "id": campaign_id,
        "status": row["status"],
        "recipient_count": row["recipient_count"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        **await campaign_stats(db, campaign_id),
    }


# ---------- suppressions ----------


@router.get("/suppressions")
async def list_suppressions(
    request: Request, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)
):
    db = request.app.state.db
    async with db.execute("SELECT COUNT(*) AS n FROM suppressions") as cur:
        total = (await cur.fetchone())["n"]
    async with db.execute(
        "SELECT * FROM suppressions ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
    ) as cur:
        rows = await cur.fetchall()
    return {"suppressions": [dict(r) for r in rows], "total": total}


@router.post("/suppressions", status_code=201)
async def add_suppression(request: Request, body: SuppressionIn):
    db = request.app.state.db
    email = normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(status_code=422, detail=f"invalid email: {body.email}")
    await db.execute(
        "INSERT OR REPLACE INTO suppressions (email, reason, detail, created_at) VALUES (?, ?, ?, ?)",
        (email, body.reason, "added via API", utcnow()),
    )
    await db.commit()
    return {"email": email, "reason": body.reason}


@router.delete("/suppressions/{email}", status_code=204)
async def remove_suppression(request: Request, email: str):
    db = request.app.state.db
    await db.execute("DELETE FROM suppressions WHERE email = ?", (normalize_email(email),))
    await db.commit()
    return Response(status_code=204)


# ---------- doctor ----------


@router.get("/doctor")
async def run_doctor(request: Request):
    return await doctor_mod.run_checks(
        request.app.state.settings, request.app.state.ses, request.app.state.db
    )
