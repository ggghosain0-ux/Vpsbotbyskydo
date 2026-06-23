import random
import logging
import subprocess
import sys
import os
import re
import time
import discord
from discord.ext import commands, tasks
import docker
import asyncio
from discord import app_commands
import sqlite3
from dotenv import load_dotenv
from datetime import datetime, timezone

# Load environment variables
load_dotenv()

# Configuration from .env
TOKEN = os.getenv('TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'UnixNodes')
WATERMARK = os.getenv('WATERMARK', 'Powered by UnixNodes VPS Bot')

# VPS Resource Defaults
DEFAULT_RAM = os.getenv('DEFAULT_RAM', '2g')
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '10G')
VPS_HOSTNAME = os.getenv('VPS_HOSTNAME', 'unix-free')
SERVER_LIMIT = int(os.getenv('SERVER_LIMIT', 1))
TOTAL_SERVER_LIMIT = int(os.getenv('TOTAL_SERVER_LIMIT', 50))
DATABASE_FILE = os.getenv('DATABASE_FILE', 'vps_bot.db')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vps_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Intents setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)
client = docker.from_env()

def is_admin(member):
    if not isinstance(member, discord.Member):
        logger.warning("is_admin called with non-Member object")
        return False
    return member.id == ADMIN_ID

# Database initialization
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    sql = f'''
        CREATE TABLE IF NOT EXISTS vps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            container_id TEXT UNIQUE NOT NULL,
            container_name TEXT NOT NULL,
            os_type TEXT NOT NULL,
            hostname TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            ssh_command TEXT,
            ram TEXT DEFAULT '{DEFAULT_RAM}',
            cpu TEXT DEFAULT '{DEFAULT_CPU}',
            disk TEXT DEFAULT '{DEFAULT_DISK}',
            suspended INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    '''
    cursor.execute(sql)
    
    # Migrations check
    cursor.execute("PRAGMA table_info(vps)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'suspended' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN suspended INTEGER DEFAULT 0")
        
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# Database Helper Actions
def add_user(user_id, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def add_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO bans (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM bans WHERE user_id = ?', (user_id,))
    banned = cursor.fetchone() is not None
    conn.close()
    return banned

def add_vps(user_id, container_id, container_name, os_type, hostname, ssh_command, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, status, ssh_command, ram, cpu, disk, suspended)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 0)
    ''', (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk))
    conn.commit()
    conn.close()

def get_user_vps(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    vps_list = cursor.fetchall()
    conn.close()
    return vps_list

def count_user_vps(user_id):
    return len(get_user_vps(user_id))

def get_vps_by_identifier(user_id, identifier):
    vps_list = get_user_vps(user_id)
    if not identifier:
        return vps_list[0] if vps_list else None
    identifier_lower = identifier.lower()
    for vps in vps_list:
        if identifier_lower in vps['container_id'].lower() or identifier_lower in vps['container_name'].lower():
            return vps
    return None

def update_vps_status(container_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET status = ? WHERE container_id = ?', (status, container_id))
    conn.commit()
    conn.close()

def update_vps_ssh(container_id, ssh_command):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET ssh_command = ? WHERE container_id = ?', (ssh_command, container_id))
    conn.commit()
    conn.close()

def update_vps_suspended(container_id, suspended):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET suspended = ? WHERE container_id = ?', (suspended, container_id))
    conn.commit()
    conn.close()

def delete_vps(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM vps WHERE container_id = ?', (container_id,))
    conn.commit()
    conn.close()

def get_total_instances():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status = "running"')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def parse_gb(resource_str):
    match = re.match(r'(\d+(?:\.\d+)?)([mMgG])?', resource_str.lower())
    if match:
        num = float(match.group(1))
        unit = match.group(2) or 'g'
        if unit in ['g', '']:
            return num
        elif unit in ['m']:
            return num / 1024.0
    return 0.0

# System Stats Utilities
def get_uptime(container_id):
    try:
        output = subprocess.check_output(["docker", "inspect", "-f", "{{.State.StartedAt}}", container_id], stderr=subprocess.STDOUT).decode().strip()
        if output == "<no value>":
            return "Not running"
        start_time = datetime.fromisoformat(output.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        uptime = now - start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
    except Exception as e:
        logger.error(f"Uptime calculation failure on {container_id}: {e}")
        return "Unknown"

def get_stats(container_id):
    try:
        output = subprocess.check_output([
            "docker", "stats", "--no-stream", "--format",
            "{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}", container_id
        ], stderr=subprocess.STDOUT).decode().strip()
        parts = output.split('\t')
        if len(parts) == 3:
            return {'cpu': parts[0], 'mem': parts[1], 'net': parts[2]}
    except Exception as e:
        logger.error(f"Stats lookup fail for {container_id}: {e}")
    return {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}

def get_logs(container_id, lines=50):
    try:
        output = subprocess.check_output(["docker", "logs", "--tail", str(lines), container_id], stderr=subprocess.STDOUT).decode()
        return output[-1900:]
    except Exception as e:
        logger.error(f"Logs pull error on {container_id}: {e}")
        return "Failed to fetch stdout history."

# Async Non-blocking Docker Execution Tasks
async def async_docker_run(image, hostname, ram, cpu, disk, container_name):
    cmd = [
        "docker", "run", "-d",
        "--privileged", "--cap-add=ALL",
        "--restart", "unless-stopped",
        f"--memory={ram}",
        f"--cpus={cpu}",
        f"--hostname={hostname}",
        f"--name={container_name}",
        image, "tail", "-f", "/dev/null"
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode != 0:
            logger.error(f"Docker run process failed: {stderr.decode()}")
            return None
        return stdout.decode().strip()
    except Exception as e:
        logger.error(f"Docker execution runtime error: {e}")
        return None

async def async_docker_start(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "start", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except Exception:
        return False

async def async_docker_stop(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "stop", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        try:
            await asyncio.create_subprocess_exec("docker", "kill", container_id, stdout=discord.utils.DEVNULL).communicate()
        except Exception: pass
        return False
    except Exception:
        return False

async def async_docker_restart(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "restart", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except Exception:
        return False

async def async_docker_rm(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "rm", "-f", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        return proc.returncode == 0
    except Exception:
        return False

async def async_install_tmate(container_id, os_type):
    install_cmd = "apt-get update && apt-get install -y tmate curl wget sudo openssh-client"
    try:
        proc = await asyncio.create_subprocess_exec("docker", "exec", container_id, "bash", "-c", install_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except Exception as e:
        logger.error(f"Failed to environment bootstrap tmate inside {container_id}: {e}")

async def capture_ssh_session_line(process):
    while True:
        try:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            if not output: break
            line = output.decode('utf-8').strip()
            if "ssh session:" in line.lower():
                return line.split("ssh session:")[-1].strip()
        except asyncio.TimeoutError:
            break
    return None

async def docker_exec_tmate(container_id):
    try:
        return await asyncio.create_subprocess_exec("docker", "exec", container_id, "tmate", "-F", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception:
        return None

# Combined Business Logic Wrappers
async def regen_ssh_command(interaction: discord.Interaction, vps_identifier, send_response=True, target_user=None):
    target_user = target_user or interaction.user
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps or vps['status'] != "running":
        if send_response: await interaction.response.send_message("VPS instances must be active to trigger access endpoints.", ephemeral=True)
        return False
    
    if send_response: await interaction.response.defer(ephemeral=True)
    container_id = vps['container_id']
    exec_process = await docker_exec_tmate(container_id)
    
    if exec_process:
        ssh_line = await capture_ssh_session_line(exec_process)
        if ssh_line:
            update_vps_ssh(container_id, ssh_line)
            embed = discord.Embed(title="New Secure SSH String Generated", description=f"```{ssh_line}```", color=discord.Color.green())
            embed.set_footer(text=WATERMARK)
            try:
                await target_user.send(embed=embed)
            except discord.Forbidden:
                if send_response: await interaction.followup.send("Failed to send SSH token via direct message. Please check server privacy configuration.", ephemeral=True)
                return True
            if send_response: await interaction.followup.send("Access tokens dispatched safely to your DMs.", ephemeral=True)
            return True
    if send_response: await interaction.followup.send("Process error registering proxy shell runtime endpoint.", ephemeral=True)
    return False

async def manage_vps(interaction: discord.Interaction, vps_identifier, action, target_user=None):
    target_user = target_user or interaction.user
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        await interaction.followup.send("Target host entry mapping missing.", ephemeral=True)
        return
    if action == "start" and vps['suspended'] and target_user == interaction.user:
        await interaction.followup.send("Container execution prohibited. Resource profile suspended by system administration.", ephemeral=True)
        return
        
    container_id = vps['container_id']
    success = False
    
    if action == "start":
        success = await async_docker_start(container_id)
        if success: update_vps_status(container_id, "running")
    elif action == "stop":
        success = await async_docker_stop(container_id)
        if success: update_vps_status(container_id, "stopped")
    elif action == "restart":
        success = await async_docker_restart(container_id)
        if success: update_vps_status(container_id, "running")
        
    if success:
        embed = discord.Embed(title=f"Host Action: {action.title()} Completed", color=discord.Color.green())
        if action in ["start", "restart"]:
            await regen_ssh_command(interaction, vps_identifier, send_response=False, target_user=target_user)
            embed.description = "Access context generated and dispatched through private DMs."
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(f"Engine command failure attempting configuration option: {action}", ephemeral=True)

async def reinstall_vps(interaction: discord.Interaction, vps_identifier, os_type, target_user=None):
    target_user = target_user or interaction.user
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        await interaction.followup.send("VPS target reference mapping was not located.", ephemeral=True)
        return
        
    container_id = vps['container_id']
    user_id = vps['user_id']
    hostname = vps['hostname']
    ram, cpu, disk = vps['ram'], vps['cpu'], vps['disk']
    
    await async_docker_stop(container_id)
    await asyncio.sleep(2)
    await async_docker_rm(container_id)
    delete_vps(container_id)
    
    suffix = random.randint(1000, 9999)
    new_container_name = f"{os_type}-vps-{user_id}-{suffix}"
    image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
    
    new_container_id = await async_docker_run(image, hostname, ram, cpu, disk, new_container_name)
    if new_container_id:
        await async_install_tmate(new_container_id, os_type)
        await asyncio.sleep(10)
        exec_process = await docker_exec_tmate(new_container_id)
        ssh_line = await capture_ssh_session_line(exec_process)
        if ssh_line:
            add_vps(user_id, new_container_id, new_container_name, os_type, hostname, ssh_line, ram, cpu, disk)
            await interaction.followup.send("OS Blueprint reinstalled successfully. Access profiles synchronized.", ephemeral=True)
            return
    await interaction.followup.send("Reinstall failure during Docker recovery pipeline.", ephemeral=True)

async def create_vps(interaction: discord.Interaction, os_type, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK, target_user=None):
    target_user = target_user or interaction.user
    user_id = target_user.id
    add_user(user_id, str(target_user))
    
    if is_banned(user_id):
        await interaction.response.send_message("System access profile prohibited from instantiation routines.", ephemeral=True)
        return
    if count_user_vps(user_id) >= SERVER_LIMIT:
        await interaction.response.send_message("Allotted instance count limit per context reached.", ephemeral=True)
        return
    if get_total_instances() >= TOTAL_SERVER_LIMIT:
        await interaction.response.send_message("Global hardware capability allocations maxed out. Contact host administrators.", ephemeral=True)
        return
        
    try:
        host_info = client.info()
        if float(cpu) > host_info['NCPU'] or parse_gb(ram) > (host_info['MemTotal'] / (1024 ** 3)):
            await interaction.response.send_message("Requested performance layout footprint rejected by platform hardware capacity safely.", ephemeral=True)
            return
    except Exception:
        await interaction.response.send_message("Resource scheduling baseline validation lookup fault.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    hostname = f"{VPS_HOSTNAME}-{user_id}"
    suffix = random.randint(1000, 9999)
    container_name = f"{os_type}-vps-{user_id}-{suffix}"
    image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
    
    container_id = await async_docker_run(image, hostname, ram, cpu, disk, container_name)
    if not container_id:
        await interaction.followup.send("Docker baseline architecture mapping subsystem fault.", ephemeral=True)
        return
        
    await asyncio.sleep(5)
    await async_install_tmate(container_id, os_type)
    await asyncio.sleep(10)
    
    exec_process = await docker_exec_tmate(container_id)
    ssh_line = await capture_ssh_session_line(exec_process)
    if ssh_line:
        add_vps(user_id, container_id, container_name, os_type, hostname, ssh_line, ram, cpu, disk)
        embed = discord.Embed(title="Virtual Environment Provisioned Successfully", color=discord.Color.green())
        embed.add_field(name="Deployment Blueprint", value=f"OS: {os_type.upper()} | Cores: {cpu} | Memory: {ram}", inline=False)
        embed.add_field(name="Initial Shell Token", value=f"```{ssh_line}```", inline=False)
        embed.set_footer(text=WATERMARK)
        try: await target_user.send(embed=embed)
        except Exception: pass
        await interaction.followup.send("Your VPS instance has initialized. Connection credentials sent to private DMs.", ephemeral=True)
    else:
        await interaction.followup.send("Failed to catch backend stream console handle. Destroying footprint context elements.", ephemeral=True)
        await async_d
