import os
import time
import threading
import subprocess
import uuid
import requests
import docker
import google.generativeai as genai
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import logging
import json
from datetime import datetime

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

# Setup logger
logger = logging.getLogger("ai_watchdog")
logger.setLevel(logging.INFO)
log_handler = logging.StreamHandler()
log_handler.setFormatter(JsonFormatter())
logger.addHandler(log_handler)

# Initialize configurations
DOCKER_SOCKET = "unix:///var/run/docker.sock"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ALERT_EMAIL = os.getenv("ALERT_EMAIL")
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8080")

# SMTP Server Configurations
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")

import hmac
import hashlib

WATCHDOG_SECRET = os.getenv("WATCHDOG_SECRET", "default_fallback_secret")

def generate_signed_url(action_id: str, expiration_seconds: int = 7200):  # Link expires in 2 hours
    expires_at = int(time.time()) + expiration_seconds
    message = f"{action_id}:{expires_at}".encode('utf-8')
    signature = hmac.new(WATCHDOG_SECRET.encode('utf-8'), message, hashlib.sha256).hexdigest()
    return f"signature={signature}&expires={expires_at}"

def verify_signed_url(action_id: str, signature: str, expires: str) -> bool:
    try:
        expires_at = int(expires)
    except (ValueError, TypeError):
        return False
        
    # Check expiration
    if time.time() > expires_at:
        print("[WARNING] Signature verification failed: Link has expired.")
        return False
        
    # Recalculate signature
    message = f"{action_id}:{expires_at}".encode('utf-8')
    expected_signature = hmac.new(WATCHDOG_SECRET.encode('utf-8'), message, hashlib.sha256).hexdigest()
    
    # Timing-attack safe comparison
    return hmac.compare_digest(expected_signature, signature)

genai.configure(api_key=GEMINI_API_KEY)
docker_client = docker.DockerClient(base_url=DOCKER_SOCKET)
app = FastAPI(title="AI Self-Healing Watchdog")

DB_PATH = "/app/data/watchdog.db"
try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except Exception:
    DB_PATH = "watchdog.db"

import sqlite3

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY,
            container_name TEXT,
            command TEXT,
            diagnosis TEXT,
            status TEXT,
            created_at TEXT
        )
    """)
    # Migrate existing databases by adding the created_at column if missing
    try:
        cursor.execute("ALTER TABLE actions ADD COLUMN created_at TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    conn.close()

init_db()

def save_action(action_id: str, container_name: str, command: str, diagnosis: str, status: str = "pending"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    
    # Check if action already exists to update it
    cursor.execute("SELECT id FROM actions WHERE id = ?", (action_id,))
    exists = cursor.fetchone()
    if exists:
        cursor.execute(
            "UPDATE actions SET container_name = ?, command = ?, diagnosis = ?, status = ? WHERE id = ?",
            (container_name, command, diagnosis, status, action_id)
        )
    else:
        cursor.execute(
            "INSERT INTO actions (id, container_name, command, diagnosis, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (action_id, container_name, command, diagnosis, status, now_str)
        )
    conn.commit()
    conn.close()

def get_action(action_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, container_name, command, diagnosis, status FROM actions WHERE id = ?", (action_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return RemediationAction(id=row[0], container_name=row[1], command=row[2], diagnosis=row[3], status=row[4])
    return None

def update_action_status(action_id: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE actions SET status = ? WHERE id = ?", (status, action_id))
    conn.commit()
    conn.close()

def validate_command(command: str) -> bool:
    """
    Validates that a command is safe to execute.
    Allowed format: docker <restart|start|stop> <valid_container_name>
    """
    parts = command.strip().split()
    if len(parts) != 3:
        return False
        
    if parts[0] != "docker":
        return False
        
    if parts[1] not in ["restart", "start", "stop"]:
        return False
        
    target_container = parts[2]
    
    # Query Docker daemon to verify the container exists on this host
    try:
        existing_containers = [c.name for c in docker_client.containers.list(all=True)]
    except Exception:
        # Fallback to static list of application containers if Docker daemon fails
        existing_containers = ["three-tier-backend", "three-tier-frontend", "three-tier-db"]
        
    return target_container in existing_containers

class RemediationAction(BaseModel):
    id: str
    container_name: str
    command: str
    diagnosis: str
    status: str = "pending"

def send_email_alert(subject: str, body: str):
    if not SENDER_EMAIL or not SENDER_PASSWORD or not ALERT_EMAIL:
        print("[WARNING] SMTP credentials or target alert email not configured. Skipping email send.")
        return

    try:
        # Create message container
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = ALERT_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        # Set up SMTP connection with TLS security
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        
        # Send mail and close connection
        server.sendmail(SENDER_EMAIL, ALERT_EMAIL, msg.as_string())
        server.close()
        print(f"[INFO] Alert email successfully sent to {ALERT_EMAIL}")
    except Exception as e:
        print(f"[ERROR] Failed to send email alert: {e}")

def send_alert(action_id: str, container_name: str, diagnosis: str, command: str, logs: str):
    """
    Sends an alert with approval links.
    """
    signature_params = generate_signed_url(action_id)
    approve_link = f"{AGENT_URL}/approve/{action_id}?{signature_params}"
    reject_link = f"{AGENT_URL}/reject/{action_id}?{signature_params}"
    
    email_body = f"""
    === SYSTEM ALERT ===
    Container '{container_name}' has encountered an error!
    
    DIAGNOSIS:
    {diagnosis}
    
    PROPOSED FIX ACTION:
    `{command}`
    
    CRITICAL LOGS:
    {logs[:1000]}
    
    Please review and approve:
    [APPROVE & FIX] -> {approve_link}
    [REJECT]        -> {reject_link}
    """
    print(email_body)
    
    subject = f"🚨 AI Self-Healing Alert: Container '{container_name}' crashed"
    send_email_alert(subject, email_body)

# def send_telegram_alert(action_id: str, container_name: str, diagnosis: str, command: str):
#     bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
#     chat_id = os.getenv("TELEGRAM_CHAT_ID")
#     if not bot_token or not chat_id:
#         print("[WARNING] Telegram credentials not configured. Skipping alert.")
#         return
#         
#     signature_params = generate_signed_url(action_id)
#     approve_link = f"{AGENT_URL}/approve/{action_id}?{signature_params}"
#     reject_link = f"{AGENT_URL}/reject/{action_id}?{signature_params}"
#     
#     text = (
#         f"🚨 *AI Watchdog Alert*\n\n"
#         f"*Container:* `{container_name}`\n"
#         f"*Diagnosis:* {diagnosis}\n"
#         f"*Proposed Fix:* `{command}`"
#     )
#     
#     reply_markup = {
#         "inline_keyboard": [
#             [
#                 {"text": "✔️ Approve & Fix", "url": approve_link},
#                 {"text": "❌ Reject", "url": reject_link}
#             ]
#         ]
#     }
#     
#     url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
#     payload = {
#         "chat_id": chat_id,
#         "text": text,
#         "parse_mode": "Markdown",
#         "reply_markup": reply_markup
#     }
#     
#     try:
#         response = requests.post(url, json=payload)
#         if response.status_code == 200:
#             print("[INFO] Telegram alert sent successfully.")
#         else:
#             print(f"[ERROR] Telegram returned status {response.status_code}: {response.text}")
#     except Exception as e:
#         print(f"[ERROR] Failed to send Telegram alert: {e}")

DEPENDENCY_MAP = {
    "three-tier-frontend": ["three-tier-backend"],
    "three-tier-backend": ["three-tier-db"]
}

def check_dependencies_healthy(container_name: str) -> tuple[bool, str]:
    """
    Checks if all upstream dependencies of container_name are running and healthy.
    Returns (True, "") if healthy, or (False, "error message") otherwise.
    """
    dependencies = DEPENDENCY_MAP.get(container_name, [])
    for dep in dependencies:
        try:
            dep_container = docker_client.containers.get(dep)
            state = dep_container.attrs.get("State", {})
            status = state.get("Status")
            
            if status != "running":
                return False, f"Dependency '{dep}' is not running (status: {status})."
                
            health = state.get("Health", {})
            if health:
                health_status = health.get("Status")
                if health_status != "healthy":
                    return False, f"Dependency '{dep}' is running but unhealthy (health status: {health_status})."
                    
        except docker.errors.NotFound:
            return False, f"Dependency '{dep}' container was not found on host."
        except Exception as e:
            return False, f"Failed to check health of dependency '{dep}': {e}"
            
    return True, ""

def send_slack_alert_deferred(container_name: str, error_msg: str):
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("[WARNING] Slack webhook URL not configured. Skipping deferred alert.")
        return
        
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "⚠️ AI Watchdog Remediation Deferred"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Container:* `{container_name}`\n*Status:* Remediation deferred because dependency health check failed.\n*Reason:* {error_msg}"
                }
            }
        ]
    }
    
    try:
        requests.post(webhook_url, json=payload)
        logger.info(f"Deferred Slack alert posted for {container_name}.")
    except Exception as e:
        logger.error(f"Failed to post deferred Slack alert: {e}")

def send_slack_alert(action_id: str, container_name: str, diagnosis: str, command: str):
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("[WARNING] Slack webhook URL not configured. Skipping alert.")
        return
        
    # Generate signature parameters for URLs
    signature_params = generate_signed_url(action_id)
    approve_url = f"{AGENT_URL}/approve/{action_id}?{signature_params}"
    reject_url = f"{AGENT_URL}/reject/{action_id}?{signature_params}"
    
    # Slack Block Kit payload
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨 AI Watchdog System Alert"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Container:* `{container_name}`\n*Diagnosis:* {diagnosis}\n*Proposed Fix:* `{command}`"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✔️ Approve & Fix"},
                        "style": "primary",
                        "url": approve_url
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject"},
                        "style": "danger",
                        "url": reject_url
                    }
                ]
            }
        ]
    }
    
    try:
        requests.post(webhook_url, json=payload)
        logger.info("Interactive Slack alert posted successfully.")
    except Exception as e:
        logger.error(f"Failed to post Slack alert: {e}")

def analyze_logs_and_remediate(container_name: str, logs: str):
    """
    Sends container logs to Gemini, asks for diagnosis and fix command.
    """
    # Check dependencies first
    is_healthy, dep_msg = check_dependencies_healthy(container_name)
    if not is_healthy:
        logger.warning(f"[REMEDIATION DEFERRED] Service '{container_name}' cannot be recovered: {dep_msg}")
        send_slack_alert_deferred(container_name, dep_msg)
        return
        
    prompt = f"""
    You are an expert DevOps engineer monitoring a 3-tier web app.
    The container '{container_name}' just crashed or failed a health check.
    Here are the last 50 lines of logs:
    ---
    {logs}
    ---
    Analyze the logs and provide:
    1. A short diagnosis of the root cause.
    2. The exact single command to recover or restart the service safely (e.g. 'docker restart {container_name}').
    
    You MUST respond with a JSON object containing exactly these keys:
    {{
      "diagnosis": "A concise explanation of the root cause.",
      "command": "The exact docker command to recover the service."
    }}
    """
    
    try:
        model = genai.GenerativeModel(
            'gemini-3.5-flash',
            generation_config={"response_mime_type": "application/json"}
        )
        response = model.generate_content(prompt)
        response_text = response.text
        
        try:
            result = json.loads(response_text)
            diagnosis = result.get("diagnosis", "Unknown error")
            command = result.get("command", f"docker restart {container_name}")
        except Exception as json_err:
            logger.error(f"Failed to parse Gemini response as JSON: {json_err}. Raw response: {response_text}")
            diagnosis = "Unknown error (JSON parsing failed)"
            command = f"docker restart {container_name}"
            
        action_id = str(uuid.uuid4())
        save_action(action_id, container_name, command, diagnosis, "pending")
        
        # Send Notification
        send_slack_alert(action_id, container_name, diagnosis, command)
        
    except Exception as e:
        logger.error(f"Error during AI analysis: {e}")

def monitor_loop():
    """
    Background worker that listens for docker events and does periodic health checks.
    """
    logger.info("AI Watchdog monitor loop started...")
    # Listen to docker events
    for event in docker_client.events(decode=True):
        status = event.get("status") or event.get("Action")
        actor = event.get("Actor", {})
        attributes = actor.get("Attributes", {})
        container_name = attributes.get("name")
        
        # Ignore watchdog itself
        if container_name == "ai-watchdog":
            continue
            
        if status in ["die", "oom", "health_status: unhealthy"]:
            logger.critical(f"Container {container_name} triggered alert with status: {status}")
            try:
                container = docker_client.containers.get(container_name)
                logs = container.logs(tail=50).decode('utf-8')
            except Exception:
                logs = "Could not fetch container logs."
                
            # Trigger analysis asynchronously
            threading.Thread(
                target=analyze_logs_and_remediate,
                args=(container_name, logs)
            ).start()

# API Endpoints for Human-in-the-Loop Approvals
@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, container_name, command, diagnosis, status, created_at FROM actions ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    
    total_actions = len(rows)
    pending_count = sum(1 for r in rows if r[4] == "pending")
    success_count = sum(1 for r in rows if r[4] in ["success", "executing"])
    failed_count = sum(1 for r in rows if r[4] in ["failed", "error", "rejected"])
    
    rows_html = ""
    for r in rows:
        action_id, container_name, command, diagnosis, status, created_at = r
        badge_cls = f"badge-{status}"
        
        actions_cell = ""
        if status == "pending":
            sig_params = generate_signed_url(action_id)
            approve_url = f"/approve/{action_id}?{sig_params}"
            reject_url = f"/reject/{action_id}?{sig_params}"
            actions_cell = f"""
            <a href="{approve_url}" class="btn btn-approve">Approve</a>
            <a href="{reject_url}" class="btn btn-reject">Reject</a>
            """
        else:
            actions_cell = "<span style='color: #64748b;'>No action needed</span>"
            
        rows_html += f"""
        <tr>
            <td style="font-weight: 600; color: #38bdf8;">{container_name}</td>
            <td>{diagnosis}</td>
            <td><code>{command}</code></td>
            <td><span class="badge {badge_cls}">{status}</span></td>
            <td>{created_at}</td>
            <td>{actions_cell}</td>
        </tr>
        """
        
    if not rows_html:
        rows_html = "<tr><td colspan='6' style='text-align: center; color: #64748b; padding: 3rem;'>No incidents reported yet. System status normal.</td></tr>"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AI Watchdog Agent Dashboard</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
        <style>
            body {{
                background: radial-gradient(circle at top left, #121829, #0a0c16);
                color: #f1f5f9;
                font-family: 'Outfit', sans-serif;
                margin: 0;
                padding: 2rem;
                min-height: 100vh;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            header {{
                margin-bottom: 2.5rem;
                text-align: center;
            }}
            h1 {{
                font-size: 2.5rem;
                font-weight: 700;
                background: linear-gradient(135deg, #38bdf8, #818cf8);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                margin: 0 0 0.5rem 0;
            }}
            .subtitle {{
                color: #94a3b8;
                font-size: 1.1rem;
                margin: 0;
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 1.5rem;
                margin-bottom: 3rem;
            }}
            .stat-card {{
                background: rgba(30, 41, 59, 0.4);
                border: 1px solid rgba(255, 255, 255, 0.05);
                backdrop-filter: blur(12px);
                border-radius: 16px;
                padding: 1.5rem;
                text-align: center;
                box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.2);
                transition: transform 0.2s;
            }}
            .stat-card:hover {{
                transform: translateY(-4px);
                border-color: rgba(56, 189, 248, 0.2);
            }}
            .stat-val {{
                font-size: 2.2rem;
                font-weight: 800;
                color: #38bdf8;
                margin-bottom: 0.5rem;
            }}
            .stat-label {{
                font-size: 0.875rem;
                color: #94a3b8;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
            .incidents-card {{
                background: rgba(15, 23, 42, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 20px;
                padding: 2rem;
                box-shadow: 0 12px 40px 0 rgba(0, 0, 0, 0.3);
            }}
            h2 {{
                font-size: 1.5rem;
                margin-top: 0;
                margin-bottom: 1.5rem;
                font-weight: 600;
            }}
            .table-wrapper {{
                overflow-x: auto;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                text-align: left;
            }}
            th {{
                padding: 1rem;
                color: #94a3b8;
                font-size: 0.875rem;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            }}
            td {{
                padding: 1.25rem 1rem;
                border-bottom: 1px solid rgba(255, 255, 255, 0.05);
                font-size: 0.95rem;
            }}
            tr:hover {{
                background: rgba(255, 255, 255, 0.02);
            }}
            .badge {{
                display: inline-block;
                padding: 0.25rem 0.75rem;
                border-radius: 20px;
                font-size: 0.75rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
            .badge-pending {{
                background: rgba(245, 158, 11, 0.15);
                color: #f59e0b;
                border: 1px solid rgba(245, 158, 11, 0.3);
            }}
            .badge-success {{
                background: rgba(16, 185, 129, 0.15);
                color: #10b981;
                border: 1px solid rgba(16, 185, 129, 0.3);
            }}
            .badge-executing {{
                background: rgba(59, 130, 246, 0.15);
                color: #3b82f6;
                border: 1px solid rgba(59, 130, 246, 0.3);
            }}
            .badge-failed, .badge-error {{
                background: rgba(239, 68, 68, 0.15);
                color: #ef4444;
                border: 1px solid rgba(239, 68, 68, 0.3);
            }}
            .badge-rejected {{
                background: rgba(148, 163, 184, 0.15);
                color: #94a3b8;
                border: 1px solid rgba(148, 163, 184, 0.3);
            }}
            .btn {{
                display: inline-flex;
                align-items: center;
                padding: 0.5rem 1rem;
                border-radius: 8px;
                font-size: 0.875rem;
                font-weight: 500;
                text-decoration: none;
                transition: all 0.2s;
                margin-right: 0.5rem;
            }}
            .btn-approve {{
                background: #10b981;
                color: #ffffff;
            }}
            .btn-approve:hover {{
                background: #059669;
            }}
            .btn-reject {{
                background: rgba(239, 68, 68, 0.2);
                color: #fca5a5;
                border: 1px solid rgba(239, 68, 68, 0.4);
            }}
            .btn-reject:hover {{
                background: rgba(239, 68, 68, 0.4);
            }}
            code {{
                background: rgba(255, 255, 255, 0.05);
                padding: 0.2rem 0.4rem;
                border-radius: 4px;
                font-family: monospace;
                font-size: 0.875rem;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>AI Self-Healing Monitor</h1>
                <p class="subtitle">Real-time Container Diagnosis & Remediation Log</p>
            </header>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-val">{total_actions}</div>
                    <div class="stat-label">Total Incidents</div>
                </div>
                <div class="stat-card">
                    <div class="stat-val" style="color: #f59e0b;">{pending_count}</div>
                    <div class="stat-label">Pending Approval</div>
                </div>
                <div class="stat-card">
                    <div class="stat-val" style="color: #10b981;">{success_count}</div>
                    <div class="stat-label">Recoveries Executed</div>
                </div>
                <div class="stat-card">
                    <div class="stat-val" style="color: #ef4444;">{failed_count}</div>
                    <div class="stat-label">Bypassed / Failed</div>
                </div>
            </div>
            
            <div class="incidents-card">
                <h2>Self-Healing Activity Logs</h2>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr>
                                <th>Container</th>
                                <th>Diagnosis</th>
                                <th>Proposed Fix</th>
                                <th>Status</th>
                                <th>Timestamp</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html_content

@app.get("/approve/{action_id}", response_class=HTMLResponse)
def approve_action(action_id: str, signature: str, expires: str, background_tasks: BackgroundTasks):
    if not verify_signed_url(action_id, signature, expires):
        return "<h3 style='color: #c62828;'>Forbidden: Invalid or expired link signature.</h3>"
        
    action = get_action(action_id)
    if not action:
        return "<h3>Action not found or already processed.</h3>"
        
    if action.status != "pending":
        return f"<h3>Action status is already: {action.status}</h3>"
        
    update_action_status(action_id, "executing")
    
    # Run fix command safely in background
    def execute():
        try:
            cmd = action.command.strip()
            
            # 1. Validate command format and container target
            if not validate_command(cmd):
                logger.warning(f"[SECURITY WARNING] Blocked execution of unsafe command: {cmd}")
                update_action_status(action_id, "failed")
                return
                
            # 2. Execute safely using the Python Docker SDK (No shell interpreter used)
            parts = cmd.split()
            docker_action = parts[1]
            target_container = parts[2]
            
            logger.info(f"Executing {docker_action} via Docker SDK for container: {target_container}")
            container = docker_client.containers.get(target_container)
            
            if docker_action == "restart":
                container.restart()
            elif docker_action == "start":
                container.start()
            elif docker_action == "stop":
                container.stop()
                
            update_action_status(action_id, "success")
            logger.info(f"Remediation succeeded via SDK for {action.container_name}")
            
        except Exception as e:
            update_action_status(action_id, "error")
            logger.error(f"Error executing fix: {e}")
            
    background_tasks.add_task(execute)
    
    return f"""
    <html>
        <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 100px;">
            <h2 style="color: #2e7d32;">Fix Approved!</h2>
            <p>Executing command: <code>{action.command}</code></p>
            <p>You will receive an update once the command finishes running.</p>
            <p><a href="/" style="color: #38bdf8; text-decoration: none;">&larr; Back to Dashboard</a></p>
        </body>
    </html>
    """

@app.get("/reject/{action_id}", response_class=HTMLResponse)
def reject_action(action_id: str, signature: str, expires: str):
    if not verify_signed_url(action_id, signature, expires):
        return "<h3 style='color: #c62828;'>Forbidden: Invalid or expired link signature.</h3>"
        
    action = get_action(action_id)
    if not action:
        return "<h3>Action not found.</h3>"
        
    update_action_status(action_id, "rejected")
    return f"""
    <html>
        <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 100px;">
            <h2 style="color: #c62828;">Fix Rejected</h2>
            <p>Remediation bypassed by user. No commands were executed.</p>
            <p><a href="/" style="color: #38bdf8; text-decoration: none;">&larr; Back to Dashboard</a></p>
        </body>
    </html>
    """

@app.on_event("startup")
def startup_event():
    # Start docker events monitoring thread
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
