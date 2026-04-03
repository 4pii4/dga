import discord
from discord.ext import commands
from discord import app_commands
import httpx
import os
import re
import html
import mimetypes
from urllib.parse import urlparse, unquote, parse_qs
from pathlib import Path
import asyncio
from PIL import Image
import json
import sys
import argparse

# ==========================================
# CONFIGURATION (Loaded from provided config)
# ==========================================
parser = argparse.ArgumentParser(description="Discord Archive Bot")
parser.add_argument("--config", required=True, help="Path to the configuration JSON file")
args = parser.parse_args()

CONFIG_FILE = args.config
BOT_TOKEN = ""
TARGET_CHANNEL_ID = 0

if not os.path.exists(CONFIG_FILE):
    print(f"Error: Configuration file '{CONFIG_FILE}' not found.")
    print("Please create the file with the following JSON format:")
    print("{")
    print('    "bot_token": "YOUR_BOT_TOKEN_HERE",')
    print('    "target_channel_id": 123456789012345678')
    print("}")
    sys.exit(1)

try:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
        BOT_TOKEN = config.get("bot_token", "").strip()
        TARGET_CHANNEL_ID = int(config.get("target_channel_id", 0))
except Exception as e:
    print(f"Error loading {CONFIG_FILE}: {e}")
    sys.exit(1)

if not BOT_TOKEN or not TARGET_CHANNEL_ID:
    print(f"Error: Configuration missing or incomplete in '{CONFIG_FILE}'.")
    print("Ensure both 'bot_token' and 'target_channel_id' are provided.")
    sys.exit(1)
# ==========================================

# Temporary folder for processing downloads
TEMP_DIR = Path("temp_archive")
TEMP_DIR.mkdir(exist_ok=True)

MEDIA_TYPES = {
    'image/gif': '.gif', 'video/mp4': '.mp4', 'image/png': '.png',
    'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'video/webm': '.webm',
    'image/webp': '.webp', 'video/quicktime': '.mov',
}

class URLResolver:
    """Stripped down resolver for Tenor/Giphy links"""
    @staticmethod
    def extract_domain(url: str) -> str:
        try: return urlparse(url).netloc
        except: return "unknown"

    @staticmethod
    async def resolve_url(url: str) -> str:
        if hasattr(html, 'unescape'):
            url = html.unescape(url)
            
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if 'tenor.com' in domain and '/view/' in url:
            try:
                async with httpx.AsyncClient() as client:
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    resp = await client.get(url, headers=headers, follow_redirects=True)
                    match_gif = re.search(r'content="(https://[^"]+\.tenor\.com/[^"]+\.gif)"', resp.text)
                    if match_gif: return match_gif.group(1)
                    match_mp4 = re.search(r'content="(https://[^"]+\.tenor\.com/[^"]+\.mp4)"', resp.text)
                    if match_mp4: return match_mp4.group(1)
            except Exception as e:
                print(f"Tenor resolve error: {e}")
                
        elif 'giphy.com' in domain and '/gifs/' in url:
            clean_path = parsed.path.strip('/')
            giphy_id = clean_path.split('/')[-1].split('-')[-1]
            return f"https://i.giphy.com/{giphy_id}.gif"
            
        elif 'images-ext-' in domain:
            path_parts = parsed.path.split('/')
            for i, part in enumerate(path_parts):
                if part == 'external' and i + 1 < len(path_parts):
                    encoded_url = '/'.join(path_parts[i+1:]).split('?')[0]
                    try:
                        decoded = unquote(encoded_url)
                        if decoded.startswith('http'): return decoded
                    except: pass

        return url

class ArchiverBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        # Sync the slash commands globally on startup
        await self.tree.sync()
        print("✅ Slash commands synced!")

bot = ArchiverBot()

@bot.event
async def on_ready():
    print(f"=======================================")
    print(f"Archiver Bot Online! Logged in as {bot.user}")
    print(f"=======================================")

@bot.tree.command(name="archive", description="Download, convert, and archive a GIF or image to your private server.")
@app_commands.describe(link="The Tenor, Giphy, or Discord CDN link to archive", image="Upload an image to convert and archive")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def archive_command(interaction: discord.Interaction, link: str = None, image: discord.Attachment = None):
    # Instantly defer ephemerally so Discord knows we are processing (prevents timeout)
    await interaction.response.defer(ephemeral=True)

    if bool(link) == bool(image):
        await interaction.followup.send("❌ Please provide exactly **one** option: either a `link` or an `image`.")
        return

    try:
        if link:
            # 1. Resolve URL
            resolved_url = await URLResolver.resolve_url(link)
            if not resolved_url:
                await interaction.followup.send("❌ Invalid URL provided.")
                return

            # 2a. Download File from Link
            async with httpx.AsyncClient() as client:
                resp = await client.get(resolved_url, timeout=30.0, follow_redirects=True)
                if resp.status_code != 200:
                    await interaction.followup.send(f"❌ Failed to download file. HTTP {resp.status_code}")
                    return
                
                content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
                ext = MEDIA_TYPES.get(content_type, '.bin')
                if ext == '.bin':
                    ext = '.' + resolved_url.split('?')[0].split('.')[-1]
                
                temp_file = TEMP_DIR / f"temp_{interaction.id}{ext}"
                temp_file.write_bytes(resp.content)
        else:
            # 2b. Download File from Attachment
            ext = f".{image.filename.split('.')[-1].lower()}" if '.' in image.filename else '.bin'
            temp_file = TEMP_DIR / f"temp_{interaction.id}{ext}"
            await image.save(temp_file)

        # 3. Pillow Conversion (Convert static images to GIF)
        final_file = temp_file
        if temp_file.suffix.lower() in ['.png', '.jpg', '.jpeg', '.webp']:
            try:
                with Image.open(temp_file) as img:
                    gif_path = temp_file.with_suffix('.gif')
                    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                        img = img.convert("RGBA")
                    img.save(gif_path, 'GIF', save_all=getattr(img, "is_animated", False))
                temp_file.unlink()
                final_file = gif_path
            except Exception as e:
                print(f"Pillow error: {e}")

        # 4. Fetch target channel and check server limits
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            channel = await bot.fetch_channel(TARGET_CHANNEL_ID)

        upload_limit = channel.guild.filesize_limit if hasattr(channel, 'guild') else 25 * 1024 * 1024
        file_size = os.path.getsize(final_file)

        if file_size > upload_limit:
            await interaction.followup.send(
                f"❌ **File Too Large:** The downloaded GIF ({file_size / (1024 * 1024):.1f} MB) "
                f"exceeds the archiving server's maximum upload limit ({upload_limit / (1024 * 1024):.1f} MB)."
            )
            final_file.unlink()
            return

        # 5. Upload to Target Channel
        try:
            with open(final_file, 'rb') as f:
                discord_file = discord.File(f, filename=final_file.name)
                # Tag the message with the original link or image name so you know where it came from
                source_text = f"<{link}>" if link else f"uploaded image (`{image.filename}`)"
                msg = await channel.send(content=f"Archived from: {source_text}", file=discord_file)
                
            await interaction.followup.send(f"✅ **Saved successfully!**\n[Click here to jump to the GIF]({msg.jump_url})")

        except discord.errors.HTTPException as e:
            if e.status == 413 or e.code == 40005:
                await interaction.followup.send(
                    f"❌ **Upload Failed:** Discord rejected the file (Payload Too Large). "
                    f"Size: {file_size / (1024*1024):.1f} MB."
                )
            else:
                await interaction.followup.send(f"❌ **Discord API Error:** `{e}`")
                
        finally:
            if final_file.exists():
                final_file.unlink()

    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: `{str(e)}`")
        source_log = link if link else image.filename
        print(f"Error archiving {source_log}: {e}")

if __name__ == "__main__":
    bot.run(BOT_TOKEN)