"""Absolut Greyhound para Streamlit.

Funciona con SQLite local o con PostgreSQL mediante DATABASE_URL en Streamlit
Secrets. Para Gmail usa una contraseña de aplicación, nunca la contraseña normal.
"""
from __future__ import annotations

import base64
import io
import imaplib
import json
import os
import random
import re
import smtplib
import time
import uuid
from datetime import date
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text


st.set_page_config(page_title="Absolut Greyhound", page_icon="⚡", layout="wide")


def setting(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, os.getenv(name, default)))
    except Exception:
        return os.getenv(name, default)


@st.cache_resource
def engine_for(url: str):
    return create_engine(url, pool_pre_ping=True)


DATABASE_URL = setting("DATABASE_URL", "sqlite:///greyhound_streamlit.sqlite")
DB_STARTUP_ERROR = ""
try:
    ENGINE = engine_for(DATABASE_URL)
    with ENGINE.connect() as connection:
        connection.execute(text("SELECT 1"))
except Exception as exc:
    # A bad/missing hosted secret must not produce a blank Streamlit crash page.
    # Fall back to local SQLite so the UI remains usable while the operator fixes it.
    DB_STARTUP_ERROR = str(exc)
    DATABASE_URL = "sqlite:///greyhound_streamlit_fallback.sqlite"
    ENGINE = engine_for(DATABASE_URL)
IS_POSTGRES = DATABASE_URL.startswith("postgres")
ID = "BIGSERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def rows(sql: str, **params):
    with ENGINE.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def row(sql: str, **params):
    found = rows(sql, **params)
    return found[0] if found else None


def execute(sql: str, **params):
    with ENGINE.begin() as conn:
        return conn.execute(text(sql), params)


def init_db():
    statements = [
        f"""CREATE TABLE IF NOT EXISTS accounts (
            id {ID}, email TEXT UNIQUE NOT NULL, smtp_host TEXT NOT NULL,
            smtp_port INTEGER NOT NULL DEFAULT 587, smtp_user TEXT NOT NULL,
            smtp_pass TEXT NOT NULL, imap_host TEXT NOT NULL,
            imap_port INTEGER NOT NULL DEFAULT 993, imap_user TEXT NOT NULL,
            imap_pass TEXT NOT NULL, daily_limit INTEGER NOT NULL DEFAULT 100,
            emails_sent_today INTEGER NOT NULL DEFAULT 0, daily_sent_date TEXT,
            status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        f"""CREATE TABLE IF NOT EXISTS campaigns (
            id {ID}, name TEXT NOT NULL, subject_template TEXT NOT NULL,
            body_html_template TEXT NOT NULL, personalization_enabled INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft', attachments_json TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        f"""CREATE TABLE IF NOT EXISTS leads (
            id {ID}, campaign_id INTEGER NOT NULL, email TEXT NOT NULL,
            variables TEXT NOT NULL DEFAULT '{{}}', status TEXT NOT NULL DEFAULT 'pending',
            bounce_reason TEXT, opened_at TIMESTAMP, open_count INTEGER NOT NULL DEFAULT 0,
            tracking_token TEXT UNIQUE, opted_out_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(campaign_id, email))""",
        f"""CREATE TABLE IF NOT EXISTS messages (
            id {ID}, message_id_header TEXT UNIQUE, in_reply_to_header TEXT,
            account_id INTEGER, lead_id INTEGER, direction TEXT NOT NULL,
            subject TEXT, body_html TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            read_at TIMESTAMP)""",
        "CREATE TABLE IF NOT EXISTS suppressions (email TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS imap_processed_messages (account_id INTEGER NOT NULL, message_id_header TEXT NOT NULL, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(account_id, message_id_header))",
        f"""CREATE TABLE IF NOT EXISTS tracking_events (
            id {ID}, lead_id INTEGER NOT NULL, user_agent TEXT, ip_hash TEXT,
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    ]
    for statement in statements:
        execute(statement)


try:
    init_db()
except Exception as exc:
    DB_STARTUP_ERROR = DB_STARTUP_ERROR or str(exc)


def show_startup_status():
    if DB_STARTUP_ERROR:
        st.warning("La conexión configurada no está disponible. Greyhound está funcionando con una base local temporal; configura DATABASE_URL para conservar los datos en la nube.")


show_startup_status()


def decode_mime(value) -> str:
    if not value:
        return ""
    return "".join((part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part)
                   for part, charset in decode_header(str(value)))


def html_to_text(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value or "").replace("&nbsp;", " ")


def render_template(value: str, lead: dict) -> str:
    variables = json.loads(lead.get("variables") or "{}")
    variables["email"] = lead["email"]
    return re.sub(r"{{\s*([^}\s]+)\s*}}", lambda m: str(variables.get(m.group(1), "")), value or "")


def reset_account(account: dict):
    today = date.today().isoformat()
    if account.get("daily_sent_date") == today:
        return account
    new_limit = min(400, max(100, int(account["daily_limit"] or 100) + (50 if account.get("daily_sent_date") else 0)))
    execute("UPDATE accounts SET emails_sent_today=0, daily_sent_date=:today, daily_limit=:limit WHERE id=:id",
            today=today, limit=new_limit, id=account["id"])
    account.update(emails_sent_today=0, daily_sent_date=today, daily_limit=new_limit)
    return account


def available_accounts():
    result = []
    for account in rows("SELECT * FROM accounts WHERE status='active' ORDER BY id"):
        account = reset_account(account)
        if account["emails_sent_today"] < account["daily_limit"]:
            result.append(account)
    return result


def mail_message(account: dict, lead: dict, campaign: dict) -> tuple[EmailMessage, str]:
    token = lead.get("tracking_token") or uuid.uuid4().hex
    if not lead.get("tracking_token"):
        execute("UPDATE leads SET tracking_token=:token WHERE id=:id", token=token, id=lead["id"])
    subject = render_template(campaign["subject_template"], lead) if campaign["personalization_enabled"] else campaign["subject_template"]
    html = render_template(campaign["body_html_template"], lead) if campaign["personalization_enabled"] else campaign["body_html_template"]
    tracking_base = setting("TRACKING_BASE_URL")
    if tracking_base:
        html += f'<img src="{tracking_base.rstrip("/")}/pixel/{token}" width="1" height="1" alt="" style="display:none">'
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = account["email"], lead["email"], subject
    message["Message-ID"] = make_msgid(domain=account["email"].split("@")[-1])
    message.set_content(html_to_text(html))
    message.add_alternative(html, subtype="html")
    for attachment in json.loads(campaign.get("attachments_json") or "[]"):
        raw = base64.b64decode(attachment["content"])
        main, sub = attachment.get("mime", "image/png").split("/", 1)
        message.add_attachment(raw, maintype=main, subtype=sub, filename=attachment["name"])
    return message, html


def smtp_send(account: dict, message: EmailMessage):
    with smtplib.SMTP(account["smtp_host"], int(account["smtp_port"]), timeout=30) as client:
        client.ehlo()
        if int(account["smtp_port"]) != 465:
            client.starttls()
            client.ehlo()
        client.login(account["smtp_user"], account["smtp_pass"])
        client.send_message(message)


def send_campaign(campaign_id: int, maximum: int, progress):
    campaign = row("SELECT * FROM campaigns WHERE id=:id", id=campaign_id)
    leads = rows("""SELECT l.* FROM leads l WHERE l.campaign_id=:campaign_id AND l.status='pending'
                  AND l.opted_out_at IS NULL AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.email=l.email)
                  ORDER BY l.id LIMIT :maximum""", campaign_id=campaign_id, maximum=maximum)
    accounts = available_accounts()
    sent = failed = 0
    for index, lead in enumerate(leads):
        accounts = available_accounts()
        if not accounts:
            break
        account = accounts[index % len(accounts)]
        try:
            message, html = mail_message(account, lead, campaign)
            smtp_send(account, message)
            execute("""INSERT INTO messages(message_id_header,account_id,lead_id,direction,subject,body_html)
                       VALUES(:message_id,:account_id,:lead_id,'outbound',:subject,:body)""",
                    message_id=message["Message-ID"], account_id=account["id"], lead_id=lead["id"],
                    subject=str(message["Subject"]), body=html)
            execute("UPDATE leads SET status='sent' WHERE id=:id", id=lead["id"])
            execute("UPDATE accounts SET emails_sent_today=emails_sent_today+1 WHERE id=:id", id=account["id"])
            sent += 1
        except Exception as exc:  # El error queda visible, pero la campaña continúa.
            execute("UPDATE leads SET status='send_failed', bounce_reason=:reason WHERE id=:id", reason=str(exc)[:500], id=lead["id"])
            failed += 1
        progress.progress((index + 1) / max(len(leads), 1), text=f"Procesando {index + 1} de {len(leads)}")
        if index < len(leads) - 1:
            time.sleep(random.uniform(5, 10))
    return sent, failed, max(0, len(leads) - sent - failed)


def sync_inbox():
    stored = replied = bounced = 0
    for account in rows("SELECT * FROM accounts WHERE status='active'"):
        try:
            client = imaplib.IMAP4_SSL(account["imap_host"], int(account["imap_port"]))
            client.login(account["imap_user"], account["imap_pass"])
            client.select("INBOX")
            _, data = client.search(None, "ALL")
            for number in data[0].split()[-200:]:
                _, content = client.fetch(number, "(RFC822)")
                source = next((part[1] for part in content if isinstance(part, tuple)), b"")
                incoming = message_from_bytes(source)
                message_id = incoming.get("Message-ID", "")
                if not message_id or row("SELECT 1 FROM imap_processed_messages WHERE account_id=:a AND message_id_header=:m", a=account["id"], m=message_id):
                    continue
                headers = f"{incoming.get('In-Reply-To', '')} {incoming.get('References', '')}"
                original = row("SELECT * FROM messages WHERE direction='outbound' AND message_id_header IN (:one)", one=incoming.get("In-Reply-To", ""))
                if not original:
                    for candidate in re.findall(r"<[^<>\s]+>", headers):
                        original = row("SELECT * FROM messages WHERE direction='outbound' AND message_id_header=:m", m=candidate)
                        if original:
                            break
                sender = parseaddr(incoming.get("From", ""))[1].lower()
                subject = decode_mime(incoming.get("Subject", ""))
                is_bounce = bool(re.search(r"postmaster|mailer-daemon|delivery status|undeliverable", sender + " " + subject, re.I))
                if original:
                    body = source.decode("utf-8", errors="replace")
                    execute("""INSERT INTO messages(message_id_header,in_reply_to_header,account_id,lead_id,direction,subject,body_html)
                               VALUES(:m,:reply,:a,:lead,'inbound',:subject,:body)""",
                            m=message_id, reply=incoming.get("In-Reply-To"), a=account["id"], lead=original["lead_id"], subject=subject, body=body)
                    if is_bounce:
                        lead = row("SELECT email FROM leads WHERE id=:id", id=original["lead_id"])
                        execute("UPDATE leads SET status='bounced', bounce_reason=:reason WHERE id=:id", reason=subject, id=original["lead_id"])
                        execute("INSERT INTO suppressions(email,reason) VALUES(:email,:reason) ON CONFLICT(email) DO NOTHING", email=lead["email"], reason=subject)
                        bounced += 1
                    else:
                        execute("UPDATE leads SET status='replied' WHERE id=:id", id=original["lead_id"])
                        replied += 1
                    stored += 1
                execute("INSERT INTO imap_processed_messages(account_id,message_id_header) VALUES(:a,:m)", a=account["id"], m=message_id)
            client.logout()
        except Exception as exc:
            st.warning(f"No se pudo sincronizar {account['email']}: {exc}")
    return stored, replied, bounced


def dashboard():
    summary = {
        "Campañas": row("SELECT COUNT(*) AS n FROM campaigns")["n"],
        "Enviados": row("SELECT COUNT(*) AS n FROM leads WHERE status='sent'")["n"],
        "Respondidos": row("SELECT COUNT(*) AS n FROM leads WHERE status='replied'")["n"],
        "Rebotados": row("SELECT COUNT(*) AS n FROM leads WHERE status='bounced'")["n"],
    }
    st.title("⚡ Absolut Greyhound")
    st.caption("Cold email B2B con control local de cuentas, campañas y conversaciones.")
    columns = st.columns(4)
    for column, (label, value) in zip(columns, summary.items()):
        column.metric(label, value)
    st.subheader("Actividad reciente")
    st.dataframe(pd.DataFrame(rows("SELECT direction,subject,timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")), use_container_width=True, hide_index=True)


def accounts_page():
    st.header("Cuentas Google")
    st.info("Usa una contraseña de aplicación de Google y activa IMAP en Gmail. No uses tu contraseña habitual.")
    with st.form("account"):
        email = st.text_input("Correo Gmail")
        password = st.text_input("Contraseña de aplicación", type="password")
        submitted = st.form_submit_button("Agregar cuenta")
    if submitted:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            st.error("Ingresa un correo válido.")
        elif not password:
            st.error("Ingresa la contraseña de aplicación.")
        else:
            execute("""INSERT INTO accounts(email,smtp_host,smtp_port,smtp_user,smtp_pass,imap_host,imap_port,imap_user,imap_pass)
                       VALUES(:email,'smtp.gmail.com',587,:email,:password,'imap.gmail.com',993,:email,:password)
                       ON CONFLICT(email) DO UPDATE SET smtp_pass=:password,imap_pass=:password,status='active'""", email=email.lower(), password=password)
            st.success("Cuenta guardada.")
    account_rows = rows("SELECT id,email,daily_limit,emails_sent_today,status,created_at FROM accounts ORDER BY id DESC")
    st.dataframe(pd.DataFrame(account_rows), use_container_width=True, hide_index=True)
    if account_rows:
        target = st.selectbox("Cuenta a desactivar o eliminar", account_rows, format_func=lambda item: item["email"])
        a, b = st.columns(2)
        if a.button("Desactivar cuenta"):
            execute("UPDATE accounts SET status='blocked' WHERE id=:id", id=target["id"])
            st.rerun()
        if b.button("Eliminar cuenta", type="secondary"):
            execute("DELETE FROM accounts WHERE id=:id", id=target["id"])
            st.rerun()


def campaigns_page():
    st.header("Campañas")
    with st.form("campaign"):
        name = st.text_input("Nombre de campaña")
        subject = st.text_input("Asunto")
        body = st.text_area("Mensaje", height=220, placeholder="Hola {{nombre}}, ...")
        personalize = st.checkbox("Personalizar con {{nombre}} y {{email}}", value=True)
        uploads = st.file_uploader("Imágenes adjuntas", type=["png", "jpg", "jpeg", "gif", "webp"], accept_multiple_files=True)
        create = st.form_submit_button("Crear campaña")
    if create:
        if not name or not subject or not body:
            st.error("Nombre, asunto y mensaje son obligatorios.")
        else:
            attachments = [{"name": item.name, "mime": item.type or "image/png", "content": base64.b64encode(item.getvalue()).decode()} for item in uploads]
            execute("""INSERT INTO campaigns(name,subject_template,body_html_template,personalization_enabled,attachments_json)
                       VALUES(:name,:subject,:body,:personalize,:attachments)""", name=name, subject=subject, body=body, personalize=int(personalize), attachments=json.dumps(attachments))
            st.success("Campaña creada.")
    campaign_rows = rows("SELECT id,name,status,created_at FROM campaigns ORDER BY id DESC")
    st.dataframe(pd.DataFrame(campaign_rows), use_container_width=True, hide_index=True)


def contacts_page():
    st.header("Contactos")
    campaigns = rows("SELECT id,name FROM campaigns ORDER BY id DESC")
    if not campaigns:
        st.info("Primero crea una campaña.")
        return
    campaign = st.selectbox("Campaña", campaigns, format_func=lambda item: item["name"])
    source = st.file_uploader("Base de contactos: solo columnas email y nombre", type=["csv", "xlsx"])
    if source and st.button("Importar contactos"):
        frame = pd.read_csv(source) if source.name.lower().endswith(".csv") else pd.read_excel(source)
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        if "email" not in frame.columns:
            st.error("El archivo debe incluir la columna email.")
            return
        imported = skipped = 0
        for _, contact in frame.iterrows():
            email = str(contact.get("email", "")).strip().lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                skipped += 1
                continue
            try:
                execute("INSERT INTO leads(campaign_id,email,variables) VALUES(:campaign,:email,:variables)", campaign=campaign["id"], email=email, variables=json.dumps({"nombre": str(contact.get("nombre", "")).strip()}))
                imported += 1
            except Exception:
                skipped += 1
        st.success(f"Importados: {imported}. Omitidos: {skipped}.")
    contacts = rows("SELECT email,status,variables,opened_at,open_count FROM leads WHERE campaign_id=:id ORDER BY id DESC", id=campaign["id"])
    st.dataframe(pd.DataFrame(contacts), use_container_width=True, hide_index=True)


def send_page():
    st.header("Enviar campaña")
    campaigns = rows("SELECT id,name,status FROM campaigns ORDER BY id DESC")
    if not campaigns:
        st.info("No hay campañas disponibles.")
        return
    campaign = st.selectbox("Campaña a enviar", campaigns, format_func=lambda item: item["name"])
    maximum = st.number_input("Correos a procesar ahora", min_value=1, max_value=100, value=10)
    pending = row("SELECT COUNT(*) AS n FROM leads WHERE campaign_id=:id AND status='pending'", id=campaign["id"])["n"]
    st.caption(f"Pendientes: {pending}. El sistema espera aleatoriamente entre 5 y 10 segundos por cada correo.")
    if st.button("⚡ Iniciar envío", type="primary"):
        execute("UPDATE campaigns SET status='active' WHERE id=:id", id=campaign["id"])
        progress = st.progress(0, text="Preparando envío...")
        sent, failed, remaining = send_campaign(campaign["id"], int(maximum), progress)
        st.success(f"Enviados: {sent} · Fallidos: {failed} · Pendientes: {remaining}")


def inbox_page():
    st.header("Bandeja maestra")
    if st.button("Sincronizar Gmail ahora"):
        stored, replied, bounced = sync_inbox()
        st.success(f"Sincronizados: {stored} · Respuestas: {replied} · Rebotes: {bounced}")
    messages = rows("""SELECT m.id,m.direction,m.subject,m.timestamp,l.email,m.body_html,m.account_id,m.lead_id
                     FROM messages m LEFT JOIN leads l ON l.id=m.lead_id ORDER BY m.timestamp DESC LIMIT 100""")
    if not messages:
        st.info("Aún no hay mensajes.")
        return
    st.dataframe(pd.DataFrame([{key: value for key, value in item.items() if key != "body_html"} for item in messages]), use_container_width=True, hide_index=True)
    inbound = [item for item in messages if item["direction"] == "inbound"]
    if inbound:
        chosen = st.selectbox("Abrir respuesta", inbound, format_func=lambda item: f"{item['email']} · {item['subject']}")
        st.text_area("Contenido recibido", chosen["body_html"], height=180, disabled=True)
        reply = st.text_area("Responder", key=f"reply_{chosen['id']}")
        if st.button("Enviar respuesta") and reply.strip():
            account = row("SELECT * FROM accounts WHERE id=:id AND status='active'", id=chosen["account_id"])
            lead = row("SELECT * FROM leads WHERE id=:id", id=chosen["lead_id"])
            if not account or not lead:
                st.error("No hay una cuenta activa para responder.")
                return
            message = EmailMessage()
            message["From"], message["To"], message["Subject"] = account["email"], lead["email"], "Re: " + (chosen["subject"] or "Mensaje")
            message.set_content(reply)
            try:
                smtp_send(account, message)
                execute("INSERT INTO messages(message_id_header,account_id,lead_id,direction,subject,body_html) VALUES(:m,:a,:l,'outbound',:s,:b)", m=message["Message-ID"] or make_msgid(), a=account["id"], l=lead["id"], s=message["Subject"], b=reply)
                st.success("Respuesta enviada.")
            except Exception as exc:
                st.error(f"No se pudo enviar: {exc}")


def settings_page():
    st.header("Configuración y seguridad")
    st.write("Para Streamlit Cloud configura `DATABASE_URL` y `TRACKING_BASE_URL` en Secrets. El archivo `.streamlit/secrets.toml` no debe subirse a GitHub.")
    if st.checkbox("Entiendo que eliminaré campañas, contactos y mensajes") and st.button("Eliminar datos", type="secondary"):
        for table in ["messages", "tracking_events", "imap_processed_messages", "leads", "campaigns", "suppressions", "accounts"]:
            execute(f"DELETE FROM {table}")
        st.success("Datos eliminados.")


PAGES = {"Resumen": dashboard, "Cuentas": accounts_page, "Campañas": campaigns_page, "Contactos": contacts_page, "Enviar": send_page, "Bandeja": inbox_page, "Configuración": settings_page}
with st.sidebar:
    try:
        st.image("preview/assets/absolut-greyhound-logo.png", use_container_width=True)
    except Exception:
        st.markdown("## ⚡ Greyhound")
    st.title("ABSOLUT\nGREYHOUND")
    selected = st.radio("", list(PAGES), label_visibility="collapsed")
    st.caption("Streamlit Edition · Gmail SMTP/IMAP")

try:
    PAGES[selected]()
except Exception as exc:
    # Keep the app alive when a single view encounters malformed data or a
    # transient provider error. The full detail stays in the server logs.
    st.error("Greyhound no pudo completar esta vista. Revisa la configuración y vuelve a intentarlo.")
    st.caption(f"Detalle: {type(exc).__name__}")
