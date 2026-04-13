import discord
from discord.ext import commands
from discord import app_commands
import os
import re
import html
import asyncio
import json
import sys
import argparse
import stealth_requests
import shutil
import math
from urllib.parse import urlparse, unquote
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple

try:
    import ffmpeg
    from wand.image import Image as WandImage
except ImportError as e:
    print(f"CRITICAL ERROR: Required python package missing. ({e})")
    print("Please install them using: pip install ffmpeg-python Wand")
    sys.exit(1)


@dataclass
class AppConfig:
    bot_token: str
    target_channel_id: int

    @classmethod
    def load_from_file(cls, path: str) -> 'AppConfig':
        if not os.path.exists(path):
            print(f"Error: Configuration file '{path}' not found.")
            print("Please create the file with the following JSON format:\n{\n    \"bot_token\": \"YOUR_BOT_TOKEN_HERE\",\n    \"target_channel_id\": 123456789012345678\n}")
            sys.exit(1)

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                bot_token = data.get("bot_token", "").strip()
                target_channel_id = int(data.get("target_channel_id", 0))

                if not bot_token or not target_channel_id:
                    raise ValueError("Missing 'bot_token' or 'target_channel_id'")

                return cls(bot_token=bot_token, target_channel_id=target_channel_id)

        except Exception as e:
            print(f"Error loading {path}: {e}")
            sys.exit(1)


class DependencyValidator:
    @staticmethod
    def verify_system_requirements():
        if not shutil.which("ffmpeg"):
            print("CRITICAL ERROR: 'ffmpeg' command not found in your system PATH.")
            sys.exit(1)

        if not shutil.which("magick"):
            print("CRITICAL ERROR: 'magick' (ImageMagick) command not found in your system PATH.")
            sys.exit(1)


class MediaProcessor:
    MEDIA_TYPES = {
        'image/gif': '.gif', 'video/mp4': '.mp4', 'image/png': '.png',
        'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'video/webm': '.webm',
        'image/webp': '.webp', 'video/quicktime': '.mov',
        'image/avif': '.avif', 'image/apng': '.apng'
    }

    @staticmethod
    def get_magic_type(file_path: Path) -> str:
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

        if len(header) >= 12 and header[4:8] == b'ftyp':
            brand = header[8:12]
            if brand in (b'avif', b'avis') or b'avif' in header[16:32] or b'avis' in header[16:32]:
                return 'avif'
            elif brand in (b'qt  ',):
                return 'mov'
            else:
                return 'mp4'

        return 'unknown'

    @staticmethod
    def compress_gif(input_path: Path, target_size: int) -> Path:
        output_path = input_path.with_name(f"compressed_{input_path.name}")
        try:
            current_size = os.path.getsize(input_path)
            if current_size <= target_size:
                return input_path

            ratio = target_size / current_size

            scale_factor = math.sqrt(ratio) * 0.75
            scale_factor = max(0.1, min(1.0, scale_factor))

            colors = 256 if ratio > 0.4 else 128 if ratio > 0.2 else 64

            stream = ffmpeg.input(str(input_path))
            v = stream.video.filter('fps', fps=15).filter('scale', w=f'trunc(iw*{scale_factor})', h='-1')
            split = v.split()

            palette = split[0].filter('palettegen', max_colors=colors)
            out = ffmpeg.filter([split[1], palette], 'paletteuse', dither='bayer', bayer_scale=5)

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

    @staticmethod
    def convert_to_gif(input_path: Path) -> Path:
        output_path = input_path.with_suffix('.gif')

        if input_path.suffix.lower() == '.gif':
            return input_path

        errors = []
        is_video = input_path.suffix.lower() in ['.mp4', '.webm', '.mov']

        def try_ffmpeg():
            try:
                if is_video:
                    stream = ffmpeg.input(str(input_path))
                    v = stream.video.filter('fps', fps=24).filter('scale', w='min(iw,800)', h='-1')
                    split = v.split()
                    palette = split[0].filter('palettegen')
                    out = ffmpeg.filter([split[1], palette], 'paletteuse')
                    (
                        ffmpeg
                        .output(out, str(output_path), loglevel="error")
                        .overwrite_output()
                        .run(capture_stdout=True, capture_stderr=True)
                    )
                else:
                    (
                        ffmpeg
                        .input(str(input_path))
                        .output(str(output_path), loglevel="error")
                        .overwrite_output()
                        .run(capture_stdout=True, capture_stderr=True)
                    )
                return output_path.exists()
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
                return output_path.exists()
            except Exception as e:
                errors.append(f"Magick failed: {e}")
            return False

        if is_video:
            if try_ffmpeg(): return output_path
            if try_wand(): return output_path
        else:
            if try_wand(): return output_path
            if try_ffmpeg(): return output_path

        raise RuntimeError(" | ".join(errors))


class URLResolver:
    @staticmethod
    async def resolve(url: str) -> str:
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
    def __init__(self, config: AppConfig):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.app_config = config
        self.temp_dir = Path("temp_archive")
        self.temp_dir.mkdir(exist_ok=True)

    async def setup_hook(self):
        await self.tree.sync()
        print("✅ Slash commands synced!")

    async def on_ready(self):
        print("=======================================")
        print(f"Archiver Bot Online! Logged in as {self.user}")
        print("=======================================")

    async def fetch_target_channel(self) -> discord.abc.Messageable:
        channel = self.get_channel(self.app_config.target_channel_id)
        if not channel:
            channel = await self.fetch_channel(self.app_config.target_channel_id)
        return channel

    async def download_from_link(self, link: str, interaction_id: int, max_size: int) -> Tuple[Path, str]:
        resolved_url = await URLResolver.resolve(link)
        if not resolved_url:
            raise ValueError("Invalid URL provided.")

        def _download_file():
            return stealth_requests.get(resolved_url, timeout=30.0)

        resp = await asyncio.to_thread(_download_file)
        if resp.status_code != 200:
            raise ValueError(f"Failed to download file. HTTP {resp.status_code}")

        if len(resp.content) > max_size * 1.5:
            raise ValueError(
                f"**Download Rejected:** The source file ({len(resp.content) / (1024 * 1024):.1f} MB) "
                f"is too large. It exceeds 150% of the server's upload limit ({max_size / (1024 * 1024):.1f} MB)."
            )

        content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
        ext = MediaProcessor.MEDIA_TYPES.get(content_type, '.bin')
        if ext == '.bin':
            ext = '.' + resolved_url.split('?')[0].split('.')[-1]

        temp_file = self.temp_dir / f"temp_{interaction_id}{ext}"
        temp_file.write_bytes(resp.content)
        return temp_file, ext

    async def save_attachment(self, attachment: discord.Attachment, interaction_id: int, max_size: int) -> Path:
        if attachment.size > max_size * 1.5:
            raise ValueError(
                f"**Upload Rejected:** The provided image ({attachment.size / (1024 * 1024):.1f} MB) "
                f"is too large. It exceeds 150% of the server's upload limit ({max_size / (1024 * 1024):.1f} MB)."
            )

        ext = f".{attachment.filename.split('.')[-1].lower()}" if '.' in attachment.filename else '.bin'
        temp_file = self.temp_dir / f"temp_{interaction_id}{ext}"
        await attachment.save(temp_file)
        return temp_file


def initialize_app():
    parser = argparse.ArgumentParser(description="Discord Archive Bot")
    parser.add_argument("--config", required=True, help="Path to the configuration JSON file")
    args = parser.parse_args()

    DependencyValidator.verify_system_requirements()
    config = AppConfig.load_from_file(args.config)

    bot = ArchiverBot(config)
    
    @bot.tree.command(name="archive", description="Download, convert, and archive a GIF or image to your private server.")
    @app_commands.describe(link="The Tenor, Giphy, or Discord CDN link to archive", image="Upload an image to convert and archive")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def archive_command(interaction: discord.Interaction, link: str = None, image: discord.Attachment = None):
        await interaction.response.defer(ephemeral=True)

        async def safe_reply(content: str):
            try:
                if not interaction.is_expired():
                    await interaction.followup.send(content)
                else:
                    await interaction.user.send(content)
            except discord.errors.NotFound:
                try:
                    await interaction.user.send(content)
                except discord.errors.Forbidden:
                    pass
            except Exception:
                pass

        if bool(link) == bool(image):
            await safe_reply("❌ Please provide exactly **one** option: either a `link` or an `image`.")
            return

        temp_file: Optional[Path] = None
        final_file: Optional[Path] = None

        try:
            channel = await bot.fetch_target_channel()
            upload_limit = channel.guild.filesize_limit if hasattr(channel, 'guild') else 25 * 1024 * 1024

            if link:
                temp_file, _ = await bot.download_from_link(link, interaction.id, upload_limit)
            else:
                temp_file = await bot.save_attachment(image, interaction.id, upload_limit)

            magic_type = MediaProcessor.get_magic_type(temp_file)
            if magic_type != 'unknown' and temp_file.suffix.lower() != f'.{magic_type}':
                proper_temp_file = temp_file.with_name(f"temp_{interaction.id}.{magic_type}")
                shutil.move(str(temp_file), str(proper_temp_file))
                temp_file = proper_temp_file

            try:
                final_file = await asyncio.to_thread(MediaProcessor.convert_to_gif, temp_file)
            except Exception as e:
                await safe_reply(f"❌ **Conversion Error:** `{e}`")
                return

            file_size = os.path.getsize(final_file)
            if file_size > upload_limit:
                if file_size <= upload_limit * 1.5:
                    compressed_file = await asyncio.to_thread(MediaProcessor.compress_gif, final_file, upload_limit)
                    
                    if compressed_file != final_file:
                        if final_file != temp_file and final_file.exists():
                            final_file.unlink()
                        final_file = compressed_file
                        
                    file_size = os.path.getsize(final_file)

                if file_size > upload_limit:
                    await safe_reply(
                        f"❌ **Converted File Too Large:** Even after optimization, the GIF ({file_size / (1024 * 1024):.1f} MB) "
                        f"exceeds the server's {upload_limit / (1024 * 1024):.1f} MB limit."
                    )
                    return

            try:
                with open(final_file, 'rb') as f:
                    discord_file = discord.File(f, filename=final_file.name)
                    source_text = f"<{link}>" if link else f"uploaded image (`{image.filename}`)"
                    msg = await channel.send(content=f"Archived from: {source_text}", file=discord_file)
                    
                await safe_reply(f"✅ **Saved successfully!**\n[Click here to jump to the GIF]({msg.jump_url})")

            except discord.errors.HTTPException as e:
                if e.status == 413 or e.code == 40005:
                    await safe_reply(
                        f"❌ **Upload Failed:** Discord rejected the file (Payload Too Large). "
                        f"Size: {file_size / (1024*1024):.1f} MB."
                    )
                else:
                    await safe_reply(f"❌ **Discord API Error:** `{e}`")

        except ValueError as ve:
            await safe_reply(f"❌ {str(ve)}")
            
        except Exception as e:
            await safe_reply(f"❌ An unexpected error occurred: `{str(e)}`")
            source_log = link if link else image.filename
            print(f"Error archiving {source_log}: {e}")
            
        finally:
            if temp_file and temp_file.exists():
                temp_file.unlink()
            if final_file and final_file.exists() and final_file != temp_file:
                final_file.unlink()

    return bot, config


if __name__ == "__main__":
    app_bot, app_config = initialize_app()
    app_bot.run(app_config.bot_token)