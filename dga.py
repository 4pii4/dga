import discord
from discord.ext import commands
from discord import app_commands
import os
import re
import html
import mimetypes
from urllib.parse import urlparse, unquote, parse_qs
from pathlib import Path
import asyncio
import json
import sys
import getpass
import argparse
import stealth_requests
import shutil
import math

# ==========================================
# DEPENDENCY CHECKS (Fails fast if missing)
# ==========================================
try:
    import ffmpeg
    from wand.image import Image as WandImage
except ImportError as e:
    print(f"CRITICAL ERROR: Required python package missing. ({e})")
    print("Please install them using: pip install ffmpeg-python Wand")
    sys.exit(1)

if not shutil.which("ffmpeg"):
    print("CRITICAL ERROR: 'ffmpeg' command not found in your system PATH.")
    sys.exit(1)

if not shutil.which("magick"):
    print("CRITICAL ERROR: 'magick' (ImageMagick) command not found in your system PATH.")
    sys.exit(1)

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
    'image/avif': '.avif', 'image/apng': '.apng'
}

def get_magic_type(file_path: Path) -> str:
    """Reads file signatures (magic bytes) to strictly identify the file format."""
    try:
        with open(file_path, 'rb') as f:
            header = f.read(32)
    except Exception:
        return 'unknown'

    if header.startswith(b'GIF8'):
        return 'gif'
    if header.startswith(b'\x1aE\xdf\xa3'):
        return 'webm'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if header.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'webp'

    # Check ISO Base Media File Formats (MP4, MOV, AVIF)
    if len(header) >= 12 and header[4:8] == b'ftyp':
        brand = header[8:12]
        # Some AVIFs bury their brand under compatible brands, so we scan the chunk
        if brand in (b'avif', b'avis') or b'avif' in header[16:32] or b'avis' in header[16:32]:
            return 'avif'
        elif brand in (b'qt  ',):
            return 'mov'
        else:
            return 'mp4'

    return 'unknown'

def compress_gif(input_path: Path, target_size: int) -> Path:
    """Compresses GIF by dynamically reducing resolution to fit the target size, preserving maximum quality."""
    output_path = input_path.with_name(f"compressed_{input_path.name}")
    try:
        import ffmpeg
        
        current_size = os.path.getsize(input_path)
        if current_size <= target_size:
            return input_path
            
        # Calculate how much we need to shrink. 
        # File size scales roughly with the number of pixels (area).
        # We multiply by 0.85 for a safety margin to ensure it falls under the limit.
        ratio = target_size / current_size
        scale_factor = math.sqrt(ratio) * 0.85 
        scale_factor = max(0.1, min(1.0, scale_factor))
        
        stream = ffmpeg.input(str(input_path))
        # Scale proportionally based on the calculated factor
        v = stream.video.filter('scale', f'trunc(iw*{scale_factor})', '-1')
        split = v.split()
        # Use 256 colors and default high-quality dithering (instead of aggressive bayer)
        palette = split[0].filter('palettegen', max_colors=256)
        out = ffmpeg.filter([split[1], palette], 'paletteuse')
        
        (
            ffmpeg
            .output(out, str(output_path), loglevel="error")
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
        if output_path.exists():
            return output_path
    except Exception as e:
        print(f"Compression failed: {e}")
        
    return input_path

def convert_to_gif(input_path: Path) -> Path:
    """Uses ffmpeg-python or Wand (ImageMagick) to convert media to GIF format."""
    output_path = input_path.with_suffix('.gif')
    
    # If already a GIF, no conversion needed
    if input_path.suffix.lower() == '.gif':
        return input_path
        
    errors = []
    is_video = input_path.suffix.lower() in ['.mp4', '.webm', '.mov']
    
    def try_ffmpeg():
        try:
            (
                ffmpeg
                .input(str(input_path))
                .output(str(output_path), loglevel="error")
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )
            if output_path.exists():
                return True
        except ffmpeg.Error as e:
            stderr_text = e.stderr.decode('utf-8', errors='ignore').strip() if e.stderr else "Unknown error"
            errors.append(f"FFmpeg failed: {stderr_text}")
        except Exception as e:
            errors.append(f"FFmpeg exception: {e}")
        return False
        
    def try_wand():
        try:
            with WandImage(filename=str(input_path)) as img:
                img.format = 'gif'
                img.save(filename=str(output_path))
            if output_path.exists():
                return True
        except Exception as e:
            errors.append(f"Magick failed: {e}")
        return False

    # FFmpeg is superior/faster for raw video formats.
    # ImageMagick correctly processes frames in animated image sequences (AVIF, WebP, APNG)
    # Priority is determined here so FFmpeg doesn't falsely succeed by ripping 1 frame out of a mislabeled AVIF.
    if is_video:
        if try_ffmpeg(): return output_path
        if try_wand(): return output_path
    else:
        if try_wand(): return output_path
        if try_ffmpeg(): return output_path
        
    raise RuntimeError(" | ".join(errors))

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
                def _fetch_tenor():
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    return stealth_requests.get(url, headers=headers).text
                    
                html_text = await asyncio.to_thread(_fetch_tenor)
                match_gif = re.search(r'content="(https://[^"]+\.tenor\.com/[^"]+\.gif)"', html_text)
                if match_gif: return match_gif.group(1)
                match_mp4 = re.search(r'content="(https://[^"]+\.tenor\.com/[^"]+\.mp4)"', html_text)
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
    # Instantly defer ephemerally so Discord knows we are processing
    await interaction.response.defer(ephemeral=True)

    if bool(link) == bool(image):
        await interaction.followup.send("❌ Please provide exactly **one** option: either a `link` or an `image`.")
        return

    # Default Variables for Safe Cleanup
    temp_file = None
    final_file = None

    try:
        # Fetch target channel & Calculate Guild Limit (Default to unboosted 25MB limit)
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            channel = await bot.fetch_channel(TARGET_CHANNEL_ID)
            
        upload_limit = channel.guild.filesize_limit if hasattr(channel, 'guild') else 25 * 1024 * 1024

        if link:
            # 1. Resolve URL
            resolved_url = await URLResolver.resolve_url(link)
            if not resolved_url:
                await interaction.followup.send("❌ Invalid URL provided.")
                return

            # 2a. Download File from Link using Stealth Requests
            def _download_file():
                return stealth_requests.get(resolved_url, timeout=30.0)
                
            resp = await asyncio.to_thread(_download_file)
            if resp.status_code != 200:
                await interaction.followup.send(f"❌ Failed to download file. HTTP {resp.status_code}")
                return
            
            # Immediately reject if the downloaded blob exceeds 150% of the limit
            if len(resp.content) > upload_limit * 1.5:
                await interaction.followup.send(
                    f"❌ **Download Rejected:** The source file ({len(resp.content) / (1024 * 1024):.1f} MB) "
                    f"is too large. It exceeds 150% of the server's upload limit ({upload_limit / (1024 * 1024):.1f} MB)."
                )
                return

            content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
            ext = MEDIA_TYPES.get(content_type, '.bin')
            if ext == '.bin':
                ext = '.' + resolved_url.split('?')[0].split('.')[-1]
            
            temp_file = TEMP_DIR / f"temp_{interaction.id}{ext}"
            temp_file.write_bytes(resp.content)
            
        else:
            # 2b. Check size limits immediately on uploaded attachment
            if image.size > upload_limit * 1.5:
                await interaction.followup.send(
                    f"❌ **Upload Rejected:** The provided image ({image.size / (1024 * 1024):.1f} MB) "
                    f"is too large. It exceeds 150% of the server's upload limit ({upload_limit / (1024 * 1024):.1f} MB)."
                )
                return
                
            ext = f".{image.filename.split('.')[-1].lower()}" if '.' in image.filename else '.bin'
            temp_file = TEMP_DIR / f"temp_{interaction.id}{ext}"
            await image.save(temp_file)

        # --- Magic Bytes Detection & Extension Fix ---
        # This prevents mislabeled files (e.g. animated AVIFs saved as .jpg or .mp4) 
        # from tricking the conversion priority engine.
        magic_type = get_magic_type(temp_file)
        if magic_type != 'unknown' and temp_file.suffix.lower() != f'.{magic_type}':
            proper_temp_file = temp_file.with_name(f"temp_{interaction.id}.{magic_type}")
            shutil.move(str(temp_file), str(proper_temp_file))
            temp_file = proper_temp_file

        # 3. FFmpeg / ImageMagick Conversion
        try:
            final_file = await asyncio.to_thread(convert_to_gif, temp_file)
        except Exception as e:
            await interaction.followup.send(f"❌ **Conversion Error:** `{e}`")
            return

        # Double check size after conversion, and apply compression if needed
        file_size = os.path.getsize(final_file)
        if file_size > upload_limit:
            if file_size <= upload_limit * 1.5:
                try:
                    compressed_file = await asyncio.to_thread(compress_gif, final_file, upload_limit)
                    
                    if compressed_file != final_file:
                        # Ensure we clean up the uncompressed output file so it isn't orphaned
                        if final_file != temp_file and final_file.exists():
                            final_file.unlink()
                        final_file = compressed_file
                        
                    file_size = os.path.getsize(final_file)
                except Exception as e:
                    print(f"Compression error: {e}")

            # Re-check against the upload limit in case compression wasn't enough
            if file_size > upload_limit:
                await interaction.followup.send(
                    f"❌ **Converted File Too Large:** Even after optimization, the GIF ({file_size / (1024 * 1024):.1f} MB) "
                    f"exceeds the server's {upload_limit / (1024 * 1024):.1f} MB limit."
                )
                return

        # 4. Upload to Target Channel
        try:
            with open(final_file, 'rb') as f:
                discord_file = discord.File(f, filename=final_file.name)
                # Tag the message with the original link or image name
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

    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: `{str(e)}`")
        source_log = link if link else image.filename
        print(f"Error archiving {source_log}: {e}")
        
    finally:
        # 5. Guaranteed local cleanup 
        if temp_file and temp_file.exists():
            temp_file.unlink()
        if final_file and final_file.exists() and final_file != temp_file:
            final_file.unlink()

if __name__ == "__main__":
    bot.run(BOT_TOKEN)